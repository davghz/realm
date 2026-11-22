"""Evidence gates exercised through the CLI against disposable Realm stores."""

if __package__:
    from .support import RealmCase
else:
    from support import RealmCase


class EvidenceGateTests(RealmCase):
    def assumed_dependency(self):
        """Return a verified dependent of an explicitly assumed source fact."""
        source = self.add(
            "fact", "source evidence awaiting verification",
            "--check", self.file_check("source.txt"),
        )
        dependent = self.add(
            "fact", "derived evidence with a passing check",
            "--check", self.file_check("derived.txt"),
        )
        self.command("link", dependent, "depends_on", source, expected=0)
        self.command("verify", dependent, expected=0)
        self.assertEqual(self.node(dependent)["status"], "VERIFIED")
        self.command("cite", source, "--assume", "--for", dependent, expected=0)
        self.assertEqual(self.node(source)["status"], "UNVERIFIED")
        return source, dependent

    def test_unverified_and_uncheckable_facts_cannot_be_cited(self):
        unchecked = self.add("fact", "claim without a registered check")
        registered = self.add(
            "fact", "claim with a check that has not run",
            "--check", self.file_check(),
        )
        for fact in (unchecked, registered):
            with self.subTest(fact=fact):
                self.assertEqual(self.node(fact)["status"],
                                 "UNCHECKABLE" if fact == unchecked else "UNVERIFIED")
                self.command("cite", fact, agent="beta", expected=2)

        self.command("verify", unchecked, expected=8)
        self.assertEqual(self.node(unchecked)["status"], "UNCHECKABLE")
        self.command("cite", unchecked, agent="beta", expected=2)

    def test_changed_real_evidence_contests_a_previously_verified_fact(self):
        fact = self.add("fact", "content is verified", "--check", self.file_check())
        self.command("verify", fact, expected=0)
        self.assertEqual(self.node(fact)["status"], "VERIFIED")
        self.command("cite", fact, agent="beta", expected=0)

        (self.project / "evidence.txt").write_text("changed input")
        # Force a replay rather than accepting a cached result from the good run.
        self.command("verify", fact, "--force", agent="beta", expected=5)
        self.assertEqual(self.node(fact)["status"], "CONTESTED")
        self.assertNotEqual(self.node(fact)["check_exit"], 0)
        self.command("cite", fact, expected=2)

    def test_first_failed_check_refutes_fact_and_blocks_citation(self):
        """Port of gate2_refuses_refuted, using a real content assertion."""
        fact = self.add("fact", "claim fails on its first check", "--check", self.file_check())
        (self.project / "evidence.txt").write_text("wrong before first check")
        self.command("verify", fact, expected=5)
        self.assertEqual(self.node(fact)["status"], "REFUTED")
        self.assertNotEqual(self.node(fact)["check_exit"], 0)
        self.command("cite", fact, agent="beta", expected=2)

    def test_verified_real_evidence_can_be_cited_by_another_agent(self):
        """Port of gate2_allows_verified; a different agent may rely on it."""
        fact = self.add("fact", "content has a real passing check", "--check", self.file_check())
        self.command("verify", fact, expected=0)
        self.assertEqual(self.node(fact)["status"], "VERIFIED")
        self.assertEqual(self.node(fact)["check_strength"], "OK")
        self.command("cite", fact, agent="beta", expected=0)

    def test_refuted_dependency_blocks_task_completion(self):
        """Port of gate1_refuses_on_refuted_dependency."""
        fact = self.add("fact", "refuted prerequisite", "--check", self.file_check())
        (self.project / "evidence.txt").write_text("not verified")
        self.command("verify", fact, expected=5)
        self.assertEqual(self.node(fact)["status"], "REFUTED")
        task = self.add("task", "work cannot rely on refuted evidence")
        self.command("link", task, "depends_on", fact, expected=0)
        self.command("take", task, expected=0)

        self.command("done", task, expected=2)
        self.assertEqual(self.node(task)["state"], "OPEN")
        self.assertEqual(self.lease(task)["owner"], "alpha")

    def test_trivial_check_does_not_prove_a_fact_to_another_agent(self):
        fact = self.add("fact", "unconditional success", "--check", "true")
        self.command("verify", fact, expected=0)
        self.assertEqual(self.node(fact)["status"], "VERIFIED")
        self.assertEqual(self.node(fact)["check_strength"], "TRIVIAL")
        # The documented warning exception applies to the author only.
        warning = self.command("cite", fact, expected=0)
        self.assertIn("warning", (warning.stdout + warning.stderr).lower())
        self.command("cite", fact, agent="beta", expected=2)

    def test_assumed_unchecked_fact_blocks_task_completion(self):
        """Port of gate2_assume_taints_and_refuses_done."""
        source = self.add("fact", "unchecked hypothesis")
        task = self.add("task", "work relying on that hypothesis")
        self.command("cite", source, "--assume", "--for", task, expected=0)
        self.command("link", task, "depends_on", source, expected=0)
        self.command("take", task, expected=0)

        self.command("done", task, expected=2)
        self.assertEqual(self.node(task)["state"], "OPEN")
        self.assertEqual(self.lease(task)["owner"], "alpha")

    def test_assumed_ancestor_blocks_completion_using_verified_dependent(self):
        _, dependent = self.assumed_dependency()
        task = self.add("task", "finish work relying on derived evidence")
        self.command("link", task, "depends_on", dependent, expected=0)
        self.command("take", task, expected=0)

        self.command("done", task, expected=2)
        self.assertEqual(self.node(task)["state"], "OPEN")
        self.assertEqual(self.lease(task)["owner"], "alpha")

    def test_taint_clears_when_assumed_ancestor_verifies_without_manual_step(self):
        """Port of taint_clears_when_assumed_fact_verifies, with a transitive chain.

        The owner explicitly specifies automatic clearing, without requiring the
        dependent's previously passing check to be rerun manually.
        """
        source, dependent = self.assumed_dependency()
        task = self.add("task", "completion unblocks when its assumption is proven")
        self.command("link", task, "depends_on", dependent, expected=0)
        self.command("take", task, expected=0)
        self.command("done", task, expected=2)

        self.command("verify", source, expected=0)
        self.assertEqual(self.node(source)["status"], "VERIFIED")
        self.command("done", task, expected=0)
        self.assertEqual(self.node(task)["state"], "DONE")
