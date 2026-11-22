"""Plan and dispatch Realm assignments to standalone CLIs or Realm Lens terminals."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import sys
import tempfile
import time
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from realm_workers import AgentError, SUPPORTED_AGENTS, run_agent


class DispatchError(RuntimeError):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Assignment:
    agent: str
    title: str
    instructions: str


@dataclass(frozen=True)
class Result:
    code: int
    path: Path
    findings: str
    detail: str


def _progress(message: str, *, file=None):
    stream = sys.stdout if file is None else file
    try:
        print(message, file=stream, flush=True)
    except BrokenPipeError:
        # A closed window can detach from a run during cancellation. Let the
        # dispatcher finish writing its artifacts and releasing leases.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), stream.fileno())


@contextmanager
def _as_agent(identity: str):
    previous = os.environ.get("REALM_AGENT")
    os.environ["REALM_AGENT"] = identity
    try:
        yield
    finally:
        if previous is None:
            del os.environ["REALM_AGENT"]
        else:
            os.environ["REALM_AGENT"] = previous


@contextmanager
def _lease(core, task: str, ttl: int):
    request = argparse.Namespace(task=task, ttl=ttl, lease_token=None)
    try:
        code = core.cmd_take(request)
        if code != core.EXIT_OK:
            raise DispatchError(f"Could not lease {task}", code)
        yield request.lease_token
    finally:
        if request.lease_token is not None:
            with closing(core.connect()) as con, core.txn(con):
                cleared = con.execute(
                    "UPDATE leases SET owner=NULL, acquired_at=NULL, expires_at=NULL "
                    "WHERE resource=? AND owner=? AND fencing_token=?",
                    (task, core.actor(), request.lease_token),
                )
                if cleared.rowcount:
                    core.emit(con, "release", task, task, request.lease_token)


def _finish(core, task: str, token: int) -> int:
    return core.cmd_done(argparse.Namespace(
        task=task, lease_token=token, require_tasks_done=True,
    ))


def _add_node(core, con, kind: str, title: str, body: str) -> str:
    node = core.new_id(con, kind)
    now = core.now_ts()
    con.execute(
        "INSERT INTO nodes(id,kind,title,body,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (node, kind, title, body, core.actor(), now, now),
    )
    core.emit(con, "add", node, detail=f"{kind}:{title[:60]}")
    return node


def _add_task(core, con, title: str, body: str) -> str:
    node = _add_node(core, con, "task", title, body)
    con.execute("INSERT INTO tasks(node_id) VALUES (?)", (node,))
    return node


def _edge(core, con, source: str, kind: str, target: str):
    con.execute(
        "INSERT INTO edges(from_id,to_id,kind,at) VALUES (?,?,?,?)",
        (source, target, kind, core.now_ts()),
    )
    core.emit(con, "link", source, detail=f"{kind}->{target}")


def _task(core, task: str):
    with closing(core.connect()) as con:
        node = core.get_node(con, task)
        if node["kind"] != "task":
            raise DispatchError(f"{task} is a {node['kind']}, not a task", core.EXIT_USAGE)
        state = con.execute("SELECT state FROM tasks WHERE node_id=?", (task,)).fetchone()["state"]
        if state != "OPEN":
            raise DispatchError(f"{task} is {state}", core.EXIT_REFUSED)
        return node


def _prerequisites(core, task: str) -> list[str]:
    with closing(core.connect()) as con:
        rows = con.execute(
            "SELECT t.node_id,t.state FROM edges e JOIN tasks t ON e.to_id=t.node_id "
            "WHERE e.from_id=? AND e.kind IN ('requires','depends_on') ORDER BY t.node_id",
            (task,),
        )
        pending = [row["node_id"] for row in rows if row["state"] != "DONE"]
        if pending:
            raise DispatchError(f"{task} is waiting for {', '.join(pending)}", core.EXIT_REFUSED)
        return [row["source_path"] for row in con.execute(
            "SELECT DISTINCT a.source_path FROM edges e "
            "JOIN edges p ON p.from_id=e.to_id AND p.kind='produces' "
            "JOIN artifacts a ON a.node_id=p.to_id "
            "WHERE e.from_id=? AND e.kind IN ('requires','depends_on') "
            "AND a.source_path IS NOT NULL ORDER BY a.source_path",
            (task,),
        )]


def _plans(core, goal: str, assignments: list[Assignment]) -> list[tuple[str, Assignment]]:
    tasks = []
    with closing(core.connect()) as con, core.txn(con):
        for assignment in assignments:
            body = f"Assigned CLI: {assignment.agent}\n\n{assignment.instructions}"
            task = _add_task(core, con, assignment.title, body)
            _edge(core, con, goal, "requires", task)
            if tasks:
                _edge(core, con, task, "requires", tasks[-1][0])
            tasks.append((task, assignment))
    return tasks


def _json_reply(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        blocks = re.findall(r"^```(?:json)?[ \t]*\n(.*?)\n```[ \t]*$", text,
                            re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if len(blocks) != 1:
            raise
        return json.loads(blocks[0])


def _plan(text: str, workers: list[str]) -> list[Assignment]:
    reply = _json_reply(text)
    if not isinstance(reply, dict) or not isinstance(reply.get("tasks"), list):
        raise ValueError("expected a tasks array")
    assigned = []
    remaining = set(workers)
    for step in reply["tasks"]:
        if not isinstance(step, dict):
            raise ValueError("each task must be an object")
        agent = step.get("agent")
        title = step.get("title")
        instructions = step.get("instructions")
        if not isinstance(agent, str) or agent not in remaining:
            raise ValueError("assign exactly one task to each selected CLI")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("each task needs a title")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("each task needs instructions")
        remaining.remove(agent)
        assigned.append(Assignment(agent, " ".join(title.split()), instructions.strip()))
    if remaining:
        raise ValueError(f"missing task assignments for {', '.join(sorted(remaining))}")
    return assigned


def _result(text: str) -> tuple[str, str]:
    reply = _json_reply(text)
    if not isinstance(reply, dict) or reply.get("status") not in ("done", "blocked"):
        raise ValueError("expected status done or blocked")
    findings = reply.get("findings")
    if isinstance(findings, (dict, list)) and findings:
        findings = json.dumps(findings, ensure_ascii=False, indent=2)
    if not isinstance(findings, str) or not findings.strip():
        raise ValueError("expected nonempty findings")
    return reply["status"], findings.strip()


def _planner_prompt(goal: str, workers: list[str]) -> str:
    return f"""You are the Realm planner.
