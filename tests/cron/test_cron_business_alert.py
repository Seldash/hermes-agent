"""rc=2 业务告警的写入端闸门 + 重复抑制（待办 853482 / plans/plan-rc2-B1-20261009.md）。

契约 SSOT：``scripts/cron_exit_codes.py``。rc=2 表示「脚本跑完并已把发现告知用户」= **不是失败**：

* 不写 ``cron_incidents``（台账只留真失败）—— 反向对照：rc=1 / rc=7 / 解析不出 仍必须写；
* 投递照旧（纪律 2：告警必须仍被看见），但同一 ``(job, 文本)`` 在冷却窗口内不重复投递；
* 存储有老化（30 天）与上限（500 键），每次抑制写一行 jsonl 可审计。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cron import alert_cooldown as ac
from cron import scheduler as sched


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """每个用例独立的冷却存储（绝不碰真实 ~/.hermes/data）。"""
    monkeypatch.setattr(ac, "ALERT_COOLDOWN_FILE", tmp_path / "alert_cooldown.json")
    monkeypatch.setattr(sched, "_alert_repeat_suppress_hours", lambda: 6.0)
    return tmp_path


def _job(job_id="j1", name="假job"):
    return {"id": job_id, "name": name}


# ── D1/D2：首次投递 → 窗口内重复被抑制 ───────────────────────────────────────
def test_first_alert_delivers_and_header_is_stripped():
    out = sched._compose_business_alert_delivery(
        _job(), "Script exited with code 2\nNAV 冻结: wsts")
    assert out and "business alert" in out and "NAV 冻结: wsts" in out
    assert "Script exited with code 2" not in out          # 调度器的头行不进用户文案


def test_repeat_inside_window_is_suppressed():
    first = sched._compose_business_alert_delivery(_job(), "Script exited with code 2\nNAV 冻结")
    second = sched._compose_business_alert_delivery(_job(), "Script exited with code 2\nNAV 冻结")
    assert first and second == ""


def test_repeat_outside_window_delivers_again(monkeypatch):
    monkeypatch.setattr(sched, "_alert_repeat_suppress_hours", lambda: 0)
    assert sched._compose_business_alert_delivery(_job(), "Script exited with code 2\nx")
    assert sched._compose_business_alert_delivery(_job(), "Script exited with code 2\nx") != ""


def test_changed_text_delivers_again():
    sched._compose_business_alert_delivery(_job(), "Script exited with code 2\nNAV 冻结: wsts")
    out = sched._compose_business_alert_delivery(_job(), "Script exited with code 2\nNAV 冻结: 医药")
    assert out and "医药" in out                          # 内容变 → 必须再投（宁可多投不可漏投）


def test_other_job_same_text_is_not_suppressed():
    sched._compose_business_alert_delivery(_job("j1"), "Script exited with code 2\nx")
    assert sched._compose_business_alert_delivery(_job("j2"), "Script exited with code 2\nx") != ""


def test_failure_copy_is_never_used_for_alerts():
    out = sched._compose_business_alert_delivery(_job(), "Script exited with code 2\nX")
    assert "failed:" not in out.lower()           # 失败文案形状（"Cron 'x' failed:"）不得出现
    assert "nothing failed" in out                # 且必须明说「不是失败」


# ── D4：负向对照 —— 真失败 / 未知文本绝不能走告警分支 ───────────────────────
@pytest.mark.parametrize("err", [
    "Script exited with code 1\nboom",
    "Script exited with code 7\nboom",
    "boom: exit=2 (loose-match bait)",        # 非权威头形状 → 按失败（fail-safe）
    "some unparseable failure text",
    "",
])
def test_not_a_business_alert(err):
    assert sched._is_business_alert(err) is False


def test_alert_headers_recognized():
    assert sched._is_business_alert("Script exited with code 2\nX") is True
    assert sched._is_business_alert("脚本退出码 2: X") is True


# ── D1 端到端：走 _compose_run_delivery 时不建 incident ──────────────────────
def test_run_delivery_rc2_creates_no_incident(monkeypatch):
    calls = []
    monkeypatch.setattr(sched, "_upsert_incident_for_failure",
                        lambda *a, **k: calls.append(a) or (False, None))
    content, blocked, silent, acked, incident_id = sched._compose_run_delivery(
        _job(), success=False, error="Script exited with code 2\nNAV 冻结",
        final_response="", output_file=None)
    assert calls == [] and incident_id is None and acked is False
    assert "business alert" in content


def test_run_delivery_rc1_still_creates_incident(monkeypatch):
    calls = []
    monkeypatch.setattr(sched, "_upsert_incident_for_failure",
                        lambda *a, **k: calls.append(a) or (False, "inc1"))
    monkeypatch.setattr(sched, "_summarize_cron_failure_for_delivery", lambda job, err: "FAILED")
    monkeypatch.setattr(sched, "_failure_streak_nudge", lambda job: "")
    monkeypatch.setattr("cron.quota_hold.hold_notice", lambda job, secs: "")
    content, *_rest = sched._compose_run_delivery(
        _job(), success=False, error="Script exited with code 1\nboom",
        final_response="", output_file=None)
    assert calls and "FAILED" in content                  # 真失败路径逐字节不变


# ── D5：老化与上限 ───────────────────────────────────────────────────────────
def test_entries_age_out_after_retention():
    base = datetime(2026, 9, 1, 12, 0)
    ac.note("j1", "old", hours=6.0, now=base)
    assert len(ac.load_store()) == 1
    ac.note("j2", "new", hours=6.0, now=base + timedelta(days=40))
    keys = list(ac.load_store())
    assert len(keys) == 1 and keys[0].startswith("j2:")    # j1 已过期清除


def test_store_is_capped(monkeypatch):
    monkeypatch.setattr(ac, "MAX_KEYS", 3)
    base = datetime(2026, 10, 9, 12, 0)
    for i in range(6):
        ac.note(f"j{i}", f"text{i}", hours=6.0, now=base + timedelta(minutes=i))
    assert len(ac.load_store()) == 3


def test_suppressed_repeats_are_logged():
    base = datetime(2026, 10, 9, 12, 0)
    ac.note("j1", "x", hours=6.0, now=base)
    assert ac.suppressed_since(base - timedelta(days=1)) == 0
    ac.note("j1", "x", hours=6.0, now=base + timedelta(minutes=5))
    assert ac.suppressed_since(base - timedelta(days=1)) == 1


def test_corrupt_store_never_breaks_delivery(tmp_path, monkeypatch):
    path = tmp_path / "alert_cooldown.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(ac, "ALERT_COOLDOWN_FILE", path)
    assert ac.load_store() == {}
    assert sched._compose_business_alert_delivery(_job(), "Script exited with code 2\nX") != ""
