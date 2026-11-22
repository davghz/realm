"""Three UI regressions against copied sources and disposable WAL stores only."""

import hashlib
import importlib.util
import os
import re
import sqlite3
import sys
import time
import types
from unittest import mock

if __package__:
    from .support import COMPANION_BYTES, RealmCase
else:
    from support import COMPANION_BYTES, RealmCase


UI_SOURCE_SHA256 = hashlib.sha256(COMPANION_BYTES["realm_ui.py"]).hexdigest()


class UiSnapshotTests(RealmCase):
    def setUp(self):
        super().setUp()
        path = self.project / "realm_ui.py"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), UI_SOURCE_SHA256)
        spec = importlib.util.spec_from_file_location("realm_ui_fixture", path)
        self.ui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ui)

    def test_failed_verification_is_not_labelled_as_successful_verification(self):
        fact = self.add("fact", "failed content check", "--check", self.file_check())
        (self.project / "evidence.txt").write_text("incorrect")
        self.command("verify", fact, expected=5)
        data = self.ui.load(self.space)
        verification = [event for event in data["events"] if event["op"] == "verify"]
        self.assertEqual(len(verification), 1)
        self.assertIn("REFUTED", verification[0]["detail"])

        rendered = self.ui.render(data, 120, 24, True, False)
        self.assertNotRegex(
            rendered, r"\+\s*[1-9]\d*\s+verified\b",
            f"a failed-only verification history was reported as success; UI={UI_SOURCE_SHA256}",
        )
        # Positive control: hard-coding zero successful checks must not pass.
        passing = self.add("fact", "real passing evidence", "--check", self.file_check("passing.txt"))
        self.command("verify", passing, expected=0)
        rendered = self.ui.render(self.ui.load(self.space), 120, 24, True, False)
        self.assertRegex(rendered, r"\+\s*1\s+verified\b",
                         "one genuine success must count, while the failed event must not")

    def test_load_keeps_one_snapshot_across_a_concurrent_wal_commit(self):
        task = self.add("task", "existing dependent")
        before_cursor = self.rows("SELECT MAX(cursor) AS c FROM events")[0]["c"]
        fact = "F900001"
        original_connect = sqlite3.connect
        committed = False

        def commit_new_fact_and_edge():
            nonlocal committed
            if committed:
                return
            # A real second connection commits the node, typed payload, edge, and
            # event atomically. WAL permits this while a read snapshot is open.
            writer = original_connect(self.db, timeout=2)
            try:
                writer.execute("PRAGMA foreign_keys=ON")
                now = int(time.time())
                writer.execute(
                    "INSERT INTO nodes(id,kind,title,created_by,created_at,updated_at) "
                    "VALUES (?,'fact','new assumption','writer',?,?)", (fact, now, now),
                )
                writer.execute(
                    "INSERT INTO facts(node_id,claim,status,assumed) "
                    "VALUES (?,'new assumption','UNCHECKABLE',1)", (fact,),
                )
                writer.execute(
                    "INSERT INTO edges(from_id,to_id,kind,at) VALUES (?,?,'assumes',?)",
                    (task, fact, now),
                )
                writer.execute(
                    "INSERT INTO events(at,actor,op,node_id,detail) "
                    "VALUES (?,'writer','add',?,'concurrent fixture commit')", (now, fact),
                )
                writer.commit()
                committed = True
            finally:
                writer.close()

        class InterleavedRead(sqlite3.Connection):
            def execute(connection, sql, *args, **kwargs):
                # Deterministically commit after the current loader's node scan
                # and before its edge scan. No sleeps, threads, or timing race.
                if re.search(r"\bFROM\s+edges\b", sql, flags=re.IGNORECASE):
                    commit_new_fact_and_edge()
                return super().execute(sql, *args, **kwargs)

        def hooked_connect(*args, **kwargs):
            kwargs["factory"] = InterleavedRead
            return original_connect(*args, **kwargs)

        with mock.patch.object(self.ui.sqlite3, "connect", side_effect=hooked_connect):
            data = self.ui.load(self.space)
        # A future loader may acquire all data in one query, never hitting this
        # hook. Committing after its load is still a valid before-state control.
        commit_new_fact_and_edge()
        self.assertTrue(committed)
        self.assertTrue(self.rows("SELECT id FROM nodes WHERE id=?", (fact,)))

        has_node = fact in data["nodes"]
        has_edge = any(edge["from_id"] == task and edge["to_id"] == fact
                       and edge["kind"] == "assumes" for edge in data["edges"])
        has_event = any(event["node_id"] == fact and event["actor"] == "writer"
                        for event in data["events"])
        has_cursor = data["cursor"] > before_cursor
        observed = (has_node, has_edge, has_event, has_cursor)
        # Accept either coherent side of the commit, without requiring BEGIN,
        # backup(), or any other particular snapshot implementation.
        self.assertIn(observed, ((False, False, False, False), (True, True, True, True)),
                      f"mixed pre/post-commit snapshot {observed}; UI={UI_SOURCE_SHA256}")
        rendered = self.ui.render(data, 120, 24, True, False)
        self.assertIsInstance(rendered, str)  # must not raise for a valid snapshot

    def test_interactive_startup_and_quit_write_no_space_files(self):
        self.add("task", "read-only interactive view")

        class Screen:
            def __init__(screen):
                screen.frames = 0
                screen.key_reads = 0

            def nodelay(screen, value):
                pass

            def getmaxyx(screen):
                return 24, 120

            def erase(screen):
                pass

            def addstr(screen, *args):
                pass

            def refresh(screen):
                screen.frames += 1
                if screen.frames > 2:
                    raise AssertionError("UI did not process the immediate quit key")

            def getch(screen):
                screen.key_reads += 1
                return ord("q")

        screen = Screen()
        curses = types.ModuleType("curses")
        curses.curs_set = lambda value: None
        curses.wrapper = lambda callback: callback(screen)
        curses.error = RuntimeError

        # Establish SQLite's own WAL/SHM coordination files before the baseline.
        # Their shared-memory bookkeeping is not an application-state write.
        keeper = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            keeper.execute("SELECT COUNT(*) FROM nodes").fetchone()

            def files():
                return {str(path.relative_to(self.space)) for path in self.space.rglob("*")}

            before_files = files()
            before_db = self.db.read_bytes()
            with mock.patch.dict(os.environ, {"REALM_SPACE": str(self.space)}), \
                    mock.patch.dict(sys.modules, {"curses": curses}):
                result = self.ui.main(["--ascii", "--poll", "0.1"])
            self.assertEqual(result, 0)
            self.assertGreaterEqual(screen.key_reads, 1)
            self.assertEqual(files(), before_files,
                             f"interactive UI added/removed space files; UI={UI_SOURCE_SHA256}")
            self.assertEqual(self.db.read_bytes(), before_db)
            self.assertFalse((self.space / "ui-opened.json").exists())
        finally:
            keeper.close()
