"""rc=2 (EXIT_ALERT) 写入端门禁 + failure_type 按退出码分流 — 待办 853482 第二缺陷 / 本地补丁 P21。

契约 SSOT: ``~/.hermes/scripts/cron_exit_codes.py``。rc=2 表示「脚本跑完并已把发现告知用户」= 不是失败：

* **写入端**: rc=2 不得进 ``cron_incidents``（两处写入点：``_compose_run_delivery`` 失败分支、
  ``_deliver_crash_failure``）；解析不出退出码 → 照旧按失败入库（fail-safe）。
* **分类端**: ``_FAILURE_TYPE_ORDER`` 纯关键字分类会把 rc=1 脚本崩溃 stdout 里的检查项名
  （``subprocess_no_timeout``）或引文里的 "timeout" 当成本次失败类型 → 必须**按权威退出码分流**：
  有 ``Script exited with code N`` 头（前 3 行）→ N==2 → ``alert``，其它 N → ``script``；无头才走关键字。

本文件用**真实** incidents 存储（临时 DB）度量「有没有入库 / 入的是什么类型」，逐条锁死 6 类误伤样本
（含 ``cron_incidents`` 里真实形态的行，见测试内注释）。unittest 形态，可用
``venv/bin/python3 -m unittest tests.cron.test_cron_incidents_rc2_gate`` 直接跑。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.alert_cooldown as alert_cooldown  # noqa: E402
import cron.incidents as incidents  # noqa: E402
import cron.jobs as cron_jobs  # noqa: E402
import cron.scheduler as sched  # noqa: E402

RC2_HEADER = "Script exited with code 2"

# ── 误伤对照样本（全部来自生产 ``cron_incidents`` 的真实文本形态）───────────────
# (标签, 错误文本, is_business_alert 期望, 入库后 failure_type 期望)
MISFIRE_SAMPLES = [
    (
        "① 无退出码 header 的真超时",
        "Script timed out after 300s: /home/hermes/.hermes/scripts/bk_index_pipeline.py",
        False,
        "timeout",
    ),
    (
        "② rc=1 真崩溃",
        "Script exited with code 1\nstderr:\nTraceback (most recent call last):\n"
        '  File "/home/hermes/.hermes/scripts/x.py", line 3, in <module>\n'
        "RuntimeError: boom",
        False,
        "script",
    ),
    (
        "③ 依赖缺失 (rc=1)",
        "Script exited with code 1\nstderr:\nModuleNotFoundError: No module named 'akshare'",
        False,
        "script",
    ),
    (
        "④ 非 2 的非零码 (rc=7)",
        "Script exited with code 7\nstdout:\nboom",
        False,
        "script",
    ),
    (
        "⑤ Script not found",
        "Script not found: /home/hermes/.hermes/scripts/nope.py. Cron scripts are looked up "
        "only in this profile's folder (~/.hermes/scripts).",
        False,
        "script",
    ),
    (
        "⑥ 正文内嵌子进程的 exit=N / 别的作业的 rc=2 引文",
        "Script exited with code 1\nstdout:\n✅ T1 语法检查: 931 个 .py, 0 失败\n"
        "  子进程 exit=0 正常\n  参考: 另一个作业 Script exited with code 2\n"
        "  subprocess_no_timeout: scripts/bk_index_pipeline.py:53",
        False,
        "script",
    ),
    (
        "⑥b rc=2 字样只出现在第 4 行之后（非权威头）",
        "运行失败\n日志:\n----\nScript exited with code 2\n",
        False,
        "script",
    ),
]

# 真告警样本：必须**不入库**，且仍走投递（告警不许被吞）。
ALERT_SAMPLE = f"{RC2_HEADER}\nstdout:\n❌ NAV 冻结: wsts"


class _IsolatedStore(unittest.TestCase):
    """每个用例独立的 executions.db + alert 冷却存储（绝不碰真实 ~/.hermes/*）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(incidents, "EXECUTIONS_FILE", tmp / "cron" / "executions.db"),
            mock.patch.object(alert_cooldown, "ALERT_COOLDOWN_FILE", tmp / "alert_cooldown.json"),
            mock.patch.object(sched, "_alert_repeat_suppress_hours", lambda: 6.0),
            # 失败分支的下游文案/提示与本补丁无关 → 打桩保持用例密闭（不停在 config/env 上）。
            mock.patch.object(sched, "_summarize_cron_failure_for_delivery",
                              lambda job, err: "FAILED"),
            mock.patch.object(sched, "_failure_streak_nudge", lambda job: ""),
            mock.patch("cron.quota_hold.hold_notice", lambda job, secs: ""),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _job(job_id="rc2gate-job", name="rc2 门禁测试作业"):
        return {"id": job_id, "name": name, "deliver": "local"}

    def _run_failure_branch(self, error, job_id="rc2gate-job"):
        """走调度器真实失败分支（``_compose_run_delivery``），返回投递文案。"""
        content, *_rest = sched._compose_run_delivery(
            self._job(job_id), success=False, error=error, final_response="", output_file=None)
        return content


class TestRc2WriteGate(_IsolatedStore):
    def test_rc2_creates_no_ledger_row_and_still_delivers(self):
        content = self._run_failure_branch(ALERT_SAMPLE)

        self.assertEqual(incidents.count_incidents(), 0, "rc=2 不得进 cron_incidents")
        self.assertTrue(content, "告警必须仍被投递（纪律：不许静默）")
        self.assertIn("business alert", content.lower())
        self.assertIn("NAV 冻结", content)                      # 发现正文照转
        self.assertNotIn(RC2_HEADER, content)                    # 调度器的头行不进用户文案

    def test_rc2_crash_path_does_not_write_ledger_but_still_delivers(self):
        """``_deliver_crash_failure`` 是第二处写入点：rc=2 同样不得建条目，且投递不许被吞。"""
        upserts, deliveries = [], []

        with mock.patch.object(sched, "_upsert_incident_for_failure",
                               lambda job, err, **kw: upserts.append(err) or (False, "inc")), \
                mock.patch.object(sched, "_deliver_result",
                                  lambda job, text, **kw: deliveries.append(text)), \
                mock.patch.object(sched, "_delivery_lane_value",
                                  lambda job, for_failure=False: "origin"), \
                mock.patch.object(sched, "_resolve_delivery_targets",
                                  lambda job, for_failure=False: True), \
                mock.patch.object(sched, "_classify_delivery_outcome",
                                  lambda **kw: "suppressed"):
            delivery_error, outcome = sched._deliver_crash_failure(
                self._job(), ALERT_SAMPLE, adapters=None, loop=None)

        self.assertEqual(upserts, [], "rc=2 崩溃路径也不得写台账")
        self.assertEqual(len(deliveries), 1, "告警必须仍离开进程")
        self.assertIsNone(delivery_error)
        self.assertEqual(outcome, "suppressed")

    def test_rc1_crash_path_still_writes_ledger(self):
        """反向对照：真失败在崩溃路径逐字节保持原行为（仍写台账、仍投递）。"""
        upserts = []

        with mock.patch.object(sched, "_upsert_incident_for_failure",
                               lambda job, err, **kw: upserts.append(err) or (False, "inc")), \
                mock.patch.object(sched, "_deliver_result", lambda job, text, **kw: None), \
                mock.patch.object(sched, "_delivery_lane_value",
                                  lambda job, for_failure=False: "origin"), \
                mock.patch.object(sched, "_resolve_delivery_targets",
                                  lambda job, for_failure=False: True), \
                mock.patch.object(sched, "_classify_delivery_outcome",
                                  lambda **kw: "suppressed"):
            sched._deliver_crash_failure(
                self._job(), "Script exited with code 1\nstderr:\nboom", adapters=None, loop=None)

        self.assertEqual(len(upserts), 1, "rc=1 崩溃必须仍写台账")


class TestMisfireSamplesStillRecorded(_IsolatedStore):
    """6 类误伤样本：一条都不许被当 rc=2 吞掉，且分类必须按权威退出码。"""

    def test_samples_still_recorded_with_expected_type(self):
        for label, error, expect_alert, expect_type in MISFIRE_SAMPLES:
            with self.subTest(sample=label):
                self.assertIs(sched._is_business_alert(error), expect_alert,
                              f"{label}: rc=2 判定错")
                self.assertFalse(sched._is_business_alert(error), f"{label}: 被误判成业务告警")

                before = incidents.count_incidents()
                self._run_failure_branch(error, job_id=f"sample-{label}")
                self.assertEqual(incidents.count_incidents(), before + 1,
                                 f"{label}: 真失败被吞，未入库")
                rows = [r for r in incidents.list_incidents() if r["job_id"] == f"sample-{label}"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["failure_type"], expect_type,
                                 f"{label}: failure_type 分流错误")
                self.assertEqual(rows[0]["state"], "detected")

    def test_rc2_sample_alone_is_the_exception(self):
        """⑦ 对照组：真 rc=2（权威头第一行）→ 不入库（与 6 类样本的差别只在这里）。"""
        self.assertTrue(sched._is_business_alert(ALERT_SAMPLE))
        self._run_failure_branch(ALERT_SAMPLE, job_id="rc2-only")
        self.assertEqual(incidents.count_incidents(), 0)


class TestFailureTypeExitCodeRouting(_IsolatedStore):
    def test_header_code_beats_body_keywords(self):
        """生产真值形态：rc=1 崩溃 stdout 里带 subprocess_no_timeout → 曾是 timeout（误标）。"""
        text = ("Script exited with code 1\nstdout:\n✅ T1 语法检查: 931 个 .py, 0 失败\n"
                "  子进程: 1 个\n  subprocess_no_timeout: scripts/bk_index_pipeline.py:53")
        self.assertEqual(incidents._classify_failure_type(text), "script")

    def test_keyword_path_unchanged_without_authoritative_header(self):
        """无权威头 → 关键字路径逐字节不变（真超时仍 timeout，未知仍 unknown）。"""
        self.assertEqual(
            incidents._classify_failure_type("Script timed out after 300s: /x.py"), "timeout")
        self.assertEqual(
            incidents._classify_failure_type("运行 300s subprocess_no_timeout 检查超阈值"), "timeout")
        self.assertEqual(
            incidents._classify_failure_type("something completely unexpected"), "unknown")
        # 非权威形状（无 "脚本退出码/exited with code" 头）不得当成本脚本退出码。
        self.assertEqual(incidents._classify_failure_type("boom: exit=2 (loose-match bait)"), "unknown")

    def test_rc2_classifies_as_alert_defensively(self):
        """写入端已拦住 rc=2；万一被别的调用方写入，类型也必须自证是告警而非失败。"""
        self.assertEqual(incidents._classify_failure_type(ALERT_SAMPLE), "alert")
        self.assertEqual(incidents._classify_failure_type("脚本退出码 2: NAV 冻结"), "alert")
        inc_id, _ = incidents.upsert_incident("direct-call", ALERT_SAMPLE)
        self.assertEqual(incidents.get_incident(inc_id)["failure_type"], "alert")

    def test_last_status_alert_marker(self):
        """rc=2 的跑次状态是 alert（「告警」列口径），rc=1 仍是 error。"""
        self.assertEqual(cron_jobs._derived_last_status(False, ALERT_SAMPLE, None), "alert")
        self.assertEqual(
            cron_jobs._derived_last_status(False, "Script exited with code 1\nboom", None), "error")
        self.assertEqual(
            cron_jobs._derived_last_status(False, "Script timed out after 300s", None), "error")


class TestAuthoritativeCodeParsing(_IsolatedStore):
    def test_only_header_shapes_are_authoritative(self):
        cases = [
            (f"{RC2_HEADER}\nstdout:\nx", 2),
            ("脚本退出码 2: NAV 冻结", 2),
            ("Script exited with code 7", 7),
            ("child: exit=2 hidden in stdout", None),          # 松散形状不作数
            ("line1\nline2\nline3\nScript exited with code 2", None),  # 第 4 行才出现 → 不权威
            ("", None),
        ]
        for text, expected in cases:
            with self.subTest(text=text[:40]):
                self.assertEqual(incidents._authoritative_exit_code(text), expected)


class TestRc2AlsoResolvesStaleIncidents(_IsolatedStore):
    """rc=2 跑次也要解析该作业的开放条目（2026-10-14 增补，待办 256052 / P19 注入点4）。

    为什么：只出 rc=2、从不 success 的作业（代码健康扫描 / 管线告警守护 / 格式合规周检 自 10-01
    起全 rc=2）既够不上 ack 判据（告警型刻意不 ack —— ack 会静默该签名的失败提醒），又永远等不到
    success 跑次 ⇒ 闸门落地前的存量行**没有退出路径**，永久滞留在「未确认」面（实测 12 条 /
    滞留 8~11 天）。rc=2 = 脚本跑完并已自报本次发现，与消费侧按 rc=2 计入「正常运行」同口径。
    """

    @staticmethod
    def _seed(job_id, error, state="alerted"):
        inc_id, _is_new = incidents.upsert_incident(job_id, error, job_name=f"{job_id}-name")
        if state != "detected":
            incidents.set_incident_state(inc_id, state)
        return inc_id

    def _rows(self, job_id):
        return [r for r in incidents.list_incidents() if r["job_id"] == job_id]

    def test_rc2_run_resolves_open_incident_without_adding_rows(self):
        self._seed("stale-rc2-job", "Script exited with code 1\nstderr:\nboom")
        before = incidents.count_incidents()
        self._run_failure_branch(ALERT_SAMPLE, job_id="stale-rc2-job")

        rows = self._rows("stale-rc2-job")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "resolved", "rc=2 跑次必须解析该作业的开放条目")
        self.assertEqual(incidents.count_incidents(), before, "rc=2 仍不得新增条目")

    def test_rc2_does_not_touch_operator_ack(self):
        """操作员 ack（closed）是终态：rc=2 不得改写、更不得复活成会再报的态。"""
        inc_id = self._seed("acked-job", "Script exited with code 1\nstderr:\nboom", state="closed")
        self._run_failure_branch(ALERT_SAMPLE, job_id="acked-job")
        self.assertEqual((incidents.get_incident(inc_id) or {}).get("state"), "closed")

    def test_rc1_run_keeps_incident_open(self):
        """反向对照：真失败跑次不得被解析（仍走失败路径，条目保持非 resolved）。"""
        self._seed("failing-job", "Script exited with code 1\nstderr:\nboom")
        self._run_failure_branch("Script exited with code 1\nstderr:\nboom", job_id="failing-job")
        for row in self._rows("failing-job"):
            self.assertNotEqual(row["state"], "resolved", "真失败不得被误解析")

    def test_rc2_resolves_only_its_own_job(self):
        """解析必须按 job 隔离：别的作业的开放条目不得被顺手关掉。"""
        other = self._seed("other-job", "Script exited with code 1\nstderr:\nboom")
        self._seed("self-job", "Script exited with code 1\nstderr:\nboom")
        self._run_failure_branch(ALERT_SAMPLE, job_id="self-job")
        self.assertEqual((incidents.get_incident(other) or {}).get("state"), "alerted")


if __name__ == "__main__":
    unittest.main()
