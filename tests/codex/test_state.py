"""State, event-watermark and persisted-edge cases migrated from the smoke suite."""
if __package__:
    from .support import RealmCase
else:
    from support import RealmCase


class StateTests(RealmCase):
    def test_link_really_persists_the_correct_dependency_direction(self):
        task = self.add("task", "Depends on evidence")
        fact = self.add("fact", "Evidence required")
        self.command("link", task, "depends_on", fact, expected=0)
        edges = self.rows("SELECT from_id,to_id,kind FROM edges")
        self.assertEqual(edges, [{"from_id": task, "to_id": fact, "kind": "depends_on"}])

    def test_state_reports_frontier_and_live_lease(self):
        task = self.add("task", "Open work")
        fact = self.add("fact", "Open question")
        state = self.command("state", expected=0).stdout
        self.assertIn("FRONTIER", state)
        self.assertIn(task, state)
        self.assertIn(fact, state)
        self.command("take", task, expected=0)
        leased = self.command("state", expected=0).stdout
        self.assertIn(task, leased)
        self.assertIn("alpha", leased)

    def test_since_only_returns_events_after_its_watermark(self):
        first = self.add("task", "First event")
        cursor = self.rows("SELECT MAX(cursor) AS cursor FROM events")[0]["cursor"]
        empty = self.command("since", str(cursor), expected=0).stdout
        self.assertIn("no events since", empty.lower())
        second = self.add("task", "Second event")
        new = self.command("since", str(cursor), expected=0).stdout
        self.assertIn(second, new)
        self.assertNotIn(first, new)
