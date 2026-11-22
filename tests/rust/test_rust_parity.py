"""Parity tests for the Rust implementation.

The canonical 24-case suite invokes the CLI as `python3 realm ...`, so it can only test the
Python implementation. This suite drives the Rust BINARY directly and asserts the same
contracts, plus byte-for-byte schema identity.

    python3 -I -B -S -m unittest discover -s tests/rust -v

A Rust build is required:  cd rust && cargo build --release
Tests skip cleanly if the binary is absent, so this never blocks the canonical suite.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
RUST = PROJECT / "rust" / "target" / "release" / "realm"
SCHEMA = PROJECT / "schema.sql"


@unittest.skipUnless(RUST.is_file(), "rust binary not built (cd rust && cargo build --release)")
class RustParityCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="realm-rust-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "present.txt").write_text("ok\n")
        (self.dir / "schema.sql").write_bytes(SCHEMA.read_bytes())
        self.env = dict(os.environ, REALM_SPACE=str(self.dir / ".realm"),
                        REALM_AGENT="alpha")
        self.realm("init")

    def realm(self, *args, agent="alpha", expect=0, timeout=20):
        env = dict(self.env, REALM_AGENT=agent)
        r = subprocess.run([str(RUST), *args], cwd=str(self.dir), env=env,
                           capture_output=True, text=True, timeout=timeout)
        if expect is not None:
            self.assertEqual(
                r.returncode, expect,
                f"{args!r} -> {r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
        return r

    def db(self):
        return sqlite3.connect(str(self.dir / ".realm" / "space.db"))

    # ------------------------------------------------------------------ init

    def test_init_creates_the_same_schema(self):
        """Byte-identity of the resulting schema is the parity contract."""
        con = self.db()
        rust_sql = sorted(
            r[0] for r in con.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%'"))
        con.close()

        py_dir = Path(tempfile.mkdtemp(prefix="realm-py-"))
        self.addCleanup(shutil.rmtree, py_dir, True)
        (py_dir / "schema.sql").write_bytes(SCHEMA.read_bytes())
        # CONTROL HONESTY: this reference half must run the PYTHON implementation.
        # If the suite is launched with REALM_IMPL=rust, delegating here would compare
        # Rust against Rust and prove nothing. Force it, and assert it took effect.
        env = dict(os.environ, REALM_SPACE=str(py_dir / ".realm"), REALM_AGENT="alpha",
                   REALM_IMPL="py")
        env.pop("REALM_SCHEMA", None)
        ref = subprocess.run([sys.executable, "-B", str(PROJECT / "realm"), "init"],
                             cwd=str(py_dir), env=env, capture_output=True, text=True)
        self.assertEqual(ref.returncode, 0, ref.stderr)
        # If the reference silently delegated to Rust we would be comparing Rust to Rust.
        self.assertNotIn("REALM_IMPL=rust", ref.stderr)
        con2 = sqlite3.connect(str(py_dir / ".realm" / "space.db"))
        py_sql = sorted(
            r[0] for r in con2.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%'"))
        con2.close()
        self.assertEqual(rust_sql, py_sql, "schema created by Rust differs from Python")

    # ------------------------------------------------------------------ gates

    def test_gate2_refuses_unverified(self):
        self.realm("add", "fact", "claim", "--check", "test -f present.txt")
        self.realm("cite", "F001", expect=2)

    def test_gate2_refuses_refuted(self):
        self.realm("add", "fact", "claim", "--check", "test -f absent.txt")
        self.realm("verify", "F001", expect=5)
        self.realm("cite", "F001", expect=2)

    def test_gate2_allows_verified(self):
        self.realm("add", "fact", "claim", "--check", "test -f present.txt && echo ok")
        self.realm("verify", "F001")
        self.realm("cite", "F001")

    def test_gate1_refuses_on_refuted_dependency(self):
        self.realm("add", "task", "work")
        self.realm("add", "fact", "bad", "--check", "test -f absent.txt")
        self.realm("verify", "F001", expect=5)
        self.realm("link", "T001", "requires", "F001")
        self.realm("take", "T001")
        self.realm("done", "T001", expect=2)

    def test_gate1_allows_on_verified_dependency(self):
        self.realm("add", "task", "work")
        self.realm("add", "fact", "good", "--check", "test -f present.txt && echo ok")
        self.realm("verify", "F001")
        self.realm("link", "T001", "requires", "F001")
        self.realm("take", "T001")
        self.realm("done", "T001")

    def test_trivial_check_cannot_be_cited_by_others(self):
        self.realm("add", "fact", "self-serving", "--check", "true", agent="author")
        self.realm("verify", "F001", agent="author")
        self.realm("cite", "F001", agent="author")
        self.realm("cite", "F001", agent="other", expect=2)

    def test_taint_blocks_done_and_clears_on_verify(self):
        self.realm("add", "fact", "hypothesis", "--check", "test -f present.txt && echo ok")
        self.realm("add", "task", "work")
        self.realm("cite", "F001", "--assume")
        self.realm("link", "T001", "depends_on", "F001")
        self.realm("take", "T001")
        self.realm("done", "T001", expect=2)
        self.realm("verify", "F001")
        self.realm("done", "T001")

    # --------------------------------------------------------- structure

    def test_link_persists_correct_columns(self):
        self.realm("add", "task", "t")
        self.realm("add", "fact", "f", "--check", "true")
        self.realm("link", "T001", "requires", "F001")
        con = self.db()
        row = con.execute("SELECT from_id,to_id,kind FROM edges").fetchone()
        con.close()
        self.assertEqual(row, ("T001", "F001", "requires"))

    def test_negative_ttl_rejected(self):
        self.realm("add", "task", "t")
        self.realm("take", "T001", "--ttl", "-5", expect=1)

    def test_zero_ttl_is_immediate_expiry(self):
        self.realm("add", "task", "t")
        self.realm("take", "T001", "--ttl", "0")
        self.realm("done", "T001", expect=2)

    def test_concurrent_take_single_winner(self):
        self.realm("add", "task", "contended")
        procs = []
        for i in range(6):
            env = dict(self.env, REALM_AGENT=f"a{i}")
            procs.append(subprocess.Popen(
                [str(RUST), "take", "T001"], cwd=str(self.dir), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        codes = [p.wait() for p in procs]
        self.assertEqual(codes.count(0), 1, f"expected 1 winner, got {codes}")

    def test_state_and_since(self):
        self.realm("add", "task", "open work")
        out = self.realm("state").stdout
        self.assertIn("FRONTIER", out)
        self.assertIn("T001", out)
        cur = int([w for w in out.split() if w.startswith("seq=")][0].split("=")[1])
        self.assertIn("no events since", self.realm("since", str(cur)).stdout)
        self.realm("add", "task", "second")
        self.assertIn("T002", self.realm("since", str(cur)).stdout)

    def test_run_records_attempt_and_preserves_argv(self):
        self.realm("add", "task", "t")
        r = self.realm("run", "--attach", "T001", "--", "echo", "hello")
        self.assertIn("hello", r.stdout)
        con = self.db()
        argv, code = con.execute("SELECT argv_json,exit_code FROM attempts").fetchone()
        con.close()
        self.assertEqual(json.loads(argv), ["echo", "hello"])
        self.assertEqual(code, 0)

    def test_missing_executable_is_controlled(self):
        self.realm("add", "task", "t")
        r = self.realm("run", "--attach", "T001", "--", "/nonexistent/cmd", expect=127)
        self.assertNotIn("panicked", r.stderr.lower())

    def test_unknown_attach_rejected_before_side_effect(self):
        r = self.realm("run", "--attach", "NOPE", "--", "echo", "hi", expect=3)
        self.assertNotIn("hi", r.stdout, "command ran despite invalid attachment")


if __name__ == "__main__":
    unittest.main()
