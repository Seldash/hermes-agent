"""P13 回归 — cron 作业显式列出的 skill 不受 config `platform_disabled` 过滤 (2026-09-13)。

实证: KB·半导体充实(19:15二四六) 手动补跑 (从飞书会话触发 → 会话平台=feishu) 时,
`kb-enrichment-lite` ∈ `skills.platform_disabled.feishu` → `skill_view()` 返回 disabled →
cron prompt 该 skill 段落变成 "could not be found and were skipped", 作业手册整段缺失、
降级运行 (同日同作业的排期运行正常加载 → 只有手动 run 中招)。

修法: `skill_view(..., allow_platform_disabled=True)`, 由 cron/scheduler_prompt.py 显式传入。
platform_disabled 是给交互路由瘦身的闸门, 不适用于「作业显式列出」的加载;
但 config 全局 `skills.disabled` (技能已废弃/移除) **仍然**拦截。

本测试钉死三点:
  T1 交互路径 (无 flag) 仍被 platform_disabled 拦截 (反向验证, 不能把闸门拆了)
  T2 cron 显式加载 (allow_platform_disabled=True) 通过
  T3 全局 `skills.disabled` 对显式加载仍然生效
  T4 端到端: cron/scheduler_prompt._load_cron_skill_parts() 真的把手册注入了 prompt
"""
import json

import pytest

import tools.skills_tool as skills_tool_module
from tools.skills_tool import skill_view


def _make_skill(skills_dir, name, body="手册正文"):
    sd = skills_dir / name
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc {name}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", skills_dir)
    _make_skill(skills_dir, "cron-manual")
    # 会话平台 = feishu (手动 run 从聊天端触发时的真实情形)
    monkeypatch.setenv("HERMES_PLATFORM", "feishu")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "feishu")
    return skills_dir


def _config_with(monkeypatch, *, platform_disabled=("cron-manual",), disabled=()):
    cfg = {"skills": {"platform_disabled": {"feishu": list(platform_disabled)},
                      "disabled": list(disabled)}}
    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)


def test_interactive_view_still_blocked_by_platform_disabled(env, monkeypatch):
    """T1: 反向验证 — 没拆掉交互侧闸门"""
    _config_with(monkeypatch)
    result = json.loads(skill_view("cron-manual"))
    assert result.get("success") is False
    assert "disabled" in (result.get("error") or "")


def test_explicit_load_bypasses_platform_disabled(env, monkeypatch):
    """T2: cron 显式加载通过"""
    _config_with(monkeypatch)
    result = json.loads(skill_view("cron-manual", allow_platform_disabled=True))
    assert result.get("success") is True, result.get("error")
    assert "手册正文" in result.get("content", "")


def test_global_disabled_still_blocks_explicit_load(env, monkeypatch):
    """T3: 全局 disabled (技能被废弃) 对显式加载仍然生效"""
    _config_with(monkeypatch, platform_disabled=(), disabled=("cron-manual",))
    result = json.loads(skill_view("cron-manual", allow_platform_disabled=True))
    assert result.get("success") is False


def test_cron_prompt_includes_platform_disabled_skill(env, monkeypatch):
    """T4: 端到端 — cron prompt 真的带上手册内容, 且不再出现 'could not be found'"""
    _config_with(monkeypatch)
    import tools.skill_usage as skill_usage_mod
    monkeypatch.setattr(skill_usage_mod, "bump_use", lambda *a, **k: None)

    from cron.scheduler_prompt import _load_cron_skill_parts
    parts = _load_cron_skill_parts({"id": "test-job", "name": "测试作业"}, ["cron-manual"])
    blob = "\n".join(parts)
    assert "手册正文" in blob
    assert "could not be found" not in blob
