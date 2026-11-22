"""Headless worker protocol tests using disposable vendor executables."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "realm_workers", Path(__file__).resolve().parents[2] / "realm_workers.py"
)
workers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workers)


def json_lines(*events):
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="realm-workers-")
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        self.bin_dir = self.project / "bin"
        self.bin_dir.mkdir()
        self.env = dict(
            os.environ, PATH=str(self.bin_dir), REALM_AGENT="fixture-worker",
            REALM_SPACE=str(self.project / ".realm"),
        )
        self.env.pop("REALM_HERMES_TOOLSETS", None)

    def install_fake(self, name, response, *, exit_code=0, stderr=""):
        target = self.bin_dir / name
        target.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path('received.json').write_text(json.dumps({\n"
            "    'prompt': sys.stdin.read(), 'agent': os.environ.get('REALM_AGENT'),\n"
            "    'space': os.environ.get('REALM_SPACE'),\n"
            "    'args': sys.argv[1:],\n"
            "}))\n"
            f"sys.stdout.write({response!r})\n"
            f"sys.stderr.write({stderr!r})\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8",
        )
        target.chmod(0o755)

    def run_worker(self, agent, prompt):
        return workers.run_agent(agent, prompt, cwd=self.project, env=self.env, timeout=5)

    def test_sends_literal_prompt_and_returns_only_final_result(self):
        answer = "11 north\u2028 7 south"
        responses = {
            "codex": json_lines(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Reading rows"}},
                {"type": "error", "message": "Reconnecting"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
                {"type": "turn.completed"},
            ),
            "opencode": json_lines(
                {"type": "text", "part": {"text": "Reading rows"}},
                {"type": "step_finish", "part": {"reason": "tool-calls"}},
                {"type": "text", "part": {"text": answer}},
                {"type": "step_finish", "part": {"reason": "stop"}},
            ),
            "claude": json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": answer}),
            "hermes": json_lines(
                {"type": "text", "text": "Reading rows"},
                {"type": "result", "exit_code": 0, "text": answer},
            ),
            "agy": json_lines(
                {"event": "step_update", "step_update": {"text_delta": "Reading rows"}},
                {"event": "result", "result": {"status": "SUCCESS", "response": answer}},
            ),
        }
        self.assertCountEqual(responses, workers.SUPPORTED_AGENTS)
        prompt = "Sum 'orders & totals.csv'\u2028 and include the literal $HOME."
        for agent, response in responses.items():
            with self.subTest(agent=agent):
                self.install_fake(agent, response)
                self.assertEqual(self.run_worker(agent, prompt), answer)
                received = json.loads((self.project / "received.json").read_text(encoding="utf-8"))
                self.assertEqual(received["agent"], self.env["REALM_AGENT"])
                self.assertEqual(received["space"], self.env["REALM_SPACE"])
                if agent == "agy":
                    self.assertEqual(json.loads(received["prompt"]),
                                     {"event": "user", "message": {"content": prompt}})
                else:
                    self.assertEqual(received["prompt"], prompt)
                self.assertNotIn(workers._FULL_ACCESS[agent], received["args"])
                self.assertEqual(workers.run_agent(agent, prompt, cwd=self.project, env=self.env,
                                                   timeout=5, full_access=True), answer)
                args = json.loads((self.project / "received.json").read_text())["args"]
                self.assertIn(workers._FULL_ACCESS[agent], args)
                if agent == "opencode":
                    self.assertLess(args.index("run"), args.index("--auto"))
                if agent == "codex":
                    self.assertNotIn("--sandbox", args)

    def test_vendor_error_with_zero_process_exit_is_a_failure(self):
        cases = [
            ("codex", json_lines({"type": "turn.failed", "error": {"message": "could not connect"}}),
             "could not connect"),
            ("opencode", json_lines({"type": "error", "error": {"data": {"message": "invalid token"}}}),
             "invalid token"),
            ("claude", json.dumps({"type": "result", "subtype": "success",
                                   "is_error": True, "result": "Credit balance is too low"}),
             "Credit balance is too low"),
            ("hermes", json_lines({"type": "result", "exit_code": 3, "error": "input required"}),
             "input required"),
            ("agy", json_lines({"event": "result", "result": {"status": "ERROR", "error": "invalid config"}}),
             "invalid config"),
        ]
        for agent, response, expected in cases:
            with self.subTest(agent=agent):
                self.install_fake(agent, response)
                with self.assertRaisesRegex(workers.AgentError, expected):
                    self.run_worker(agent, "Read orders.csv.")

    def test_tool_activity_is_delivered_before_the_cli_replies(self):
        target = self.bin_dir / "codex"
        target.write_text(f"#!{sys.executable}\n" + '''
import json, sys, time
from pathlib import Path
sys.stdin.read()
print(json.dumps({"type":"item.started","item":{"type":"command_execution","command":"read index.html"}}), flush=True)
for _ in range(100):
    if Path("observed").exists():
        break
    time.sleep(0.02)
else:
    sys.exit(3)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"four species"}}))
print(json.dumps({"type":"turn.completed"}))
''')
        target.chmod(0o755)
        def observe(message):
            if "read index.html" in message:
                (self.project / "observed").touch()
        self.assertEqual(workers.run_agent("codex", "Check the site", cwd=self.project,
                                          env=self.env, timeout=5, on_activity=observe), "four species")

    def test_opencode_incomplete_step_cannot_be_returned_as_a_reply(self):
        self.install_fake("opencode", json_lines(
            {"type": "text", "part": {"text": "partial answer"}},
            {"type": "step_finish", "part": {"reason": "length"}},
        ))
        with self.assertRaisesRegex(workers.AgentError, "did not finish normally"):
            self.run_worker("opencode", "Read orders.csv.")

    def test_hermes_pins_workspace_and_accepts_explicit_toolsets(self):
        self.install_fake("hermes", json_lines({"type": "result", "exit_code": 0, "text": "21"}))
        expected = [
            "chat", "--query-file", "-", "--format", "stream-json",
            "--in", str(self.project.resolve()),
        ]
        receipt = self.project / "received.json"
        self.assertEqual(self.run_worker("hermes", "Read orders.csv."), "21")
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["args"], expected)
        self.env["REALM_HERMES_TOOLSETS"] = "file"
        self.assertEqual(self.run_worker("hermes", "Read orders.csv."), "21")
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["args"],
                         expected + ["--toolsets", "file"])

    def test_nonzero_exit_is_a_failure_even_with_a_successful_reply(self):
        self.install_fake("claude", json.dumps({"type": "result", "subtype": "success", "result": "11"}),
                          exit_code=7, stderr="worker could not close its session")
        with self.assertRaisesRegex(workers.AgentError, "exited with status 7.*could not close"):
            self.run_worker("claude", "Read orders.csv.")

    def test_timeout_and_interrupt_stop_the_private_process_group(self):
        failures = [
            (subprocess.TimeoutExpired("codex", 5), workers.AgentError),
            (KeyboardInterrupt(), KeyboardInterrupt),
        ]
        for exception, expected in failures:
            with self.subTest(exception=type(exception)), \
                    patch.object(workers.subprocess, "Popen") as popen, \
                    patch.object(workers.os, "killpg") as kill_group:
                child = popen.return_value.__enter__.return_value
                child.pid = 12345
                child.communicate.side_effect = exception
                with self.assertRaises(expected):
                    self.run_worker("codex", "Read orders.csv.")
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
                kill_group.assert_called_once_with(child.pid, signal.SIGKILL)
