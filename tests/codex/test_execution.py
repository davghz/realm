"""Black-box execution and evidence tests, confined to disposable Realm stores."""

import json
import os
import sqlite3
import subprocess
import sys

if __package__:
    from .support import RealmCase
else:
    from support import RealmCase


class ExecutionTests(RealmCase):
    def test_python_entrypoints_find_schema_through_symlinks(self):
        bin_dir = self.project / "bin"
        bin_dir.mkdir()
        for name in ("realm", "realm-py"):
            with self.subTest(entrypoint=name):
                link = bin_dir / name
                link.symlink_to(self.project / name)
                target = self.project / f"{name}-workspace"
                target.mkdir()
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-S", str(link), "init", "--path", str(target)],
                    cwd=target,
                    env=dict(os.environ, REALM_IMPL="py", REALM_AGENT="alpha"),
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                db = target / ".realm" / "space.db"
                with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (2,))

    def python_command(self, code, *arguments):
        return (sys.executable, "-I", "-B", "-S", "-c", code, *arguments)

    def test_documented_run_separator_preserves_literal_arguments(self):
        recorded = self.project / "executed-argv.json"
        arguments = [
            "two words",
            "semi;colon",
            "$(printf expanded)",
            "`printf expanded`",
            "*.sql",
            "",
            '"quoted value"',
            "$__REALM_TEST_LITERAL_MISSING__",
        ]
        command = self.python_command(
            "import json,sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))",
            str(recorded), *arguments,
        )
        self.command("run", "--", *command, expected=0)
        self.assertEqual(json.loads(recorded.read_text()), arguments)
        attempts = self.rows("SELECT argv_json FROM attempts WHERE run_kind='run'")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(json.loads(attempts[0]["argv_json"]), list(command))

    def test_child_exit_status_is_preserved_and_recorded(self):
        task = self.add("task", "record an unsuccessful execution")
        command = self.python_command("raise SystemExit(17)")
        self.command(
            "run", "--attach", task,
            *command,
            agent="execution-agent", expected=17,
        )

        attempts = self.rows(
            "SELECT a.*, n.created_by FROM attempts a "
            "JOIN nodes n ON n.id=a.node_id WHERE a.run_kind='run'"
        )
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(attempt["target_id"], task)
        self.assertEqual(attempt["created_by"], "execution-agent")
        self.assertEqual(attempt["exit_code"], 17)
        self.assertEqual(json.loads(attempt["argv_json"]), list(command))
        self.assertIsNotNone(attempt["started_at"])
        self.assertIsNotNone(attempt["finished_at"])
        self.assertLessEqual(attempt["started_at"], attempt["finished_at"])

        events = self.rows("SELECT * FROM events WHERE op='run'")
        self.assertEqual(len(events), 1)
        self.assertIn(events[0]["node_id"], (attempt["node_id"], task))
        self.assertEqual(events[0]["actor"], "execution-agent")
        self.assertIn("17", events[0]["detail"])

    def test_missing_executable_is_a_controlled_failure(self):
        missing = self.project / "realm-command-does-not-exist"
        result = self.command("run", str(missing))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn(missing.name, result.stderr)

    def test_unknown_attachment_is_rejected_before_command_side_effect(self):
        marker = self.project / "command-must-not-have-run"
        result = self.command(
            "run", "--attach", "T999999999",
            *self.python_command(
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('executed')",
                str(marker),
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists(), "an invalid attachment was checked only after executing the command")
        self.assertNotIn("Traceback", result.stderr)
