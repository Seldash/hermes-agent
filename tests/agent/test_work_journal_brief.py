"""Tests for the cross-session work-journal brief in the system prompt.

Covers the volatile-tier injection added in 2026-08-07 (A1):
- flag off → no block
- journal file missing / corrupt → fail-open, no block
- no active items → no block
- max_items truncation + remainder hint
- active items render with id / task / when / depends_on
"""
import contextlib
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agent.system_prompt as system_prompt
from agent.system_prompt import _build_work_journal_block


class FakeAgent:
    _work_journal_inject = True
    _work_journal_max_items = 8


class WorkJournalBriefTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wj_brief_test_")
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

    def test_flag_off_returns_empty(self):
        a = FakeAgent()
        a._work_journal_inject = False
        self.assertEqual(_build_work_journal_block(a), "")

    def test_missing_file_returns_empty(self):
        self.assertEqual(_build_work_journal_block(FakeAgent()), "")

    def test_corrupt_file_returns_empty(self):
        Path(self._tmp, "data", ".work_journal.json").write_text("{broken!!", encoding="utf-8")
        self.assertEqual(_build_work_journal_block(FakeAgent()), "")

    def test_no_active_items_returns_empty(self):
        self._write([{"id": "x1", "task": "done", "status": "completed"}])
        self.assertEqual(_build_work_journal_block(FakeAgent()), "")

    def test_active_items_render(self):
        self._write([
            {"id": "abc123", "task": "修 bug", "status": "pending", "when": "本周"},
            {"id": "def456", "task": "部署", "status": "in_progress", "depends_on": "等待审批"},
        ])
        block = _build_work_journal_block(FakeAgent())
        self.assertIn("修 bug", block)
        self.assertIn("[本周]", block)
        self.assertIn("等待审批", block)
        self.assertNotIn("completed", block)  # 终态排除

    def test_max_items_truncation(self):
        self._write([
            {"id": f"id{n:04d}", "task": f"T{n}", "status": "pending"} for n in range(12)
        ])
        a = FakeAgent()
        a._work_journal_max_items = 3
        block = _build_work_journal_block(a)
        item_lines = [l for l in block.split("\n") if l.startswith("- [")]
        self.assertEqual(len(item_lines), 3)
        self.assertIn("其余 9 条", block)

    def test_profile_annotation(self):
        todos = [
            {"id": "ab0001", "task": "魔方应用", "status": "pending", "profile": "game-dev",
             "created": "2026-08-01T00:00:00"},
            {"id": "ab0002", "task": "审计优化", "status": "pending", "profile": "default",
             "created": "2026-08-02T00:00:00"},
            {"id": "ab0003", "task": "历史条目", "status": "pending",
             "created": "2026-08-03T00:00:00"},
        ]
        self._write(todos)
        block = _build_work_journal_block(FakeAgent())
        self.assertIn("[game-dev]", block)
        self.assertIn("魔方应用", block)
        self.assertNotIn("ab0002] 审计优化 [default]", block)  # default 不标注
        self.assertIn("[?]", block)  # 无字段历史条目标 [?]
        self.assertIn("历史条目", block)

    # ── 排序（2026-09-21 P0-7, todo 144665）──
    def test_pin_progress_and_nearest_due_win_slots(self):
        """最早登记的无日期门老条目不得挤掉 进行中/今日到期 项。"""
        todos = [
            # 文件序靠前：老、无日期门、pending —— 旧实现下必然占据名额
            {"id": "o00001", "task": "远古无日期门项", "status": "pending",
             "created": "2026-08-06T00:00:00"},
            {"id": "o00002", "task": "远古无日期门项2", "status": "pending",
             "created": "2026-08-07T00:00:00"},
            # 文件序靠后：in_progress + 近到期
            {"id": "n00001", "task": "进行中项", "status": "in_progress",
             "created": "2026-09-20T00:00:00"},
            {"id": "n00002", "task": "今日到期项", "status": "pending", "when": "2026-09-21",
             "created": "2026-09-19T00:00:00"},
        ]
        self._write(todos)
        a = FakeAgent()
        a._work_journal_max_items = 2
        lines = [ln for ln in _build_work_journal_block(a).splitlines() if ln.startswith("- [")]
        self.assertIn("进行中项", lines[0])
        self.assertIn("今日到期项", lines[1])

    @contextlib.contextmanager
    def _frozen_today(self, base):
        """冻结 ``agent.system_prompt`` 模块内的 ``date``（``today()`` 恒返回 *base*）。

        用例内 when/created 一律以 *base* 为锚写成字面日期 → 断言只取决于冻结日，与真实
        墙钟无关。旧写法用 ``date.today()`` 生成 when、月粒度项却写死 ``"2026-10"``，自
        2026-10-01 起该项（解析为当月 1 日）已成过去 → 位序必错、永久红（台账 851460）。
        子类覆写 ``today()``，生产代码零改动。
        """
        class _FrozenDate(date):
            @classmethod
            def today(cls):
                return base

        with mock.patch.object(system_prompt, "date", _FrozenDate):
            yield

    def test_overdue_before_future_and_month_granularity(self):
        """逾期 < 今日 < 未来；YYYY-MM 月粒度按当月 1 日参与排序（不是无日期门）。

        时间无关：两个冻结日各跑一次，排序结论必须一致（台账 851460 长期红的根因）。
        """
        for frozen in (date(2026, 9, 21), date(2026, 12, 31)):
            with self.subTest(frozen=frozen.isoformat()):
                with self._frozen_today(frozen):
                    self.assertEqual(system_prompt.date.today(), frozen)  # 冻结确实生效
                    self._assert_rank_order(frozen)

    def _assert_rank_order(self, base):
        """以 *base* 为「今天」的四个条目 → 断言注入块的位序（口径与原用例一致）。"""
        overdue = base - timedelta(days=3)
        future = base + timedelta(days=60)
        # 月粒度写法 YYYY-MM → 解析为该月 1 日；取次月，恒晚于 逾期(-3)、早于 远期(+60)
        next_month = (f"{base.year + 1}-01" if base.month == 12
                      else f"{base.year}-{base.month + 1:02d}")
        # created 一律取 7 天窗口之外（2026-10-13, 台账 653694 项1）：本用例的判据是
        # **日期档内部**的位序，而 2026-10-12 起 _journal_rank 多了 key2「近 7 天新建」档，
        # 原先 -10/-9/-8/-7 天的 created 会让「无日期门项」独享 fresh 档而排到第一位，
        # 用例断言的日期档位序被 freshness 档遮蔽（实测 2 子例恒红）。fresh 档的契约由
        # tests/agent/test_work_journal_fresh_tier.py 单独锁定，本用例只钉日期档。
        todos = [
            {"id": "f00001", "task": "远期项", "status": "pending",
             "when": future.isoformat(), "created": (base - timedelta(days=30)).isoformat()},
            {"id": "p00001", "task": "逾期项", "status": "pending",
             "when": overdue.isoformat(), "created": (base - timedelta(days=29)).isoformat()},
            {"id": "m00001", "task": "月粒度项", "status": "pending", "when": next_month,
             "created": (base - timedelta(days=28)).isoformat()},
            {"id": "x00001", "task": "无日期门项", "status": "pending",
             "created": (base - timedelta(days=27)).isoformat()},
        ]
        self._write(todos)
        lines = [ln for ln in _build_work_journal_block(FakeAgent()).splitlines()
                 if ln.startswith("- [")]
        self.assertEqual(len(lines), 4)
        self.assertIn("逾期项", lines[0])
        self.assertIn("月粒度项", lines[1])          # 次月 1 日早于 today+60
        self.assertIn("远期项", lines[2])
        self.assertIn("无日期门项", lines[3])         # 无日期门排最后


if __name__ == "__main__":
    unittest.main(verbosity=2)


def test_journal_due_delta_accepts_yearless_mm_dd():
    """缺年 MM-DD 必须与 YYYY-MM-DD 同口径（补当年）。

    行为契约：闸门 / triage / 注入排序吃同一个 when 字段 → 三种写法解析成同一天。
    反例（2026-09-21）：948091/814010 的 when="09-24" 在 triage 里有日期、在注入排序里
    被判无日期而降档——同一条目在不同视图档位不同。
    """
    from datetime import date as _date

    from agent.system_prompt import _journal_due_delta as f

    today = _date(2026, 9, 24)
    assert f("09-24", today) == 0
    assert f("2026-09-24", today) == 0
    assert f("09-21 (周一窗口)", today) == -3
    assert f("09-24 复核观察数据", today) == 0
    assert f("挂起(等用户)", today) is None
    assert f("2027-02", today) is not None
    assert f("13-45", today) is None  # 非法月日不炸，按无日期处理
