"""本地语义断言（原先改在上游测试文件里）— 台账 CUSTOM_PATCHES.md T1/T4。

背景：上游 `tests/cron/test_agent_scheduling_gate.py` 与 `tests/cron/test_scheduler.py`
断言的是**纯上游语义**（cron agents 有 memory；`skill_view` 只收 1 个位置参数）。
本仓的 P2/0002（cron `skip_memory=True` → memory toolset 恒被拒）与 P13
（`skill_view(name, allow_platform_disabled=True)`）会让这些上游断言必红。

原做法是**修改上游测试文件**对齐 → 每次升级都在这些文件上冲突（实测 2026-09-24
干跑：这两个文件各贡献冲突块，且 T4 的 verify 失败）。2026-09-25 起改为：
**上游测试文件保持原样**，本地语义断言集中在本文件；
`replay_patches.sh` 对 test-alignment 的 ceded 路径还原上游内容
（ceded 清单见 `~/.hermes/data/patch_manifest.json` 的 `ceded_paths`）。

维护约定：上游若改变 cron denylist 语义或 `skill_view` 签名，先看这里的断言再动。
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import patch

import pytest

from cron.scheduler import _resolve_cron_disabled_toolsets
from cron.scheduler_prompt import _build_job_prompt

# 上游 `ALWAYS_DISABLED = ["messaging", "clarify"]`；本仓无条件追加 memory。
EXPECTED_DENYLIST = ["cronjob", "messaging", "clarify", "memory"]


class TestCronMemoryDeniedByLocalOverride:
    """P2/0002：cron agents 以 `skip_memory=True` 运行 → memory 恒被策略拒绝。

    对应上游 `TestGateOffDefault` 的期望列表（上游不含 memory）与
    `test_memory_not_denied`（上游期望 memory **不**被拒）——本文件断言相反方向。
    """

    @pytest.mark.parametrize(
        "cfg",
        [{}, None, {"cron": {"preflight": True}}],
        ids=["empty", "none", "gate-absent"],
    )
    def test_memory_always_in_denylist(self, cfg):
        assert _resolve_cron_disabled_toolsets(cfg) == EXPECTED_DENYLIST

    @pytest.mark.parametrize(
        "cfg",
        [{}, {"cron": {"allow_agent_scheduling": True}}],
        ids=["gate-off", "gate-on"],
    )
    def test_gate_cannot_reenable_memory(self, cfg):
        """agent-scheduling 闸门只影响 cronjob，不能把 memory 放回来。"""
        assert "memory" in _resolve_cron_disabled_toolsets(cfg)

    def test_user_denylist_wins_over_gate(self):
        cfg = {
            "agent": {"disabled_toolsets": ["cronjob"]},
            "cron": {"allow_agent_scheduling": True},
        }
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "cronjob" in disabled, "用户 denylist 必须优先于闸门"
        assert "memory" in disabled, "本地 memory 拒绝不受用户配置影响"


class TestCronSkillPlatformDisabledKwargContract:
    """T4/P13：显式列出的 cron skill 必须绕过 `platform_disabled` 过滤。

    上游 `TestBuildJobPromptAbsoluteSkillPath` / `TestBuildJobPromptBumpUse` 的
    `_skill_view` fake 只收 1 个位置参数；P13 让它以 `allow_platform_disabled=True`
    调用 → 上游 fake 抛 TypeError，被 `except (json.JSONDecodeError, TypeError)` 吞掉
    → 判成「无效 JSON」跳过（假红：症状指向 skill 未找到，真因是测试替身签名）。
    """

    def test_skill_view_signature_has_kwarg(self):
        from tools import skills_tool

        sig = inspect.signature(skills_tool.skill_view)
        assert "allow_platform_disabled" in sig.parameters, (
            "P13 注入点1 丢失：tools/skills_tool.py 的 skill_view 不再接受 "
            "allow_platform_disabled（见 CUSTOM_PATCHES.md P13）"
        )
        assert sig.parameters["allow_platform_disabled"].default is False, (
            "默认值必须为 False：交互式调用不得默认绕过 platform_disabled"
        )

    def test_build_job_prompt_requests_platform_disabled_bypass(self, tmp_path):
        """行为契约：绝对路径 skill 经 `_build_job_prompt` 加载时实参为 True。"""
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "alpha-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Alpha\nDo alpha.", encoding="utf-8")

        seen: list[tuple[str, bool]] = []

        def _skill_view(name: str, allow_platform_disabled: bool = False) -> str:
            seen.append((name, allow_platform_disabled))
            if name == "alpha-skill":
                return json.dumps({"success": True, "content": "# Alpha\nDo alpha."})
            return json.dumps({"success": False, "error": f"Skill '{name}' not found."})

        with patch("tools.skills_tool.SKILLS_DIR", skills_dir), \
             patch("tools.skills_tool.skill_view", side_effect=_skill_view):
            result = _build_job_prompt({"skills": [str(skill_dir)], "prompt": "go"})

        assert [n for n, _ in seen] == ["alpha-skill"], "绝对路径必须先归一化为 skill 名"
        assert [f for _, f in seen] == [True], (
            "P13 注入点2 丢失：cron/scheduler_prompt.py 不再传 allow_platform_disabled=True "
            "（手动 run 手册会被 platform_disabled 静默跳过）"
        )
        assert "Do alpha." in result
