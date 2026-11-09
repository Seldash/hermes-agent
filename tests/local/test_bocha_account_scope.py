"""Bocha 账号级作用域回归：key 与配额属于账号，不属于 profile（2026-11-06 审计发现）。

背景（实测）：``hermes --profile auditor`` 启动时 ``HERMES_HOME`` 指向 ``profiles/auditor``，
``get_process_hermes_home()`` 随之返回 profile 目录 —— 于是 ① 配额账本切到
``profiles/auditor/data/.bocha_daily.json``（与网关进程分裂成两本账）；② ``profiles/auditor/.env``
通常没有 ``BOCHA_API_KEY`` ⇒ ``is_available()`` 为 False，web_search 静默回落到别的后端
（auditor 明明配了 bocha，却拿到 exa 的结果）。

判据三条（缺一不算落地）：
  ① profile 形态的 home 向上两级回到安装根；非 profile 形态原样返回（网关/单进程不受影响）。
  ② profile 进程的配额文件 = 安装根 ``data/.bocha_daily.json``（账号一份），不是 profile 一份。
  ③ key 解析顺序 = 进程环境 → profile .env → 安装根 .env。
"""
import importlib
from pathlib import Path

import pytest

from plugins.web.bocha import provider


def test_account_home_climbs_out_of_profile(monkeypatch):
    monkeypatch.setattr(provider, "_HERMES_HOME", Path("/srv/hermes/profiles/auditor"))
    assert provider._account_home() == Path("/srv/hermes")


def test_account_home_is_identity_outside_profiles(monkeypatch):
    monkeypatch.setattr(provider, "_HERMES_HOME", Path("/srv/hermes"))
    assert provider._account_home() == Path("/srv/hermes")
    # 名字像但不是 profiles/<name> 形态的一律不动（避免把 data/profiles 之类的真实目录切走）
    monkeypatch.setattr(provider, "_HERMES_HOME", Path("/srv/hermes/x/profiles"))
    assert provider._account_home() == Path("/srv/hermes/x/profiles")


def test_profile_process_uses_account_quota_file(tmp_path, monkeypatch):
    """F：多 profile 单进程/多进程都只有一本 bocha 账。"""
    install_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(install_root / "profiles" / "auditor"))
    reloaded = importlib.reload(provider)
    try:
        assert reloaded.QUOTA_FILE == str(install_root / "data" / ".bocha_daily.json"), (
            f"profile 进程必须落到账号级账本，实得 {reloaded.QUOTA_FILE}"
        )
        assert "profiles" not in reloaded.QUOTA_FILE
    finally:
        importlib.reload(provider)


def _write_env(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'# local\nBOCHA_API_KEY="{key}"\n', encoding="utf-8")


def test_api_key_order_env_then_profile_then_root(tmp_path, monkeypatch):
    profile = tmp_path / "hermes" / "profiles" / "auditor"
    root = tmp_path / "hermes"
    monkeypatch.setattr(provider, "_HERMES_HOME", profile)
    monkeypatch.setattr(provider, "_ACCOUNT_HOME", root)

    # ① 进程环境优先（Hermes 启动时已按 profile .env 覆盖根 .env 合并注入）
    monkeypatch.setenv("BOCHA_API_KEY", "from-process-env")
    assert provider._load_api_key() == "from-process-env"

    # ② 无环境变量时先看 profile .env
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    _write_env(profile / ".env", "from-profile")
    assert provider._load_api_key() == "from-profile"

    # ③ profile 没有 key 时回落到安装根 .env（这正是 auditor 之前拿不到 key 的那条路）
    (profile / ".env").write_text("# no key here\n", encoding="utf-8")
    _write_env(root / ".env", "from-install-root")
    assert provider._load_api_key() == "from-install-root"

    # ④ 三处都没有 → None（调用侧据此判定 provider 不可用，不得伪造空串）
    (root / ".env").write_text("# none\n", encoding="utf-8")
    assert provider._load_api_key() is None