Plan the goal below using the selected CLIs: {', '.join(workers)}.
Plan exactly one task per selected CLI. List work in the order it should run.
Later workers will have access to artifacts from earlier workers in the same workspace.
Give specific instructions for producing and checking each result. Only plan the work.
If a worker should return named fields, specify them inside its "findings" value.
Realm supplies the worker's top-level reply format and submission instructions.
Put the complete plan JSON directly in your final reply. The object has one key,
"tasks", an array of objects with string keys "agent", "title", and "instructions".

Goal:
{goal}
"""


def _worker_prompt(task: str, assignment: Assignment, goal: str, paths: list[str],
                   space: Path, identity: str) -> str:
    resources = "\n".join(f"- {path}" for path in paths) or "None."
    return f"""You are a Realm worker.
Realm has leased {task} to your identity. Complete this assignment using the shared
workspace, following its instructions. Realm records your result and closes the
task if it passes the gates. Use Realm's evidence gates for any facts you cite.
Return findings in your final reply. The dispatcher owns the lease, the done
command and files under .realm/runs; leave those to the dispatcher.
For Realm commands use REALM_SPACE={shlex.quote(str(space))} and REALM_AGENT={shlex.quote(identity)}.
Read relevant earlier reports and verify claims when your assignment needs them.

Overall goal:
{goal}

