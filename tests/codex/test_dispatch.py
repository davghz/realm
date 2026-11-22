"""Orchestration through the CLI and subprocess boundary in disposable spaces."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import patch

if __package__:
    from .support import RealmCase
else:
    from support import RealmCase


WORKER = r'''
import json
import os
from pathlib import Path
import sys
import time

harness = Path(sys.argv[0]).name
prompt = sys.stdin.read()
with Path("calls.jsonl").open("a") as log:
    log.write(json.dumps({"harness": harness, "identity": os.environ["REALM_AGENT"],
                          "space": os.environ["REALM_SPACE"], "pid": os.getpid(),
                          "prompt": prompt}) + "\n")
mode = Path("mode").read_text()
if "You are the Realm planner." in prompt:
    result = {"tasks": [
        {"agent": "codex", "title": "Count units", "instructions": "Sum the fixture."},
        {"agent": "opencode", "title": "Review units", "instructions": "Check the first result."},
    ]}
else:
    assert "You are a Realm worker." in prompt, prompt
    if mode == "wait":
        time.sleep(45)
    if mode == "blocked":
        result = {"status": "blocked", "findings": "The fixture is unavailable."}
    elif harness == "codex":
        result = {"status": "done", "findings": "Counted 49 units."}
    else:
        evidence = list(Path(os.environ["REALM_SPACE"]).glob("runs/**/*.md"))
        assert ("Counted 49 units." in prompt or
                any(str(path) in prompt and "Counted 49 units." in path.read_text()
                    for path in evidence)), prompt
        result = {"status": "done", "findings": "Confirmed the worker's total independently."}

text = json.dumps(result)
if harness == "codex":
    print(json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": text}}))
    print(json.dumps({"type": "turn.completed", "usage": {}}))
else:
    text = f"Independent check complete.\n\n```json\n{text}\n```"
    print(json.dumps({"type": "text", "part": {"type": "text", "text": text}}))
    print(json.dumps({"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}}))
'''


class DispatchTests(RealmCase):
    def setUp(self):
        super().setUp()
        bin_dir = self.project / "bin"
        bin_dir.mkdir()
        for harness in ("codex", "opencode"):
            executable = bin_dir / harness
            executable.write_text(f"#!{sys.executable}\n" + WORKER)
            executable.chmod(0o755)
        self.env_patch = patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        (self.project / "mode").write_text("success")

    def calls(self):
        path = self.project / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def report(self, result):
        paths = [line.removeprefix("Report: ") for line in result.stdout.splitlines()
                 if line.startswith("Report: ")]
        self.assertEqual(len(paths), 1, result.stdout)
        return Path(paths[0]).read_text()

    def assert_no_owners(self):
        self.assertEqual(self.rows("SELECT owner FROM leases WHERE owner IS NOT NULL"), [])

    def test_goal_launches_two_worker_clis_and_registers_their_results(self):
        result = self.command("orchestrate", "--timeout", "5", "--", "Reconcile the fixture units",
                              agent="coordinator", expected=0)
        calls = self.calls()
        self.assertEqual([call["harness"] for call in calls], ["codex", "codex", "opencode"])
        self.assertTrue(all(Path(call["space"]).resolve() == self.space.resolve() for call in calls))
        self.assertEqual(len({call["identity"] for call in calls}), 3)

        tasks = self.rows("SELECT nodes.id, title, state FROM nodes JOIN tasks ON node_id=id ORDER BY id")
        self.assertEqual([row["state"] for row in tasks], ["DONE", "DONE", "DONE"])
        self.assertEqual([row["title"] for row in tasks[1:]], ["Count units", "Review units"])
        self.assertEqual(
            self.rows("SELECT to_id FROM edges WHERE from_id=? AND kind='requires' ORDER BY to_id",
                      (tasks[0]["id"],)),
            [{"to_id": tasks[1]["id"]}, {"to_id": tasks[2]["id"]}],
        )
        for task, call in zip(tasks[1:], calls[1:]):
            outputs = self.rows(
                "SELECT created_by, source_path FROM edges JOIN artifacts ON node_id=to_id "
                "JOIN nodes ON nodes.id=to_id WHERE from_id=? AND edges.kind='produces'",
                (task["id"],),
            )
            self.assertEqual([output["created_by"] for output in outputs], [call["identity"]])
            self.assertTrue(Path(outputs[0]["source_path"]).is_file())
        self.assertIn("Counted 49 units.", self.report(result))
        self.assertIn("Confirmed the worker's total", self.report(result))
        self.assert_no_owners()

    def test_blocked_worker_keeps_goal_open_and_skips_dependent_worker(self):
        (self.project / "mode").write_text("blocked")
        result = self.command("orchestrate", "--timeout", "5", "--", "Reconcile fixture")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual([call["harness"] for call in self.calls()], ["codex", "codex"])
        self.assertEqual(self.rows("SELECT DISTINCT state FROM tasks"), [{"state": "OPEN"}])
        self.assertIn("The fixture is unavailable.", self.report(result))
        self.assertIn("The fixture is unavailable.", result.stderr)
        self.assert_no_owners()

    def test_visible_terminal_handoff_uses_the_same_worker_lease(self):
        env = dict(os.environ, REALM_LENS_BRIDGE="fixture-bridge", REALM_SPACE=str(self.space),
                   REALM_AGENT="screen-test")
        command = [sys.executable, str(self.cli), "orchestrate", "--timeout", "5", "--planner",
                   "opencode", "--workers", "codex,claude", "--", "Read the fixture"]
        turns = []
        directories = []
        with subprocess.Popen(command, cwd=self.project, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
            for line in child.stdout:
                if not line.startswith("REALM_PANE "):
                    continue
                request = json.loads(line.removeprefix("REALM_PANE "))
                turns.append((request["role"], request["agent"]))
                directory = Path(request["directory"])
                for previous in directories:
                    self.assertTrue((previous / "reply.json").is_file())
                directories.append(directory)
                self.assertEqual(directory.parent.resolve(), (self.space / "tmp").resolve())
                self.assertEqual(request["bridge"], "fixture-bridge")
                task = request["task"]
                self.assertEqual(self.lease(task)["owner"], request["identity"])
                brief = (directory / "assignment.txt").read_text()
                if request["role"] == "orchestrator":
                    reply = {"tasks": [{"agent": name, "title": name, "instructions": "Submit exactly the fields rows and units."}
                                       for name in ("codex", "claude")]}
                else:
                    self.assertIn('inside "findings"', brief.split("Earlier reports:", 1)[1])
                    self.assertIn('"status" ("done" or "blocked") and "findings"', brief)
                    reply = {"status": "done", "findings": {"rows": 4}}
                    if request["agent"] == "codex":
                        # Some real CLIs close their assignment before returning and
                        # independently save a report. Both results must survive.
                        self.command("done", task, agent=request["identity"], expected=0)
                        (self.space / "runs" / request["run"] / "report.md").write_text("worker note")
                    else:
                        self.assertIn("codex.md", brief)
                (directory / "reply.json").write_text(json.dumps(reply))
            self.assertEqual(child.wait(timeout=8), 0)
        self.assertEqual(turns, [("orchestrator", "opencode"), ("worker", "codex"), ("worker", "claude")])
        self.assertEqual(self.calls(), [])
        self.assertTrue(all(not directory.exists() for directory in directories))
        self.assert_no_owners()
        self.assertEqual(self.rows("SELECT DISTINCT state FROM tasks"), [{"state": "DONE"}])
        self.assertEqual(next(self.space.glob("runs/*/report.md")).read_text(), "worker note")
        self.assertIn("all worker tasks completed", next(self.space.glob("runs/*/report-*.md")).read_text())

    def test_dispatch_checks_existing_leases_and_fact_gate(self):
        fact = self.add("fact", "Fixture must be verified", "--check", self.file_check())
        task = self.add("task", "Count units", "--depends-on", fact)
        self.command("take", task, agent="first-owner", expected=0)
        self.command("dispatch", task, "--agent", "codex", expected=4)
        self.assertEqual(self.lease(task)["owner"], "first-owner")
        self.assertEqual(self.calls(), [])
        self.expire(task)

        self.command("dispatch", task, "--agent", "codex", expected=2)
        self.assertEqual(self.node(task)["state"], "OPEN")
        self.assert_no_owners()
        self.command("verify", fact, expected=0)
        self.command("dispatch", task, "--agent", "codex", expected=0)
        self.assertEqual(self.node(task)["state"], "DONE")
        self.assert_no_owners()

    def test_cancel_with_closed_progress_keeps_report_and_releases_workers(self):
        (self.project / "mode").write_text("wait")
        command = [sys.executable, "-I", "-B", "-S", str(self.cli), "orchestrate", "Verify fixture"]
        env = {**os.environ, "REALM_SPACE": str(self.space), "REALM_AGENT": "coordinator"}
        child = subprocess.Popen(command, cwd=self.project, env=env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        worker_pid = None
        try:
            end = time.monotonic() + 8
            while time.monotonic() < end and len(self.calls()) < 2 and child.poll() is None:
                time.sleep(0.05)
            self.assertEqual(len(self.calls()), 2, "worker never received its task")
            worker_pid = self.calls()[1]["pid"]
            self.assertEqual(self.lease("T002")["owner"], self.calls()[1]["identity"])
            child.stdout.close()
            child.send_signal(signal.SIGINT)
            self.assertEqual(child.wait(timeout=8), 130)
            self.assertEqual(self.rows("SELECT DISTINCT state FROM tasks"), [{"state": "OPEN"}])
            self.assert_no_owners()
            report, = self.space.glob("runs/*/report.md")
            self.assertIn("Worker canceled", report.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(worker_pid, 0)
        finally:
            if child.poll() is None:
                child.kill()
            child.stdout.close()
            child.wait(timeout=5)
            if worker_pid is not None:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
