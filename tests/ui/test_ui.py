"""Bounded checks for realm ui. Read-only rendering; no curses, no live DB.

These run `realm ui --once` against a temp space and assert on the rendered text.
They belong to the UI owner (OpenCode), not the canonical CLI suite.

    python3 -I -B -S -m unittest discover -s tests/ui -v
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
REALM = PROJECT / "realm"


class UiCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="realm-ui-"))
        (self.dir / "present.txt").write_text("ok\n")
        self.env = dict(os.environ, REALM_SPACE=str(self.dir / ".realm"), REALM_AGENT="alpha")
        self.realm("init")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def realm(self, *args, agent="alpha", expect=0, ui=False):
        env = dict(self.env, REALM_AGENT=agent)
        cmd = [sys.executable, "-B", str(REALM)]
        if ui:
            cmd += ["ui"]
        cmd += list(args)
        r = subprocess.run(cmd, cwd=str(self.dir), env=env, capture_output=True, text=True)
        if expect is not None:
            self.assertEqual(r.returncode, expect,
                             f"{args} -> {r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
        return r

    def ui(self, *args, expect=0):
        return self.realm(*args, ui=True, expect=expect).stdout

    # ---------------------------------------------------------------- tests

    def test_once_renders_and_exits_zero(self):
        out = self.ui("--once", "--ascii", "--cols", "100", "--rows", "24")
        self.assertIn("REALM", out)
        self.assertIn("AGENTS", out)
        self.assertIn("EVIDENCE", out)

    def test_task_and_fact_appear_in_tree(self):
        t = self.realm("add", "task", "install magisk").stdout.strip()
        f = self.realm("add", "fact", "kernel supports init_boot",
                     "--check", "test -f present.txt && echo ok").stdout.strip()
        self.realm("link", t, "requires", f)
        out = self.ui("--once", "--ascii")
        self.assertIn(t, out)
        self.assertIn(f, out)

    def test_dependency_nests_dependent_under_prerequisite(self):
        """The tree must show a dependent BELOW its prerequisite, so the reader sees
        'this fact, and what relies on it' rather than a flat list."""
        f = self.realm("add", "fact", "base", "--check", "test -f present.txt && echo ok").stdout.strip()
        t = self.realm("add", "task", "derived work").stdout.strip()
        self.realm("link", t, "requires", f)
        out = self.ui("--once", "--ascii", "--cols", "100")
        lines = out.splitlines()
        fi = next(i for i, l in enumerate(lines) if f in l)
        ti = next(i for i, l in enumerate(lines) if t in l)
        self.assertGreater(ti, fi, f"dependent {t} must render after prerequisite {f}")
        self.assertRegex(lines[ti], r"[\\|-]{1,2}-\s", "dependent should show an ASCII tree connector")

    def test_blocked_marker_when_prerequisite_unverified(self):
        t = self.realm("add", "task", "needs a fact").stdout.strip()
        f = self.realm("add", "fact", "unproven", "--check", "test -f absent.txt").stdout.strip()
        self.realm("link", t, "requires", f)
        self.realm("verify", f, expect=5)
        out = self.ui("--once", "--ascii", "--cols", "120")
        row = next(l for l in out.splitlines() if t in l)
        self.assertIn("BLOCKED", row, row)

    def test_no_blocked_marker_when_verified(self):
        t = self.realm("add", "task", "ok work").stdout.strip()
        f = self.realm("add", "fact", "proven", "--check", "test -f present.txt && echo ok").stdout.strip()
        self.realm("link", t, "requires", f)
        self.realm("verify", f)
        out = self.ui("--once", "--ascii", "--cols", "120")
        row = next(l for l in out.splitlines() if t in l)
        self.assertNotIn("BLOCKED", row, row)

    def test_taint_is_derived_not_read_from_cache(self):
        """The UI is read-only and cannot call refresh_taint, so it must compute taint
        itself. Deliberately leave nodes.tainted at 0 and assert the marker still shows."""
        src = self.realm("add", "fact", "assumed source",
                       "--check", "test -f present.txt && echo ok").stdout.strip()
        t = self.realm("add", "task", "relies on it").stdout.strip()
        self.realm("link", t, "depends_on", src)
        self.realm("cite", src, "--assume")
        # force the cached column to lie
        import sqlite3
        con = sqlite3.connect(str(self.dir / ".realm" / "space.db"))
        con.execute("UPDATE nodes SET tainted=0")
        con.commit()
        con.close()
        out = self.ui("--once", "--ascii", "--cols", "120")
        row = next(l for l in out.splitlines() if t in l)
        self.assertIn("TAINTED", row, row)

    def test_leased_marker_and_roster(self):
        t = self.realm("add", "task", "leased work").stdout.strip()
        self.realm("take", t, agent="beta")
        out = self.ui("--once", "--ascii", "--cols", "120")
        # match the TREE row, not the header line (which also mentions the lease)
        row = next(l for l in out.splitlines() if t in l and "task" in l)
        self.assertIn("LEASED", row, row)
        self.assertIn("beta", out)

    def test_refuted_is_labelled_in_words(self):
        f = self.realm("add", "fact", "bad claim", "--check", "test -f absent.txt").stdout.strip()
        self.realm("verify", f, expect=5)
        out = self.ui("--once", "--ascii", "--cols", "120")
        row = next(l for l in out.splitlines() if f in l)
        self.assertIn("REFUTED", row, "status must be spelled out, not glyph-only")

    def test_ascii_mode_has_no_unicode_glyphs(self):
        self.realm("add", "task", "x")
        out = self.ui("--once", "--ascii", "--cols", "100")
        self.assertNotIn("✓", out)
        self.assertNotIn("✗", out)
        self.assertNotIn("├", out)

    def test_unicode_mode_uses_glyphs(self):
        """NO_COLOR is set in this environment, so Unicode must be requested explicitly."""
        f = self.realm("add", "fact", "good",
                       "--check", "test -f present.txt && echo ok").stdout.strip()
        self.realm("verify", f)
        env = dict(self.env, REALM_UNICODE="1", REALM_AGENT="alpha")
        env.pop("NO_COLOR", None)
        env["REALM_SPACE"] = str(self.dir / ".realm")
        r = subprocess.run([sys.executable, "-B", str(REALM), "ui", "--once", "--cols", "100"],
                           cwd=str(self.dir), env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("✓", r.stdout, r.stdout)

    def test_output_respects_requested_width(self):
        for _ in range(12):
            self.realm("add", "task", "a fairly long task title that will need trimming " * 2)
        out = self.ui("--once", "--ascii", "--cols", "90", "--rows", "20")
        for line in out.splitlines():
            self.assertLessEqual(len(line), 92, f"line exceeds width: {line!r}")

    def test_truncation_is_never_silent(self):
        for i in range(30):
            self.realm("add", "task", f"task {i}")
        out = self.ui("--once", "--ascii", "--cols", "100", "--rows", "20")
        self.assertIn("more", out, "overflow must announce itself")

    def test_ui_never_writes_to_the_space(self):
        """A read-only lens. Snapshot the db bytes, render, compare."""
        t = self.realm("add", "task", "x").stdout.strip()
        self.realm("add", "fact", "y", "--check", "test -f present.txt && echo ok")
        db = self.dir / ".realm" / "space.db"
        before = (db.stat().st_size, db.stat().st_mtime_ns)
        self.ui("--once", "--ascii")
        after = (db.stat().st_size, db.stat().st_mtime_ns)
        self.assertEqual(before, after, "ui --once must not modify the database")

    def test_empty_space_renders_without_crash(self):
        out = self.ui("--once", "--ascii", "--cols", "80", "--rows", "20")
        self.assertIn("REALM", out)
        self.assertIn("0 nodes", out)

    def test_since_view_reports_changes(self):
        self.realm("add", "task", "first")
        self.realm("add", "task", "second")
        out = self.ui("--once", "--ascii", "--since")
        self.assertTrue("since you opened" in out or "changed" in out, out)


if __name__ == "__main__":
    unittest.main()
