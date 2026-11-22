"""Transport a single Realm assignment through an agent CLI's headless interface."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time


class AgentError(RuntimeError):
    """The worker did not return a successful final reply."""


def _object(value):
    return value if isinstance(value, dict) else {}


def _json_object(text: str):
    try:
        value = json.loads(text)
    except ValueError as error:
        raise AgentError("invalid JSON response") from error
    if not isinstance(value, dict):
        raise AgentError("expected a JSON object")
    return value


def _events(stdout: str):
    for line in stdout.split("\n"):
        if line.strip():
            yield _json_object(line)


def _error_detail(value) -> str:
    error = _object(value)
    detail = error.get("message") or _object(error.get("data")).get("message") or error.get("name") or value
    return detail.strip() if isinstance(detail, str) else ""


def _reply(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentError("no final reply")
    return value.strip()


def _codex_reply(stdout: str) -> str:
    answer = None
    diagnostic = ""
    for event in _events(stdout):
        kind = event.get("type")
        if kind == "item.completed":
            item = _object(event.get("item"))
            if item.get("type") == "agent_message":
                answer = item.get("text")
        elif kind == "turn.completed":
            return _reply(answer)
        elif kind == "turn.failed":
            raise AgentError(_error_detail(event.get("error")) or diagnostic or "turn failed")
        elif kind == "error":
            diagnostic = _error_detail(event.get("message"))
    raise AgentError(diagnostic or "turn did not complete")


def _opencode_reply(stdout: str) -> str:
    parts = []
    finish_reason = None
    answer = None
    for event in _events(stdout):
        kind = event.get("type")
        part = _object(event.get("part"))
        if kind == "step_start":
            finish_reason = None
            parts.clear()
        elif kind == "text":
            value = part.get("text")
            if isinstance(value, str):
                parts.append(value)
        elif kind == "step_finish":
            finish_reason = part.get("reason")
            answer = "\n".join(parts)
            parts.clear()
        elif kind == "error":
            raise AgentError(_error_detail(event.get("error")) or "turn failed")
    if finish_reason != "stop" or parts:
        raise AgentError(f"turn did not finish normally ({finish_reason or 'no final step'})")
    return _reply(answer)


def _claude_reply(stdout: str) -> str:
    try:
        result = _json_object(stdout)
    except AgentError:
        result = next((e for e in _events(stdout) if e.get("type") == "result"), {})
    if result.get("type") != "result":
        raise AgentError("no result record")
    if result.get("is_error") or result.get("subtype") != "success":
        raise AgentError(_error_detail(result.get("result")) or "turn failed")
    return _reply(result.get("result"))


def _hermes_reply(stdout: str) -> str:
    for event in _events(stdout):
        if event.get("type") == "result":
            if event.get("exit_code") != 0:
                raise AgentError(_error_detail(event.get("error")) or "turn failed")
            return _reply(event.get("text"))
    raise AgentError("no result record")


def _agy_reply(stdout: str) -> str:
    for event in _events(stdout):
        if event.get("event") == "result":
            result = _object(event.get("result"))
            if result.get("status") != "SUCCESS":
                raise AgentError(_error_detail(result.get("error")) or "turn failed")
            return _reply(result.get("response"))
    raise AgentError("no result record")


_ADAPTERS = {
    "codex": (
        ("exec", "--json", "--ephemeral", "--skip-git-repo-check", "--color", "never", "-"),
        _codex_reply,
    ),
    "opencode": (
        ("--standalone", "run", "--format", "json", "--archived"),
        _opencode_reply,
    ),
    "claude": (
        ("--print", "--output-format", "json", "--no-session-persistence", "--permission-prompts", "none"),
        _claude_reply,
    ),
    "hermes": (("chat", "--query-file", "-", "--format", "stream-json"), _hermes_reply),
    "agy": (
        ("--input-format", "stream-json", "--output-format", "stream-json", "--disable-slash-commands"),
        _agy_reply,
    ),
}

SUPPORTED_AGENTS = tuple(_ADAPTERS)
_FULL_ACCESS = {
    "codex": "--dangerously-bypass-approvals-and-sandbox",
    "opencode": "--auto",
    "claude": "--dangerously-skip-permissions",
    "hermes": "--yolo",
    "agy": "--dangerously-skip-permissions",
}


def _activity(event: dict):
    """Render public CLI messages and tool activity. Protocol payloads stay out of the space."""
    kind = event.get("type") or event.get("event")
    item = _object(event.get("item"))
    part = _object(event.get("part"))
    if kind in ("turn.started", "step_start"):
        return "Turn started" if kind == "turn.started" else "Step started"
    if kind in ("item.started", "item.completed"):
        name = item.get("type")
        if name == "agent_message":
            return item.get("text")
        if name == "command_execution":
            command = str(item.get("command", "")).splitlines()
            label = "Running" if kind == "item.started" else f"Exit {item.get('exit_code', '?')}"
            return f"{label} · {command[0][:240] if command else 'command'}"
        if name in ("mcp_tool_call", "tool_call", "dynamic_tool_call"):
            return f"{'Calling' if kind == 'item.started' else 'Finished'} · {item.get('tool') or item.get('name') or 'tool'}"
        if name == "file_change":
            paths = [c["path"] for c in item.get("changes", []) if isinstance(c, dict) and isinstance(c.get("path"), str)]
            return "File change · " + ", ".join(paths)
    if kind == "tool_use":
        state = _object(part.get("state"))
        arguments = _object(state.get("input"))
        detail = arguments.get("description") or arguments.get("filePath") or arguments.get("command")
        return f"{part.get('tool', 'Tool')} · {detail or state.get('title') or state.get('status') or 'called'}"
    if kind == "text":
        return part.get("text") or event.get("text")
    if kind == "assistant":
        blocks = _object(event.get("message")).get("content", [])
        return "\n".join(str(b.get("text") if b.get("type") == "text" else f"Calling · {b.get('name')}")
                         for b in blocks if isinstance(b, dict) and b.get("type") in ("text", "tool_use"))
    if kind in ("tool_call", "tool_start", "tool_result", "tool_end"):
        return f"{kind.replace('_', ' ')} · {event.get('tool_name') or event.get('name') or 'tool'}"
    if kind in ("error", "turn.failed"):
        return _error_detail(event.get("error") or event.get("message"))
    if kind == "result":
        result = event.get("result")
        return result if isinstance(result, str) else event.get("text") or _object(result).get("response")
    return None


def _stream_stdout(child, timeout, agent, notify):
    """Drain the actual child pipe while it runs, including partial UTF-8 and split lines."""
    deadline = time.monotonic() + timeout
    raw = bytearray()
    line_start = 0
    previous = None

    def publish(line):
        nonlocal previous
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return
        message = _activity(_object(event))
        if isinstance(message, str) and message.strip() and message != previous:
            previous = message
            notify(message.strip()[:1600])

    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(agent, timeout)
            old_end = len(raw)
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:
                if line_start < len(raw):
                    publish(raw[line_start:])
                break
            raw.extend(chunk)
            newline = raw.find(b"\n", old_end)
            while newline >= 0:
                publish(raw[line_start:newline])
                line_start = newline + 1
                newline = raw.find(b"\n", line_start)
    child.wait(timeout=max(0, deadline - time.monotonic()))
    return raw.decode("utf-8", errors="replace")


def run_agent(agent: str, prompt: str, *, cwd: Path, env: Mapping[str, str], timeout: float,
              on_activity: Callable[[str], None] | None = None, full_access: bool = False) -> str:
    """Run one turn in cwd and return only the agent's final response."""
    if agent not in _ADAPTERS:
        raise AgentError(f"unknown worker {agent!r}")
    if not math.isfinite(timeout) or timeout <= 0:
        raise AgentError("worker timeout must be a positive finite number")
    args, read_reply = _ADAPTERS[agent]
    if full_access:
        # OpenCode's --auto belongs to the run subcommand for headless sessions.
        args = (*args, _FULL_ACCESS[agent]) if agent == "opencode" else (_FULL_ACCESS[agent], *args)
    elif agent == "codex":
        args = ("-a", "never", "--sandbox", "workspace-write", *args)
    if agent == "agy":
        prompt = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
    elif agent == "hermes":
        args += ("--in", str(cwd.resolve()))
        toolsets = env.get("REALM_HERMES_TOOLSETS", "").strip()
        if toolsets:
            args += ("--toolsets", toolsets)
    elif agent == "claude" and on_activity:
        args = tuple("stream-json" if arg == "json" else arg for arg in args) + ("--verbose",)

    try:
        with ExitStack() as files:
            stdin = stderr_file = subprocess.PIPE
            if on_activity:
                stdin = files.enter_context(tempfile.TemporaryFile())
                stdin.write(prompt.encode())
                stdin.seek(0)
                stderr_file = files.enter_context(tempfile.TemporaryFile())
            child = files.enter_context(subprocess.Popen(
                [agent, *args], cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE,
                stderr=stderr_file, start_new_session=True, encoding="utf-8", errors="replace",
            ))
            try:
                if on_activity:
                    stdout = _stream_stdout(child, timeout, agent, on_activity)
                    stderr_file.seek(0)
                    stderr = stderr_file.read().decode("utf-8", errors="replace") if child.returncode else ""
                else:
                    stdout, stderr = child.communicate(prompt, timeout=timeout)
            except BaseException:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                raise
    except subprocess.TimeoutExpired as error:
        raise AgentError(f"{agent} timed out after {timeout:g}s") from error
    except OSError as error:
        raise AgentError(f"could not run {agent}: {error}") from error

    if child.returncode:
        detail = ""
        if stdout.strip():
            try:
                read_reply(stdout)
            except AgentError as error:
                detail = str(error)
        if not detail:
            diagnostic = stderr.strip().splitlines()
            detail = diagnostic[-1] if diagnostic else ""
        suffix = f": {detail[:600]}" if detail else ""
        raise AgentError(f"{agent} exited with status {child.returncode}{suffix}")
    try:
        return read_reply(stdout)
    except AgentError as error:
        raise AgentError(f"{agent}: {error}") from error
