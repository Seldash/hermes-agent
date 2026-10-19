"""Tests for the builtin todo <-> work journal bridge (B1, 2026-08-07).

Covers agent/todo_journal_bridge.py:
- mirror writes the in-memory store into journal.builtin_todos
- main todos array is NOT touched (cross-end space stays clean)
- config gate off → no mirror
- no store → no mirror
- read_builtin_todos returns persisted mirror
- fail-open on corrupt journal
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.todo_journal_bridge import (
    _MIRROR_KEY,
    mirror_builtin_todos,
    read_builtin_todos,
)


class FakeStore:
    def __init__(self, items):
        self._items = items

    def read(self):
        return [dict(i) for i in self._items]


class FakeAgent:
    def __init__(self, store=None, gate=True):
        self._todo_store = store
        self._todo_mirror_to_journal = gate


class TodoJournalBridgeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="wj_bridge_test_"))
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self._tmp)
        (self._tmp / "data").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._old_home:
            os.environ["HERMES_HOME"] = self._old_home
        else:
            os.environ.pop("HERMES_HOME", None)

    def _journal(self):
        p = self._tmp / "data" / ".work_journal.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def test_mirror_writes_builtin_todos(self):
        store = FakeStore([
            {"id": "t1", "content": "step one", "status": "in_progress"},
            {"id": "t2", "content": "step two", "status": "pending"},
        ])
        ok = mirror_builtin_todos(FakeAgent(store))
        self.assertTrue(ok)
        j = self._journal()
        self.assertEqual(len(j.get(_MIRROR_KEY, [])), 2)
        self.assertEqual(j[_MIRROR_KEY][0]["id"], "t1")

    def test_main_todos_untouched(self):
        # Seed a journal with cross-end todos first
        p = self._tmp / "data" / ".work_journal.json"
        p.write_text(json.dumps({
            "todos": [{"id": "abc123", "task": "cross-end", "status": "pending"}],
        }), encoding="utf-8")
        store = FakeStore([{"id": "t1", "content": "session plan", "status": "pending"}])
        mirror_builtin_todos(FakeAgent(store))
        j = self._journal()
        # main todos preserved exactly
        self.assertEqual(len(j.get("todos", [])), 1)
        self.assertEqual(j["todos"][0]["id"], "abc123")
        # builtin mirror separate
        self.assertEqual(len(j.get(_MIRROR_KEY, [])), 1)

    def test_gate_off_no_mirror(self):
        store = FakeStore([{"id": "t1", "content": "x", "status": "pending"}])
        ok = mirror_builtin_todos(FakeAgent(store, gate=False))
        self.assertFalse(ok)
        self.assertNotIn(_MIRROR_KEY, self._journal())

    def test_no_store_no_mirror(self):
        ok = mirror_builtin_todos(FakeAgent(store=None))
        self.assertFalse(ok)
        self.assertNotIn(_MIRROR_KEY, self._journal())

    def test_read_builtin_todos(self):
        store = FakeStore([{"id": "t1", "content": "persist me", "status": "in_progress"}])
        mirror_builtin_todos(FakeAgent(store))
        items = read_builtin_todos()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "persist me")

    def test_corrupt_journal_fail_open(self):
        p = self._tmp / "data" / ".work_journal.json"
        p.write_text("{corrupt!!!", encoding="utf-8")
        store = FakeStore([{"id": "t1", "content": "x", "status": "pending"}])
        # mirror should not raise; journal recreated cleanly
        ok = mirror_builtin_todos(FakeAgent(store))
        self.assertTrue(ok)
        j = self._journal()
        self.assertEqual(len(j.get(_MIRROR_KEY, [])), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
