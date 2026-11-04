"""P14 修订回归：bocha 配额落盘原子性 + 坏文件留证（F-20261103-004/005）。

坏样本两条（缺一不算落地）：
  ① 写一半就抛异常 → 旧文件必须逐字节不变，且不留 .tmp 残渣（旧实现直接 open(w)，
     失败即留半截 JSON，下一次读取被当成「无文件」静默重置计数）。
  ② 文件已是坏 JSON → 必须打 warning + 改名 `.corrupt-<ts>` 留证，然后才返回 fresh；
     旧实现无日志、无留证，计数归零这件事对任何人都不可见。

正样本一条：正常 save → load 往返，且没有任何 `.tmp` 残留。
"""
import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from plugins.web.bocha import provider


@pytest.fixture
def quota_file(tmp_path, monkeypatch):
    path = tmp_path / ".bocha_daily.json"
    monkeypatch.setattr(provider, "QUOTA_FILE", str(path))
    return path


def test_save_then_load_roundtrip(quota_file):
    # 日期必须取「今天」：_load_quota 在 data["date"] != today 时走跨日重置分支（只保留月度计数），
    # 写死常量日期会让本用例在次日变红（2026-11-04 实测恒红），属时变假绿/假红陷阱。
    today = datetime.now().date().isoformat()
    data = provider._fresh_quota(today, today[:7])
    data["count"] = 7
    data["monthly_count"] = 42
    provider._save_quota(data)

    loaded = provider._load_quota()
    assert loaded["count"] == 7
    assert loaded["monthly_count"] == 42
    assert list(quota_file.parent.glob("*.tmp")) == []


def test_save_is_atomic_when_dump_fails_midway(quota_file, monkeypatch):
    quota_file.write_text(json.dumps({"date": "2026-11-03", "count": 99}), encoding="utf-8")
    before = quota_file.read_bytes()

    def _boom(obj, f, *a, **kw):
        f.write('{"date": "2026-11-03", "count":')  # 半截
        raise RuntimeError("disk full")

    monkeypatch.setattr(provider.json, "dump", _boom)
    with pytest.raises(RuntimeError):
        provider._save_quota({"date": "2026-11-03", "count": 100})

    assert quota_file.read_bytes() == before, "写失败后旧配额文件必须原样保留（不得留半截）"
    assert list(quota_file.parent.glob("*.tmp")) == [], "异常路径必须清掉同目录 tmp"


def test_corrupt_file_keeps_evidence_and_warns(quota_file, caplog):
    quota_file.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="plugins.web.bocha.provider"):
        quota = provider._load_quota()

    assert quota["count"] == 0 and quota["monthly_results"] == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("not valid JSON" in m for m in warnings), f"坏文件必须留下 warning，实得 {warnings}"
    kept = list(Path(provider.QUOTA_FILE).parent.glob(".bocha_daily.json.corrupt-*"))
    assert len(kept) == 1, f"坏文件原件必须改名留证，实得 {kept}"
    assert kept[0].read_text(encoding="utf-8") == "{not json"
    assert not Path(provider.QUOTA_FILE).exists(), "坏文件应被改名移走，而非原地覆盖"


def test_missing_file_is_fresh_without_warning(quota_file, caplog):
    with caplog.at_level(logging.WARNING, logger="plugins.web.bocha.provider"):
        quota = provider._load_quota()

    assert quota["count"] == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert list(quota_file.parent.glob(".bocha_daily.json.corrupt-*")) == []


def test_quota_path_is_process_global_not_profile_scoped(tmp_path, monkeypatch):
    """F-005：配额走进程级全局根（多 profile 单进程共用一份），且不再用 expanduser 字面量。"""
    import importlib
    import os

    from hermes_constants import get_process_hermes_home

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "global-root"))
    reloaded = importlib.reload(provider)
    try:
        assert reloaded.QUOTA_FILE == str(
            get_process_hermes_home() / "data" / ".bocha_daily.json"
        )
        assert "~" not in reloaded.QUOTA_FILE
    finally:
        # 还原模块级常量，避免污染同进程后续用例（环境由 monkeypatch 自动回滚）。
        importlib.reload(provider)