Assignment {task}: {assignment.title}
{assignment.instructions}

Earlier reports:
{resources}

Realm reply format for {task}:
Return a JSON object with "status" and "findings". Use "done" when the assignment
is complete or "blocked" when you cannot finish. If the assignment requests
specific keys or a JSON object, put that entire result inside "findings".
For example: {{"status":"done","findings":{{"observed_value":"what you checked"}}}}.
A plain string containing the evidence is also valid for "findings".
"""


class Run(ExitStack):
    def __init__(self, core, timeout: float, full_access: bool = False):
        super().__init__()
        self.core = core
        self.timeout = timeout
        self.full_access = full_access
        self.space = core.space_dir().resolve()
        if not (self.space / "space.db").is_file():
            raise DispatchError("space not initialised. run `realm init`.", core.EXIT_USAGE)
        runs = self.space / "runs"
        runs.mkdir(exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="run-", dir=runs))

    def identity(self, agent: str) -> str:
        return f"{agent}/{self.directory.name.removeprefix('run-')}"

    def activity(self, agent: str, task: str, op: str, message: str):
        with _as_agent(self.identity(agent)), closing(self.core.connect()) as con:
            self.core.emit(con, op, task, detail=message[:2400])
        _progress(f"[{agent} {task}] {message.splitlines()[0][:240]}")

    def invoke(self, agent: str, task: str, prompt: str, *, role: str, title: str) -> str:
        bridge = os.environ.get("REALM_LENS_BRIDGE")
        if bridge:
            return self.pane_turn(bridge, agent, task, role, title, prompt)
        env = dict(os.environ, REALM_SPACE=str(self.space), REALM_AGENT=self.core.actor())
        return run_agent(agent, prompt, cwd=self.space.parent, env=env, timeout=self.timeout, full_access=self.full_access,
                         on_activity=lambda line: self.activity(agent, task, "agent-output", line))

    def pane_turn(self, bridge, agent, task, role, title, prompt):
        # The Mac app owns the interactive PTY. Its CLI gets a short native startup
        # prompt and can submit structured results without scraping a terminal screen.
        # Keep each participant's private address stable while later turns run.
        # This also lets the TUI reread its own accepted reply during its recap.
        channel = Path(self.enter_context(tempfile.TemporaryDirectory(prefix="pane-", dir=self.space / "tmp")))
        brief, reply = channel / "assignment.txt", channel / "reply.json"
        form = ('The top-level key is "tasks".' if role == "orchestrator" else
                'Use "status" ("done" or "blocked") and "findings" as the top-level keys. '
                'Put any fields requested by the assignment inside "findings".')
        brief.write_text(prompt + f"""

