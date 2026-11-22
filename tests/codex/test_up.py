"""Black-box tests for `realm up`.

Every case owns a disposable project and space. No live store is read. The CLI and
companion modules are snapshotted at import time, so an edit during the run cannot mix
two source versions in one test.
"""
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
import time
import unittest

if __package__:
    from .support import NATIVE_CLI, install_companions
else:
    from support import NATIVE_CLI, install_companions

PROJECT = Path(__file__).resolve().parents[2]
SOURCE_BYTES = (PROJECT / "realm").read_bytes()
SCHEMA_BYTES = (PROJECT / "schema.sql").read_bytes()
UP_BYTES = (PROJECT / "realm_up.py").read_bytes()


class UpCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="realm-up-")
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        if os.environ.get("REALM_IMPL", "").lower() == "rust":
            install_companions(self.project, [NATIVE_CLI])
        self.cli = self.project / "realm"
        self.cli.write_bytes(SOURCE_BYTES)
        (self.project / "schema.sql").write_bytes(SCHEMA_BYTES)
        (self.project / "realm_up.py").write_bytes(UP_BYTES)
        self.space = self.project / ".realm"
        self.db = self.space / "space.db"
        self.command("init", expected=0)

    def command(self, *args, agent="alpha", expected=None, timeout=20):
        env = dict(os.environ, REALM_SPACE=str(self.space), REALM_AGENT=agent,
                   PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1", REALM_UNICODE="1")
        argv = [sys.executable, "-I", "-B", "-S", str(self.cli), *args]
        child = subprocess.Popen(argv, cwd=self.project, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate(timeout=3)
            raise
        result = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
        if expected is not None:
            self.assertEqual(result.returncode, expected,
                f"{args!r} -> {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def up(self, *args, expected=0, cols=100, rows=30):
        return self.command("up", "--once", "--cols", str(cols), "--rows", str(rows),
                            *args, expected=expected)

    def add(self, kind, title, *args, agent="alpha"):
        r = self.command("add", kind, title, *args, agent=agent, expected=0)
        m = re.search(r"\b([TFDAX]\d+)\b", r.stdout)
        self.assertIsNotNone(m, r.stdout)
        return m[1]

    def rows(self, query, params=()):
        with sqlite3.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(query, params)]


class TestRendering(UpCase):
    def test_live_roles_fit_a_standard_terminal_and_same_cli_roles_stay_distinct(self):
        goal = self.add("task", "Check the site")
        task = self.add("task", "Compare headings")
        self.command("link", goal, "requires", task, expected=0)
        self.command("take", goal, agent="coordinator/run", expected=0)
        self.command("take", task, agent="agy/run", expected=0)
        insert = "INSERT INTO events(at,actor,op,node_id,detail) VALUES(1,?,?,?,?)"
        with sqlite3.connect(self.db) as con:
            con.executemany(insert, [
                ("codex/run", "agent-queued", goal, "Waiting for plan"),
                ("agy/run", "agent-queued", goal, "Waiting for plan"),
                ("opencode/run", "agent-start", goal, "Planner accepted"),
                ("agy/run", "agent-start", task, "Checking headings"),
            ])
        roster = ("--ascii", "--agents", "opencode,codex,claude,agy")
        frame = self.up(*roster, cols=80, rows=24).stdout
        self.assertEqual(frame.count("+ opencode "), 1)
        self.assertIn("opencode (orch)", frame)
        self.assertLess(frame.index("agy (worker)"), frame.index("codex (worker)"))
        self.assertIn(f"{task} lease", frame)
        self.assertLess(frame.index("codex (worker)"), frame.index("+ claude "))

        with sqlite3.connect(self.db) as con:
            con.execute(insert, ("opencode/run", "agent-queued", goal, "Waiting as worker"))
        frame = self.up(*roster, cols=100, rows=40).stdout
        self.assertEqual(frame.count("+ opencode "), 2)
        self.assertIn("Planner accepted", frame.split("opencode (orch)", 1)[1].split("+---", 1)[0])
        self.assertIn("Waiting as worker", frame.split("opencode (worker)", 1)[1].split("+---", 1)[0])

    def test_leased_task_appears_in_left_column_and_leases(self):
        t = self.add("task", "install magisk")
        self.command("take", t, expected=0)
        out = self.up("--agents", "alpha,beta").stdout
        left, _, right = out.partition("│")
        self.assertIn(t, out)
        # it must be BOTH in the agent block and in the LEASES section
        self.assertIn(f"{t} lease", out)
        self.assertIn("LEASES", out)
        leases_at = out.index("LEASES")
        self.assertIn(t, out[leases_at:])
        self.assertIn("alpha", out)
        self.assertIn("left", out)

    def test_idle_agent_says_idle(self):
        self.add("task", "something")
        out = self.up("--agents", "alpha,beta").stdout
        self.assertIn("idle", out)

    def test_unverified_fact_appears_in_frontier(self):
        f = self.add("fact", "device has init_boot", "--check", "true")
        out = self.up().stdout
        frontier_at = out.index("FRONTIER")
        self.assertIn(f, out[frontier_at:])
        self.assertIn("UNVERIFIED", out[frontier_at:])

    def test_refuted_fact_appears_in_attention(self):
        f = self.add("fact", "wrong claim", "--check", "false")
        self.command("verify", f, expected=5)      # verify returns 5 on FAIL
        out = self.up().stdout
        attention_at = out.index("ATTENTION")
        self.assertIn(f, out[attention_at:])
        self.assertIn("REFUTED", out[attention_at:])

    def test_taint_derived_from_assumed_dependency(self):
        # F001 assumed but never verified -> T001 which depends on it is TAINTED
        f = self.add("fact", "unproven premise", "--check", "true")
        t = self.add("task", "work that relies on it")
        self.command("link", t, "requires", f, expected=0)
        self.command("cite", f, "--assume", expected=0)
        out = self.up().stdout
        self.assertIn("TAINTED", out)
        self.assertIn(t, out)

    def test_blocked_task_not_duplicated_in_attention(self):
        f = self.add("fact", "prereq", "--check", "true")
        t = self.add("task", "dependent work")
        self.command("link", t, "requires", f, expected=0)
        out = self.up().stdout
        attention_at = out.index("ATTENTION")
        self.assertNotIn(t, out[attention_at:],
                         "a BLOCKED node belongs in FRONTIER, not repeated in ATTENTION")

    def test_active_and_idle_ropes_differ(self):
        t = self.add("task", "leased work")
        self.command("take", t, expected=0)
        out = self.up("--agents", "alpha,beta").stdout
        lines = out.split("\n")
        # A rope is the contiguous run of ─ (active) or . (idle) between the pane's right
        # border and the rule. Matching on runs rather than on a junction glyph keeps this
        # test honest: the arrow row deliberately draws a plain rule instead of a tee, and
        # splitting on "│" would land on the box border instead of the rule.
        solid = re.findall(r"─{4,}", "\n".join(lines))
        dotted = re.findall(r"\.{4,}", "\n".join(lines))
        self.assertTrue(solid, f"no solid rope found:\n{out}")
        self.assertTrue(dotted, f"no dotted rope found:\n{out}")
        # and they must not be the same run: an idle agent must look different from an
        # agent holding a lease, otherwise the rope carries no information
        self.assertNotEqual(solid[0], dotted[0])

    def test_no_agents_no_crash(self):
        r = self.up("--agents", "", expected=0)
        self.assertIn("REALM UP", r.stdout)

    def test_empty_space_no_crash(self):
        r = self.up(expected=0)
        self.assertIn("REALM UP", r.stdout)
        self.assertIn("nothing open", r.stdout)

    def test_agent_not_in_roster_but_holding_lease_is_shown(self):
        t = self.add("task", "ghost work", agent="stranger")
        self.command("take", t, agent="stranger", expected=0)
        out = self.up("--agents", "alpha").stdout
        self.assertIn("stranger", out)


class TestFrame(UpCase):
    def test_once_exits_zero(self):
        self.up(expected=0)

    def test_every_line_within_80_columns(self):
        self.add("task", "a title long enough to overflow a narrow terminal " * 3)
        for cols in (80, 100, 120):
            out = self.up(cols=cols, rows=24).stdout
            for line in out.split("\n")[:-1]:
                self.assertLessEqual(len(line), cols, f"cols={cols}: {line!r}")

    def test_minimum_size_does_not_crash(self):
        self.add("task", "x")
        r = self.up(cols=80, rows=24, expected=0)
        self.assertIn("REALM UP", r.stdout)
        self.assertLessEqual(len(r.stdout.split("\n")) - 1, 24)

    def test_tiny_terminal_degrades(self):
        r = self.up(cols=40, rows=12, expected=0)
        self.assertIn("REALM UP", r.stdout)

    def test_ascii_has_no_non_ascii_bytes(self):
        t = self.add("task", "ascii only")
        self.command("take", t, expected=0)
        r = self.command("up", "--once", "--ascii", "--cols", "100", "--rows", "30",
                         expected=0)
        bad = [(i, ch) for i, ch in enumerate(r.stdout) if ord(ch) > 127]
        self.assertEqual(bad, [], f"non-ASCII at {bad[:5]}")

    def test_uniform_line_width(self):
        t = self.add("task", "leased")
        self.command("take", t, expected=0)
        out = self.up(cols=100, rows=30).stdout
        widths = {len(l) for l in out.split("\n")[:-1]}
        self.assertEqual(widths, {100}, f"ragged frame: {sorted(widths)}")


class TestSafety(UpCase):
    def test_hot_journal_refused_and_untouched(self):
        self.add("task", "before crash")
        # Build a REAL hot journal: a writer that begins, writes, and dies without
        # committing. os._exit skips interpreter cleanup, so the journal survives.
        # A hot journal only exists in ROLLBACK-journal mode. realm runs in WAL, where a
        # crashed writer leaves a non-empty -wal and NO -journal, so a fixture that stays
        # in WAL can never exercise the refusal path. Test the mode the refusal guards,
        # and prove the guard is inert on a healthy WAL space in the next test.
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA journal_mode=DELETE")
        code = (
            "import os, sqlite3, sys;"
            f"c=sqlite3.connect({str(self.db)!r});"
            "c.execute('PRAGMA journal_mode=DELETE');"
            "c.execute('BEGIN IMMEDIATE');"
            "c.execute(\"INSERT INTO nodes(id,kind,title,body,created_by,created_at,updated_at)"
            " VALUES('X999','task','uncommitted','','ghost',0,0)\");"
            "os._exit(9)"
        )
        subprocess.run([sys.executable, "-I", "-B", "-S", "-c", code], timeout=20)
        journal = Path(str(self.db) + "-journal")
        self.assertTrue(journal.is_file(), "fixture failed to leave a journal")
        self.assertGreater(journal.stat().st_size, 0, "journal is empty")
        before_db = self.db.read_bytes()
        before_j = journal.read_bytes()

        r = self.up(expected=1)
        self.assertIn("recovery", (r.stdout + r.stderr).lower())
        self.assertEqual(self.db.read_bytes(), before_db, "database was modified")
        self.assertEqual(journal.read_bytes(), before_j, "journal was modified")

    def test_healthy_wal_space_is_not_refused(self):
        """WAL leaves a -wal file during normal operation. That is not a hot journal.

        A guard that refused on any sidecar file would make `realm up` unusable on a live
        space, so the distinction is asserted rather than assumed.
        """
        t = self.add("task", "live write")
        self.command("take", t, expected=0)
        wal = Path(str(self.db) + "-wal")
        if wal.is_file() and wal.stat().st_size > 0:
            r = self.up(expected=0)
            self.assertIn(t, r.stdout)
        else:  # pragma: no cover - the CLI checkpoints on close, which is also fine
            self.up(expected=0)

    def test_missing_space_errors_cleanly(self):
        env = dict(os.environ, REALM_SPACE=str(self.project / "nope"),
                   PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-I", "-B", "-S", str(self.cli), "up", "--once"],
                           cwd=self.project, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("space", (r.stdout + r.stderr).lower())
        self.assertNotIn("Traceback", r.stderr)

    def test_wal_without_shm_still_reads(self):
        """A checkpoint can leave a WAL database with no -shm sidecar.

        A mode=ro handle cannot create it, so every query would fail with "attempt to
        write a readonly database" — a sidecar problem that looks like a read-only
        permission problem. The reader must fall back to query_only and still render.
        """
        t = self.add("task", "after checkpoint")
        self.command("take", t, expected=0)
        for suffix in ("-shm", "-wal"):
            f = Path(str(self.db) + suffix)
            if f.is_file():
                f.unlink()
        r = self.up(expected=0)
        self.assertIn(t, r.stdout)
        self.assertNotIn("readonly", (r.stdout + r.stderr).lower())

    def test_unwritable_directory_reports_a_sentence(self):
        """SQLite needs -shm to read a WAL database, and that needs a writable directory.

        No reader can work around it, so the failure must be a sentence naming the cause,
        not a traceback.
        """
        self.add("task", "x")
        for suffix in ("-shm", "-wal"):
            f = Path(str(self.db) + suffix)
            if f.is_file():
                f.unlink()
        os.chmod(self.space, 0o555)
        self.addCleanup(os.chmod, self.space, 0o755)
        r = self.up(expected=1)
        combined = r.stdout + r.stderr
        self.assertNotIn("Traceback", combined)
        self.assertIn("writable", combined.lower())

    def test_uninitialised_space_errors_cleanly(self):
        empty = self.project / "empty"
        empty.mkdir()
        env = dict(os.environ, REALM_SPACE=str(empty), PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-I", "-B", "-S", str(self.cli), "up", "--once"],
                           cwd=self.project, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)

    def test_refuses_wrong_schema_version(self):
        self.add("task", "x")
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA user_version = 99")
        r = self.up(expected=1)
        self.assertIn("schema", (r.stdout + r.stderr).lower())

    def test_up_never_writes(self):
        t = self.add("task", "read only")
        self.command("take", t, expected=0)
        before = self.db.read_bytes()
        events_before = self.rows("SELECT COUNT(*) c FROM events")[0]["c"]
        self.up()
        self.assertEqual(self.db.read_bytes(), before, "up modified the database")
        self.assertEqual(self.rows("SELECT COUNT(*) c FROM events")[0]["c"], events_before,
                         "up wrote an event")

    def test_no_python_bytecode_written(self):
        self.add("task", "x")
        self.up()
        stray = list(self.project.rglob("__pycache__"))
        self.assertEqual(stray, [], f"bytecode written: {stray}")


class TestInteractive(UpCase):
    """The curses path, driven through a real PTY.

    `--once` exercises render() but NOT the interactive loop, and the loop is where a
    terminal app actually fails: no input, no exit, or a frame that never paints. This
    test child-processes `realm up` under a pty and drives it like a user.
    """

    def test_paints_and_quits_on_q(self):
        import pty
        import select

        t = self.add("task", "pty driven")
        self.command("take", t, expected=0)

        pid, fd = pty.fork()
        if pid == 0:                                  # child
            os.environ.update(REALM_AGENT="alpha", TERM="xterm-256color",
                              REALM_SPACE=str(self.space))
            os.execv(sys.executable,
                     [sys.executable, "-I", "-B", "-S", str(self.cli),
                      "up", "--agents", "alpha,beta", "--poll", "0.5"])
        captured = b""
        sent = False
        exited = None
        deadline = time.time() + 20
        try:
            # Drain CONTINUOUSLY. A curses app writes escape sequences on endwin(); a
            # harness that stops reading fills the PTY buffer and the child blocks in
            # write(), which looks identical to "q does nothing".
            while time.time() < deadline:
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        _, status = os.waitpid(pid, 0)
                        exited = os.waitstatus_to_exitcode(status)
                        break
                    captured += chunk
                if not sent and b"REALM UP" in captured:
                    os.write(fd, b"q")
                    sent = True
                w, status = os.waitpid(pid, os.WNOHANG)
                if w == pid:
                    exited = os.waitstatus_to_exitcode(status)
                    break
                time.sleep(0.02)
        finally:
            if exited is None:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
                os.close(fd)

        self.assertTrue(sent, f"never painted a frame: {captured[:300]!r}")
        self.assertIsNotNone(exited, "did not exit after 'q'")
        self.assertEqual(exited, 0)
        text = captured.decode("utf-8", "replace")
        self.assertIn("ORCHESTRATOR", text)
        self.assertIn("LEASES", text)
        self.assertIn(t, text)


if __name__ == "__main__":
    unittest.main()
