"""Observable task ownership and completion contracts through the Realm CLI."""
if __package__:
    from .support import RealmCase
else:
    from support import RealmCase
import os
import subprocess
import sys


class LeaseAndCompletionTests(RealmCase):
    def test_unleased_task_cannot_be_completed(self):
        task = self.add("task", "Needs a worker")
        self.command("done", task, expected=4)
        self.assertEqual(self.node(task)["state"], "OPEN")

    def test_live_lease_has_one_owner_and_conflict_does_not_transfer_it(self):
        task = self.add("task", "Exclusive work")
        self.command("take", task, agent="alpha", expected=0)
        before = self.rows("SELECT * FROM leases WHERE resource=?", (task,))[0]
        self.command("take", task, agent="bravo", expected=4)
        after = self.rows("SELECT * FROM leases WHERE resource=?", (task,))[0]
        self.assertEqual(before, after)
        self.command("done", task, agent="bravo", expected=4)
        self.assertEqual(self.node(task)["state"], "OPEN")

    def test_expired_lease_cannot_complete_and_reclaim_increments_token(self):
        task = self.add("task", "Expired work")
        self.command("take", task, agent="alpha", expected=0)
        first = self.lease(task)["fencing_token"]
        self.expire(task)
        self.command("done", task, agent="alpha", expected=2)
        self.command("take", task, agent="bravo", expected=0)
        second = self.lease(task)
        self.assertGreater(second["fencing_token"], first)
        self.assertEqual(second["owner"], "bravo")
        self.command("done", task, agent="alpha", expected=4)
        self.command("done", task, agent="bravo", expected=0)
        self.assertEqual(self.node(task)["state"], "DONE")

    def test_verified_dependency_allows_completion_and_removes_lease(self):
        task = self.add("task", "Evidence-backed completion")
        fact = self.add("fact", "The file has expected contents", "--check", self.file_check())
        self.command("link", task, "depends_on", fact, expected=0)
        self.command("take", task, expected=0)
        self.command("done", task, expected=2)
        self.assertEqual(self.node(task)["state"], "OPEN")
        self.command("verify", fact, expected=0)
        self.command("done", task, expected=0)
        self.assertEqual(self.node(task)["state"], "DONE")
        lease = self.rows("SELECT * FROM leases WHERE resource=?", (task,))
        self.assertTrue(not lease or lease[0]["owner"] is None, lease)
        self.command("take", task, expected=2)

    def test_non_dependency_fact_edge_does_not_block_completion(self):
        task = self.add("task", "Task with an unrelated disputed claim")
        prerequisite = self.add("fact", "Required evidence is true", "--check", self.file_check())
        contradicted = self.add("fact", "Claim this task contradicts", "--check", self.file_check("disputed.txt"))
        (self.project / "disputed.txt").write_text("refuted")
        self.command("verify", prerequisite, expected=0)
        self.command("verify", contradicted, expected=5)
        self.assertEqual(self.node(prerequisite)["status"], "VERIFIED")
        self.assertEqual(self.node(contradicted)["status"], "REFUTED")
        self.command("link", task, "depends_on", prerequisite, expected=0)
        self.command("link", task, "contradicts", contradicted, expected=0)
        self.command("take", task, expected=0)
        self.command("done", task, expected=0)
        self.assertEqual(self.node(task)["state"], "DONE")

    def test_negative_lease_duration_is_rejected_without_leasing(self):
        task = self.add("task", "Invalid duration")
        self.command("take", task, "--ttl", "-1", expected=1)
        self.assertEqual(self.node(task)["state"], "OPEN")
        self.assertEqual(self.rows("SELECT * FROM leases WHERE resource=?", (task,)), [])

    def test_zero_ttl_is_valid_but_does_not_grant_a_live_lease(self):
        task = self.add("task", "Immediately expired work")
        self.command("take", task, "--ttl", "0", expected=0)
        self.command("done", task, expected=2)
        self.assertEqual(self.node(task)["state"], "OPEN")

    def test_concurrent_take_has_exactly_one_winner(self):
        task = self.add("task", "Six workers compete")
        processes = []
        try:
            for i in range(6):
                env = dict(os.environ, REALM_SPACE=str(self.space), REALM_AGENT=f"worker-{i}",
                           PYTHONDONTWRITEBYTECODE="1")
                processes.append(subprocess.Popen(
                    [sys.executable, "-I", "-B", "-S", str(self.cli), "take", task],
                    cwd=self.project, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            results = [(p.returncode, out, err) for p in processes
                       for out, err in [p.communicate(timeout=15)]]
            self.assertEqual(sum(code == 0 for code, _, _ in results), 1, results)
            self.assertTrue(all(code in (0, 4) for code, _, _ in results), results)
            self.assertTrue(self.lease(task)["owner"].startswith("worker-"))
        finally:
            for p in processes:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
