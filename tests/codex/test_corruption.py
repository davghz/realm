"""Failure-path tests: what realm does when the space is not healthy.

These are the paths a user hits at the worst moment — a damaged file, a space from a
newer build — and the behaviour that matters is refusing cleanly rather than crashing
or, worse, writing into a schema it does not understand.
"""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

if __package__:
    from .support import NATIVE_CLI, install_companions
else:
    from support import NATIVE_CLI, install_companions

PROJECT = Path(__file__).resolve().parents[2]
SOURCE_BYTES = (PROJECT / "realm").read_bytes()
SCHEMA_BYTES = (PROJECT / "schema.sql").read_bytes()


class CorruptionCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="realm-corrupt-")
        self.addCleanup(self.dir.cleanup)
        self.project = Path(self.dir.name)
        if os.environ.get("REALM_IMPL", "").lower() == "rust":
            install_companions(self.project, [NATIVE_CLI])
        self.cli = self.project / "realm"
        self.cli.write_bytes(SOURCE_BYTES)
        (self.project / "schema.sql").write_bytes(SCHEMA_BYTES)
        self.space = self.project / ".realm"
        self.db = self.space / "space.db"
        self.run_cli("init", expected=0)

    def run_cli(self, *args, expected=None, agent="alpha"):
        env = dict(os.environ, REALM_SPACE=str(self.space), REALM_AGENT=agent,
                   PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-I", "-B", "-S", str(self.cli), *args],
                           cwd=self.project, env=env, capture_output=True, text=True,
                           timeout=30)
        if expected is not None:
            self.assertEqual(r.returncode, expected,
                             f"{args}: {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def corrupt(self):
        data = self.db.read_bytes()
        # Keep the header valid so SQLite opens the file, then destroy interior pages.
        self.db.write_bytes(data[:4096] + b"\x00" * 8192 + data[12288:])

    def test_corrupt_db_reports_instead_of_traceback(self):
        self.run_cli("add", "task", "before corruption", expected=0)
        self.corrupt()
        r = self.run_cli("state", expected=7)
        combined = r.stdout + r.stderr
        self.assertNotIn("Traceback", combined)
        self.assertIn("malformed", combined.lower())

    def test_corrupt_db_never_leaks_source_path(self):
        self.corrupt()
        r = self.run_cli("state", expected=7)
        combined = r.stdout + r.stderr
        # A traceback would print the absolute path of the CLI on this machine.
        self.assertNotIn("/realm\", line", combined)
        self.assertNotIn("File \"", combined)

    def test_corrupt_db_does_not_silently_succeed(self):
        """Exit 0 on corruption would let a script treat a dead space as healthy."""
        self.corrupt()
        r = self.run_cli("state")
        self.assertNotEqual(r.returncode, 0)

    def test_newer_schema_is_refused(self):
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA user_version=99")
        r = self.run_cli("state")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("newer", (r.stdout + r.stderr).lower())
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_newer_schema_blocks_writes(self):
        """The dangerous case: not just unreadable, but writing into an unknown schema.

        Asserted SEMANTICALLY, not by byte equality: opening a WAL database read-write
        bumps its change counter and WAL state even when the application writes nothing,
        so bytes legitimately differ. What must not happen is a node landing or an event
        being emitted.
        """
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA user_version=99")
        nodes_before = self._count("nodes")
        events_before = self._count("events")
        r = self.run_cli("add", "task", "must not land")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self._count("nodes"), nodes_before, "a node landed in a refused write")
        self.assertEqual(self._count("events"), events_before, "an event was emitted")
        # And the refusal must persist: schema stays unknown.
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 99)

    def _count(self, table):
        with sqlite3.connect(self.db) as con:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_unversioned_file_is_refused(self):
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA user_version=0")
        r = self.run_cli("state")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("predates", (r.stdout + r.stderr).lower())

    def test_older_schema_is_refused(self):
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA user_version=1")
        r = self.run_cli("state")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("older", (r.stdout + r.stderr).lower())

    def test_healthy_space_still_works(self):
        self.run_cli("add", "task", "fine", expected=0)
        r = self.run_cli("state", expected=0)
        self.assertIn("T001", r.stdout)

    def test_schema_refusal_matches_the_rust_port(self):
        """Both implementations must refuse identically.

        They diverged: Python refused a schema-99 space while Rust read it happily. A
        silent divergence between the reference and the port is the failure the port
        exists to catch, so the wording is asserted equal rather than merely 'both fail'.
        """
        rust = PROJECT / "rust" / "target" / "release" / "realm"
        if not rust.is_file():
            self.skipTest("rust port not built")
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA user_version=99")

        env = dict(os.environ, REALM_SPACE=str(self.space), REALM_AGENT="alpha",
                   PYTHONDONTWRITEBYTECODE="1")
        py = subprocess.run([sys.executable, "-I", "-B", "-S", str(self.cli), "state"],
                            cwd=self.project, env=dict(env, REALM_IMPL="py"), capture_output=True, text=True)
        rs = subprocess.run([str(rust), "state"], cwd=self.project, env=env,
                            capture_output=True, text=True)
        self.assertNotEqual(py.returncode, 0)
        self.assertNotEqual(rs.returncode, 0)
        py_msg = (py.stdout + py.stderr).strip().splitlines()[0]
        rs_msg = (rs.stdout + rs.stderr).strip().splitlines()[0]
        self.assertEqual(py_msg, rs_msg,
                         f"implementations disagree:\n  py: {py_msg}\n  rs: {rs_msg}")

    def test_missing_space_is_a_sentence(self):
        self.db.unlink()
        r = self.run_cli("state", expected=1)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("not initialised", (r.stdout + r.stderr).lower())


if __name__ == "__main__":
    unittest.main()
