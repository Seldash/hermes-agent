"""653694 项1（B2② 注入换序）「近 7 天新建档」的行为契约（2026-10-12）。

锁定两件事：
1. 同档内，近 7 天新建的条目先出 —— 即使旧条目到期日更近（病根：改前前 12 条
   最新一条也是 8 天前创建，新工作被旧条目整体挤出）；
2. created 不可解析时只降档（走旧的 日期档 → delta 路径），不炸排序。
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.system_prompt import (  # noqa: E402
    _build_work_journal_block,
    _journal_created_date,
)


class FakeAgent:
    _work_journal_inject = True
    _work_journal_max_items = 1


class FreshTierTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wj_fresh_tier_")
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self._tmp
        Path(self._tmp, "data").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._old_home:
            os.environ["HERMES_HOME"] = self._old_home
        else:
            os.environ.pop("HERMES_HOME", None)

    def _write(self, todos):
        Path(self._tmp, "data", ".work_journal.json").write_text(
            json.dumps({"todos": todos}), encoding="utf-8")

    def test_created_parser_contract(self):
        self.assertEqual(_journal_created_date({"created": "2026-10-05T07:12:33.1"}),
                         date(2026, 10, 5))
        self.assertIsNone(_journal_created_date({"created": "not-a-date"}))
        self.assertIsNone(_journal_created_date({"created": None}))
        self.assertIsNone(_journal_created_date({}))

    def test_fresh_new_beats_older_with_nearer_due(self):
        """近 7 天新建 vs 30 天前登记但今天到期：前者先出（key2 在 key3 之前）。"""
        today = date.today()
        self._write([
            {"id": "old001", "task": "旧但今日到期", "status": "pending",
             "when": today.isoformat(),
             "created": (today - timedelta(days=30)).isoformat() + "T00:00:00"},
            {"id": "new001", "task": "本周新建", "status": "pending",
             "when": (today + timedelta(days=90)).isoformat(),
             "created": today.isoformat() + "T00:00:00"},
        ])
        lines = [ln for ln in _build_work_journal_block(FakeAgent()).splitlines()
                 if ln.startswith("- [")]
        self.assertEqual(len(lines), 1)
        self.assertIn("本周新建", lines[0])

    def test_in_progress_still_wins_over_fresh(self):
        today = date.today()
        self._write([
            {"id": "fresh1", "task": "本周新建", "status": "pending",
             "created": today.isoformat() + "T00:00:00"},
            {"id": "inpro1", "task": "进行中旧项", "status": "in_progress",
             "created": (today - timedelta(days=120)).isoformat() + "T00:00:00"},
        ])
        lines = [ln for ln in _build_work_journal_block(FakeAgent()).splitlines()
                 if ln.startswith("- [")]
        self.assertIn("进行中旧项", lines[0])

    def test_bad_created_degrades_to_date_tier(self):
        """created 不可解析 → 降档，仍按日期档排序（不抛异常、不静默丢条目）。"""
        today = date.today()
        self._write([
            {"id": "bad001", "task": "created 坏值", "status": "pending",
             "when": (today + timedelta(days=365)).isoformat(), "created": "garbage"},
            {"id": "ok0001", "task": "有日期的旧项", "status": "pending",
             "when": today.isoformat(),
             "created": (today - timedelta(days=300)).isoformat() + "T00:00:00"},
        ])
        lines = [ln for ln in _build_work_journal_block(FakeAgent()).splitlines()
                 if ln.startswith("- [")]
        self.assertEqual(len(lines), 1)
        self.assertIn("有日期的旧项", lines[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