You are working in the visible {agent} terminal. To hand off to Realm, write the
complete final JSON object to {reply} using your file tools. Write a temporary
sibling file and rename it to reply.json after it is complete. {form} Submitting this
file is part of the local Realm protocol, including on read-only website reviews.
Your turn ends when that rename succeeds. Summarize only your own submission in
one sentence here and stop. Realm delivers subsequent assignments to their own
terminals. Use only the exact assignment and reply paths above. If this directory
disappears after submission, Realm has finished the run.
""", encoding="utf-8")
        request = dict(bridge=bridge, agent=agent, role=role, task=task, identity=self.core.actor(),
                       title=title, directory=str(channel), run=self.directory.name)
        _progress("REALM_PANE " + json.dumps(request, ensure_ascii=False))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if (channel / "error.txt").is_file():
                raise AgentError((channel / "error.txt").read_text()[:600])
            if reply.is_file():
                text = reply.read_text(encoding="utf-8")
                try:
                    if isinstance(json.loads(text), dict):
                        return text
                except ValueError:
                    pass  # A writer may still be finishing a non-atomic save.
            time.sleep(0.15)
        raise AgentError(f"{agent} terminal did not submit a reply within {self.timeout:g}s")

    def artifact(self, task: str, filename: str, title: str, text: str) -> Path:
        path = self.directory / filename
        # Separate attempts can return identical findings; artifacts have unique digests.
        content = f"{text}\n\nRealm run: {self.directory.name}\n".encode()
        try:
            file = path.open("xb")
        except FileExistsError:
            # A worker may already have written a separate artifact under this name.
            file = tempfile.NamedTemporaryFile(dir=self.directory, prefix=path.stem + "-",
                                               suffix=path.suffix, delete=False)
            path = Path(file.name)
        with file:
            file.write(content)
        core = self.core
        with closing(core.connect()) as con, core.txn(con):
            node = _add_node(core, con, "artifact", title, "")
            con.execute(
                "INSERT INTO artifacts(node_id,digest,byte_count,source_path) VALUES (?,?,?,?)",
                (node, hashlib.sha256(content).hexdigest(), len(content), str(path)),
            )
            _edge(core, con, task, "produces", node)
        return path

    def work(self, task: str, assignment: Assignment, goal: str, earlier: list[str]) -> Result:
        core = self.core
        with _as_agent(self.identity(assignment.agent)), _lease(core, task, math.ceil(self.timeout) + 60) as token:
            paths = list(dict.fromkeys(earlier + _prerequisites(core, task)))
            _progress(f"Dispatching {task} to {assignment.agent}")
            self.activity(assignment.agent, task, "agent-start", assignment.title + "\n" + assignment.instructions)
            text = ""
            try:
                prompt = _worker_prompt(task, assignment, goal, paths, self.space, core.actor())
                text = self.invoke(assignment.agent, task, prompt, role="worker", title=assignment.title)
                status, findings = _result(text)
            except KeyboardInterrupt:
                status, findings = "canceled", "Worker invocation interrupted."
            except AgentError as error:
                status, findings = "failed", str(error)
            except ValueError as error:
                status, findings = "failed", f"Invalid worker result ({error}).\n\n{text}"

            result_text = f"# {task} findings from {assignment.agent}\n\nWorker result: {status}\n\n{findings}"
            path = self.artifact(task, f"{assignment.agent}.md", f"{task} {assignment.agent} findings", result_text)
            _progress(f"{task} findings: {path}")
            if status == "done":
                code = _finish(core, task, token)
                detail = "Completed" if code == core.EXIT_OK else "Completion gate refused"
            elif status == "canceled":
                code, detail = 130, "Worker canceled"
            else:
                code = core.EXIT_REFUSED if status == "blocked" else core.EXIT_INTERNAL
                detail = f"Worker {status}"
            _progress(f"{task} {detail}")
            self.activity(assignment.agent, task, "agent-end", f"{detail}\n{findings}")
            return Result(code, path, findings, detail)

    def report(self, goal_task: str, goal: str, tasks: list[tuple[str, Assignment]],
               results: dict[str, Result], error: str | None):
        sections = [f"# {goal_task} orchestration report", goal]
        if error:
            sections.append(f"Outcome: {error}")
        else:
            sections.append("Outcome: all worker tasks completed")
        for task, assignment in tasks:
            sections.append(f"## {task} · {assignment.agent} · {assignment.title}")
            if task in results:
                result = results[task]
                sections.extend((result.detail, f"Findings: {result.path}", result.findings))
            else:
                sections.append("Pending, no completed result")
        path = self.artifact(goal_task, "report.md", f"{goal_task} orchestration report",
                             "\n\n".join(sections))
        _progress(f"Report: {path}")


def _validate(core, agents: list[str], timeout: float):
    if not agents or any(agent not in SUPPORTED_AGENTS for agent in agents):
        raise DispatchError(f"Choose agents from {', '.join(SUPPORTED_AGENTS)}", core.EXIT_USAGE)
    if not math.isfinite(timeout) or timeout <= 0:
        raise DispatchError("Timeout must be a positive number of seconds", core.EXIT_USAGE)


def _print_error(command: str, error: Exception):
    # Keep multiline worker or planner text in the artifact, outside the progress stream.
    lines = str(error).splitlines()
    _progress(f"realm {command}: {lines[0] if lines else 'run failed'}", file=sys.stderr)


def dispatch(args, core) -> int:
    try:
        _validate(core, [args.agent], args.timeout)
        node = _task(core, args.task)
        _prerequisites(core, args.task)
        assignment = Assignment(args.agent, node["title"], node["body"])
        with Run(core, args.timeout, args.full_access) as run:
            result = run.work(args.task, assignment, node["title"], [])
        _progress(f"Report: {result.path}")
        return result.code
    except DispatchError as error:
        _print_error("dispatch", error)
        return error.code


def orchestrate(args, core) -> int:
    try:
        _validate(core, [args.planner, *args.workers], args.timeout)
        if not args.workers or len(args.workers) != len(set(args.workers)):
            raise DispatchError("Select at least one worker with no duplicates", core.EXIT_USAGE)
        goal = args.goal.strip()
        if not goal:
            raise DispatchError("A goal is required", core.EXIT_USAGE)
        initiator = core.actor()
        with Run(core, args.timeout, args.full_access) as run:
            owner = run.identity("orchestrator" if initiator == "unknown" else f"{initiator}-orchestrator")
            with _as_agent(owner):
                with closing(core.connect()) as con, core.txn(con):
                    goal_task = _add_task(core, con, " ".join(goal.split())[:120], goal)
                ttl = (math.ceil(args.timeout) + 60) * (len(args.workers) + 1)
                with _lease(core, goal_task, ttl) as token:
                    return _execute_plan(core, run, args, goal_task, goal, token)
    except DispatchError as error:
        _print_error("orchestrate", error)
        return error.code


def _execute_plan(core, run: Run, args, goal_task: str, goal: str, token: int) -> int:
    tasks = []
    results: dict[str, Result] = {}
    try:
        _progress(f"Planning {goal_task} with {args.planner}")
        for worker in args.workers:
            run.activity(worker, goal_task, "agent-queued", f"Waiting for {args.planner} to plan: {goal}")
        run.activity(args.planner, goal_task, "agent-start", f"Planning for {', '.join(args.workers)}\n{goal}")
        try:
            reply = run.invoke(args.planner, goal_task, _planner_prompt(goal, args.workers),
                               role="orchestrator", title=goal)
            assignments = _plan(reply, args.workers)
        except ValueError as error:
            raise DispatchError(f"Invalid planner response ({error}).\n\n{reply}", core.EXIT_INTERNAL) from error
        tasks = _plans(core, goal_task, assignments)
        run.activity(args.planner, goal_task, "agent-end", "Plan ready\n" + "\n".join(
            f"{task} → {assignment.agent}: {assignment.title}" for task, assignment in tasks))
        for task, assignment in tasks:
            run.activity(assignment.agent, task, "agent-queued", "Assigned: " + assignment.title)
        for task, assignment in tasks:
            result = run.work(task, assignment, goal, [str(item.path) for item in results.values()])
            results[task] = result
            if result.code != core.EXIT_OK:
                message = f"{task} {result.detail.lower()}"
                if result.code != 130 and result.findings.strip():
                    message += f". {result.findings.strip().splitlines()[0][:240]}"
                raise DispatchError(message, result.code)
    except AgentError as error:
        run.activity(args.planner, goal_task, "agent-end", f"Planner failed: {error}")
        run.report(goal_task, goal, tasks, results, f"Planner failed. {error}")
        _print_error("orchestrate", error)
        return core.EXIT_INTERNAL
    except DispatchError as error:
        run.report(goal_task, goal, tasks, results, str(error))
        raise
    except KeyboardInterrupt:
        run.report(goal_task, goal, tasks, results, "Canceled")
        raise
    run.report(goal_task, goal, tasks, results, None)
    return _finish(core, goal_task, token)
