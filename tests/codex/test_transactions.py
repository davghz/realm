"""Bounded process, verification, and atomic-mutation regression tests."""

import os
import shlex
import signal
import sqlite3
import subprocess
import sys

if __package__:
    from .support import RealmCase
else:
    from support import RealmCase


class TransactionTests(RealmCase):
    def start_realm(self, *arguments):
        env = dict(
            os.environ,
            REALM_SPACE=str(self.space),
            REALM_SCHEMA=str(self.project / "schema.sql"),
            REALM_AGENT="alpha",
            PYTHONDONTWRITEBYTECODE="1",
        )
        return subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", str(self.cli), *arguments],
            cwd=self.project,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def stop_realm_group(self, process):
        # start_realm creates a private session/process group. Kill that group
        # even if the direct process exited, since a blocked check may survive it.
        # Never target the test runner's group or search for unrelated processes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=3)
        finally:
            process.stdout.close()
            process.stderr.close()

    def test_large_check_output_completes_without_pipe_deadlock(self):
        check = shlex.join([
            sys.executable, "-I", "-B", "-S", "-c",
            "import sys; sys.stdout.write('x' * 1048576); sys.stderr.write('y' * 1048576)",
        ])
        fact = self.add("fact", "a successful check with large output", "--check", check)
        process = self.start_realm("verify", fact)
        try:
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.fail("a successful check writing 1 MiB to each pipe did not finish within 5 seconds")
            self.assertEqual(process.returncode, 0, f"stdout={stdout}\nstderr={stderr}")
            self.assertEqual(self.node(fact)["status"], "VERIFIED")
        finally:
            self.stop_realm_group(process)

    def test_unknown_assumption_target_leaves_no_partial_mutation(self):
        fact = self.add("fact", "an assumption with an invalid dependent")
        before_fact = self.rows("SELECT * FROM facts WHERE node_id=?", (fact,))
        before_edges = self.rows("SELECT * FROM edges ORDER BY from_id,to_id,kind")
        before_events = self.rows("SELECT * FROM events ORDER BY cursor")

        result = self.command("cite", fact, "--assume", "--for", "NO_SUCH_NODE")

        self.assertNotEqual(result.returncode, 0, "a missing --for node must reject the citation")
        self.assertEqual(self.rows("SELECT * FROM facts WHERE node_id=?", (fact,)), before_fact)
        self.assertEqual(self.rows("SELECT * FROM edges ORDER BY from_id,to_id,kind"), before_edges)
        self.assertEqual(self.rows("SELECT * FROM events ORDER BY cursor"), before_events)

    def test_event_insert_failure_rolls_back_the_node_mutation(self):
        before_nodes = self.rows("SELECT * FROM nodes ORDER BY id")
        before_tasks = self.rows("SELECT * FROM tasks ORDER BY node_id")
        before_events = self.rows("SELECT * FROM events ORDER BY cursor")
        # Fault injection is confined to this test's disposable DB. A mutation
        # and its poll-watermark event must either both commit or both roll back.
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TRIGGER codex_reject_add_event BEFORE INSERT ON events "
                "WHEN NEW.op='add' BEGIN "
                "SELECT RAISE(ABORT, 'codex forced event failure'); END"
            )

        result = self.command("add", "task", "must not commit without its event")

        self.assertNotEqual(result.returncode, 0, "an event SQL failure must not be discarded")
        self.assertEqual(self.rows("SELECT * FROM nodes ORDER BY id"), before_nodes)
        self.assertEqual(self.rows("SELECT * FROM tasks ORDER BY node_id"), before_tasks)
        self.assertEqual(self.rows("SELECT * FROM events ORDER BY cursor"), before_events)

    def test_writer_contention_waits_then_acquires_the_lease(self):
        task = self.add("task", "a briefly contended lease")
        writer = sqlite3.connect(self.db, isolation_level=None)
        process = None
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE tasks SET priority=priority+1 WHERE node_id=?", (task,))
            process = self.start_realm("take", task)
            try:
                with self.assertRaises(
                    subprocess.TimeoutExpired,
                    msg="take exited while a short-lived writer still held the DB lock",
                ):
                    process.communicate(timeout=0.25)
            finally:
                writer.commit()

            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, f"stdout={stdout}\nstderr={stderr}")
            self.assertEqual(self.lease(task)["owner"], "alpha")
        finally:
            writer.close()
            if process is not None:
                self.stop_realm_group(process)

    def test_repeated_verify_reuses_the_successful_check_result(self):
        counter = self.project / "check-counter.txt"
        check = shlex.join([
            sys.executable, "-I", "-B", "-S", "-c",
            "from pathlib import Path; p=Path('check-counter.txt'); "
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
            "print('counter check passed')",
        ])
        fact = self.add("fact", "a reusable successful check", "--check", check)

        self.command("verify", fact, expected=0)
        self.command("verify", fact, expected=0)

        self.assertEqual(counter.read_text(), "1", "an unchanged verified check executed again")
        cached = self.rows("SELECT * FROM verify_cache WHERE fact_id=?", (fact,))
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["result"], "PASS")
        self.assertEqual(cached[0]["attempt_count"], 1)
        events = self.rows("SELECT * FROM events WHERE op='verify' AND node_id=?", (fact,))
        self.assertEqual(len(events), 1, "a cache hit must not create a second execution event")

    def test_done_state_update_failure_rolls_back_all_rows(self):
        task = self.add("task", "completion must be atomic")
        self.command("take", task, expected=0)
        queries = {
            "tasks": "SELECT * FROM tasks ORDER BY node_id",
            "nodes": "SELECT * FROM nodes ORDER BY id",
            "leases": "SELECT * FROM leases ORDER BY resource",
            "events": "SELECT * FROM events ORDER BY cursor",
        }
        before = {table: self.rows(query) for table, query in queries.items()}
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TRIGGER codex_reject_task_completion "
                "BEFORE UPDATE OF state ON tasks WHEN NEW.state='DONE' BEGIN "
                "SELECT RAISE(ABORT, 'codex forced completion failure'); END"
            )

        result = self.command("done", task)
        after = {table: self.rows(query) for table, query in queries.items()}

        self.assertEqual(
            {"reported_success": result.returncode == 0, "rows": after},
            {"reported_success": False, "rows": before},
            "a failed completion update must retain the task, node, lease, and event rows; "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}",
        )
