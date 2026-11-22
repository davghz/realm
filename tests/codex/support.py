"""Black-box Realm fixtures. Each case owns a disposable project and SQLite DB."""
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[2]
# Capture once per test process; concurrent implementation edits cannot mix source
# versions within one run. No live .realm store is read, created, or changed.
SOURCE_BYTES = (PROJECT / "realm").read_bytes()
SCHEMA_BYTES = (PROJECT / "schema.sql").read_bytes()
# The launcher needs its Python reference and, when built, the native executable.
# Snapshot these too: a fixture must neither miss a sibling nor dispatch back to
# a mutable implementation in the checkout. UI is a sibling of the reference.
NATIVE_CLI = "rust/target/release/realm" if (PROJECT / "rust/target/release/realm").is_file() else "realm-rs"
COMPANION_BYTES = {
    name: (PROJECT / name).read_bytes()
    for name in ("realm-py", "realm_ui.py", "realm_dispatch.py", "realm_workers.py",
                 NATIVE_CLI)
    if (PROJECT / name).is_file()
}


def install_companions(project, names):
    for name in names:
        if name not in COMPANION_BYTES:
            continue
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(COMPANION_BYTES[name])
        if name == NATIVE_CLI:
            target.chmod(0o755)


class RealmCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="realm-codex-")
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        self.cli = self.project / "realm"
        self.cli.write_bytes(SOURCE_BYTES)
        (self.project / "schema.sql").write_bytes(SCHEMA_BYTES)
        install_companions(self.project, COMPANION_BYTES)
        self.space = self.project / ".realm"
        self.db = self.space / "space.db"
        self.command("init", expected=0)

    def command(self, *args, agent="alpha", expected=None, timeout=10):
        env = dict(os.environ, REALM_SPACE=str(self.space), REALM_AGENT=agent,
                   PYTHONDONTWRITEBYTECODE="1")
        argv = [sys.executable, "-I", "-B", "-S", str(self.cli), *args]
        child = subprocess.Popen(argv, cwd=self.project, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except BaseException:
            # The Python entrypoint can delegate to Rust, which can run a check.
            # On timeout stop this fixture's whole group, not just its wrapper.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate(timeout=3)
            raise
        result = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
        if expected is not None:
            self.assertEqual(result.returncode, expected,
                f"{args!r} returned {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def add(self, kind, title, *args, agent="alpha"):
        result = self.command("add", kind, title, *args, agent=agent, expected=0)
        match = re.search(r"\b([TFDAX]\d+)\b", result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return match[1]

    def rows(self, query, parameters=()):
        with sqlite3.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(query, parameters)]

    def node(self, ident):
        node = self.rows("SELECT * FROM nodes WHERE id=?", (ident,))[0]
        table = {"task": "tasks", "fact": "facts", "artifact": "artifacts",
                 "decision": "decisions", "attempt": "attempts"}[node["kind"]]
        specific = self.rows(f"SELECT * FROM {table} WHERE node_id=?", (ident,))
        if specific:
            node.update(specific[0])
        return node

    def lease(self, ident):
        return self.rows("SELECT * FROM leases WHERE resource=?", (ident,))[0]

    def expire(self, ident):
        # Deterministic fixture setup, without sleeps or changes to any real store.
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE leases SET expires_at=0 WHERE resource=?", (ident,))

    def file_check(self, filename="evidence.txt", expected="verified"):
        (self.project / filename).write_text(expected)
        code = ("from pathlib import Path; "
                f"assert Path({filename!r}).read_text() == {expected!r}; "
                "print('evidence content verified')")
        return shlex.join([sys.executable, "-I", "-B", "-S", "-c", code])
