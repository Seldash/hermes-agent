"""541935 — 注入块「表外到期/逾期」行的行为契约（2026-10-17）。

背景：排序契约（653694 项1：近 7 天新建先于日期档，由 test_work_journal_fresh_tier.py 锁定）
会把「登记早、已逾期」的条目挤到百位之后，而注入块只取前 max_items → 欠账对新会话隐形。
本行是等效层实现：**不吃槽位、不改排序**，只把表外逾期项的 id 列出来。

锁定四件事：
1. 表外逾期项出现在提示行里（有 id），但不进「- [」条目行（槽位不变）；
2. 表内已显示的逾期项不重复列；
3. 无表外逾期项时不打印该行（不留噪音）；
4. 挂起类条目不进逾期行（有过去日期也不算欠账）。
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.system_prompt import _build_work_journal_block  # noqa: E402


class FakeAgent:
    _work_journal_inject = True
    _work_journal_max_items = 1


class OverdueLineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wj_overdue_line_")
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self._tmp
        Path(self._tmp, "data").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._old_home:
            os.environ["HERMES_HOME"] = self._old_home
        else:
            os.environ.pop("HERMES_HOME", None)

    def _block(self, todos, **attrs):
        Path(self._tmp, "data", ".work_journal.json").write_text(
            json.dumps({"todos": todos}), encoding="utf-8")
        agent = FakeAgent()
        for k, v in attrs.items():
            setattr(agent, k, v)
        return _build_work_journal_block(agent)

    def test_overdue_outside_slots_is_named_but_does_not_consume_slot(self):
        today = date.today()
        block = self._block([
            # 表内那一条：本周新建、日期很远（按既有契约排在首位）
            {"id": "new001", "task": "本周新建", "status": "pending",
             "when": (today + timedelta(days=90)).isoformat(),
             "created": today.isoformat() + "T00:00:00"},
            # 表外那一条：30 天前登记、已逾期 1 天（既有排序会沉底）
            {"id": "olderr", "task": "早已逾期的欠账", "status": "pending",
             "when": (today - timedelta(days=1)).isoformat(),
             "created": (today - timedelta(days=30)).isoformat() + "T00:00:00"},
        ])
        item_lines = [ln for ln in block.splitlines() if ln.startswith("- [")]
        self.assertEqual(len(item_lines), 1)                 # 槽位不变
        self.assertIn("本周新建", item_lines[0])              # 排序契约不反转
        self.assertIn("表外到期/逾期 1 条", block)
        self.assertIn("[olderr]", block)
        self.assertIn("wj todo show", block)

    def test_no_line_when_nothing_overdue_outside(self):
        today = date.today()
        block = self._block([
            {"id": "new001", "task": "本周新建", "status": "pending",
             "when": (today + timedelta(days=5)).isoformat(),
             "created": today.isoformat() + "T00:00:00"},
        ])
        self.assertNotIn("表外到期/逾期", block)

    def test_suspended_is_not_counted_as_overdue(self):
        today = date.today()
        block = self._block([
            {"id": "new001", "task": "本周新建", "status": "pending",
             "when": (today + timedelta(days=5)).isoformat(),
             "created": today.isoformat() + "T00:00:00"},
            {"id": "susp01", "task": "挂起项", "status": "pending",
             "when": "挂起(等用户裁决 " + (today - timedelta(days=3)).isoformat() + ")",
             "created": (today - timedelta(days=40)).isoformat() + "T00:00:00"},
        ])
        self.assertNotIn("表外到期/逾期", block)

    def test_included_overdue_is_not_listed_twice(self):
        """表内已经显示的逾期项不该再出现在表外行。"""
        today = date.today()
        block = self._block([
            {"id": "due001", "task": "今日到期", "status": "pending",
             "when": today.isoformat(), "created": today.isoformat() + "T00:00:00"},
        ])
        item_lines = [ln for ln in block.splitlines() if ln.startswith("- [")]
        self.assertIn("今日到期", item_lines[0])
        self.assertNotIn("表外到期/逾期", block)


    def test_hard_date_gate_beats_detail_prose_marker(self):
        """硬日期门 + detail 里写了「等用户」→ 仍要进表外逾期行（不能整体隐形）。

        实测形态：396966 的 when=2026-10-17（当日到期），detail 写「待拍板 5 项（等用户一句话）」
        → `_journal_suspended` 判挂起，排序沉底且本行也不列 ⇒ 该条目在任何一面都看不见。
        """
        today = date.today()
        block = self._block([
            {"id": "new001", "task": "本周新建", "status": "pending",
             "when": (today + timedelta(days=5)).isoformat(),
             "created": today.isoformat() + "T00:00:00"},
            {"id": "hard01", "task": "今日到期的欠账", "status": "pending",
             "when": today.isoformat(),
             "detail": "待拍板 5 项（等用户一句话）：a) 拆 vs 豁免",
             "created": (today - timedelta(days=20)).isoformat() + "T00:00:00"},
        ])
        self.assertIn("表外到期/逾期 1 条", block)
        self.assertIn("[hard01]", block)

    def test_when_field_marker_still_excluded(self):
        """when 字段本身写「挂起(...)」→ 不算逾期（过去日期只是裁决记录）。"""
        today = date.today()
        block = self._block([
            {"id": "new001", "task": "本周新建", "status": "pending",
             "when": (today + timedelta(days=5)).isoformat(),
             "created": today.isoformat() + "T00:00:00"},
            {"id": "susp02", "task": "已裁决暂不投入", "status": "pending",
             "when": "挂起(用户 " + (today - timedelta(days=22)).isoformat() + " 裁决：暂不投入)",
             "created": (today - timedelta(days=30)).isoformat() + "T00:00:00"},
        ])
        self.assertNotIn("表外到期/逾期", block)

    # ── 2026-10-18：可见性/口径四件（决策标注 · 逾期与今日拆分 · id 不再截断 · 时间锚） ──

    def test_decision_items_are_marked(self):
        """kind=decision 必须与执行项可区分（实测 603482/926304 都是等用户拍板）。"""
        today = date.today()
        block = self._block([
            {"id": "dec001", "task": "等用户拍板的项", "status": "pending",
             "kind": "decision", "owner": "user", "when": today.isoformat(),
             "created": today.isoformat() + "T00:00:00"},
            {"id": "exe001", "task": "可开工的执行项", "status": "pending",
             "kind": "exec", "owner": "agent", "when": today.isoformat(),
             "created": today.isoformat() + "T00:00:00"},
        ], _work_journal_max_items=2)
        item_lines = [ln for ln in block.splitlines() if ln.startswith("- [")]
        dec = [ln for ln in item_lines if "[dec001]" in ln][0]
        exe = [ln for ln in item_lines if "[exe001]" in ln][0]
        self.assertIn("⚖️决策", dec)
        self.assertNotIn("⚖️决策", exe)

    def test_due_today_is_not_counted_as_overdue(self):
        """d==0（今日到期）与 d<0（真逾期）必须分开报数，否则「已逾期」名不副实。"""
        today = date.today()
        block = self._block([
            {"id": "new001", "task": "本周新建", "status": "pending",
             "when": (today + timedelta(days=30)).isoformat(),
             "created": today.isoformat() + "T00:00:00"},
            {"id": "late01", "task": "昨天该做", "status": "pending",
             "when": (today - timedelta(days=1)).isoformat(),
             "created": (today - timedelta(days=40)).isoformat() + "T00:00:00"},
            {"id": "tod001", "task": "今天该做", "status": "pending",
             "when": today.isoformat(),
             "created": (today - timedelta(days=40)).isoformat() + "T00:00:00"},
        ])
        self.assertIn("表外到期/逾期 2 条（逾期 1 · 今日 1", block)

    def test_all_ids_listed_up_to_twenty(self):
        """旧实现只列 8 个 id，第 9 条起无入口；拆到 20 条以内必须全部可见。"""
        today = date.today()
        todos = [{"id": "new001", "task": "本周新建", "status": "pending",
                  "when": (today + timedelta(days=30)).isoformat(),
                  "created": today.isoformat() + "T00:00:00"}]
        for i in range(10):
            todos.append({"id": f"od{i:04d}", "task": f"欠账{i}", "status": "pending",
                          "when": (today - timedelta(days=3)).isoformat(),
                          "created": (today - timedelta(days=60)).isoformat() + "T00:00:00"})
        block = self._block(todos)
        self.assertIn("表外到期/逾期 10 条", block)
        for i in range(10):
            self.assertIn(f"[od{i:04d}]", block)

    def test_block_carries_build_time_and_handoff_pointer(self):
        """无时点则新会话无法判断快照年龄；不指 wj-handoff 则拿不到上轮留痕。"""
        today = date.today()
        block = self._block([
            {"id": "new001", "task": "本周新建", "status": "pending",
             "when": (today + timedelta(days=5)).isoformat(),
             "created": today.isoformat() + "T00:00:00"},
        ])
        self.assertIn("📌 截至 ", block)
        self.assertIn("wj-handoff", block)


if __name__ == "__main__":
    unittest.main()
