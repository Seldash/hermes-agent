"""本地插件断言（原先改在上游测试文件里）— 台账 CUSTOM_PATCHES.md T3 / T1。

覆盖两处本地语义：
1. **T3**：bundled web-plugin 名单里多了 `bocha`（P8 的中文搜索 provider），
   上游 `tests/plugins/web/test_web_search_provider_plugins.py` 硬编码了整份名单，
   加一行就冲突。本文件只断言**我们的新增项**（存在 + 能力位），不复制上游清单，
   这样上游增删 provider 与本仓无关。
2. **T1**：bundled Bocha 插件从 `~/.hermes/.env` 直读 `BOCHA_API_KEY`
   （不经 os.environ），所以上游「清空环境变量即等于零凭据」的假设在本机（存在真实
   .env）不成立 → 需要显式把 key loader 打成 None，否则 `TestUnconfiguredErrorEnvelopeParity`
   假红。
"""

from __future__ import annotations

import json

import pytest


def _ensure_plugins_loaded() -> None:
    from hermes_cli.plugins import _ensure_plugins_discovered

    _ensure_plugins_discovered()


def _clear_web_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "BOCHA_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "SEARXNG_URL",
        "KEENABLE_API_KEY",
        "EXA_API_KEY",
        "PARALLEL_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "FIRECRAWL_GATEWAY_URL",
        "TOOL_GATEWAY_DOMAIN",
        "TAVILY_API_KEY",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)


class TestBundledBochaProvider:
    """T3：bocha 必须在 bundled 注册表内，且只声明 search 能力。"""

    def test_bocha_registered_with_search_only(self):
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        provider = get_provider("bocha")
        assert provider is not None, (
            "P8 的 bocha provider 未注册 → 检查 plugins/web/bocha/ 是否随补丁重放"
            "（CUSTOM_PATCHES.md P8）"
        )
        assert provider.supports_search() is True
        assert provider.supports_extract() is False, "bocha 只提供搜索，不应声明 extract"

    def test_bocha_has_display_name(self):
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        provider = get_provider("bocha")
        assert provider is not None
        assert getattr(provider, "display_name", ""), "provider 必须自带 display_name"


class TestBochaFileSideCredential:
    """T1（P8 派生）：文件侧密钥 —— 清空 env 之后 bocha 仍会被判为已配置。"""

    @pytest.fixture(autouse=True)
    def _register_and_reset(self):
        from tests.tools.conftest import register_all_web_providers

        register_all_web_providers()
        yield
        from agent.web_search_registry import _reset_for_tests

        _reset_for_tests()

    def test_file_side_key_is_not_env_visible(self, monkeypatch):
        """先固化事实：bundled bocha 的 key loader 不是 os.environ。"""
        try:
            from plugins.web.bocha import provider as bocha_provider
        except ImportError:  # pragma: no cover - plugin not bundled in this tree
            pytest.skip("bocha 插件未随本树分发")

        _clear_web_env(monkeypatch)
        assert hasattr(bocha_provider, "_load_api_key"), (
            "bocha provider 失去 _load_api_key（文件侧密钥加载点）。"
            "若上游/本地改直读 os.environ，本契约与 T1 的 test_web_providers 对齐都需重做"
        )

    def test_unconfigured_search_emits_top_level_error(self, monkeypatch):
        """零凭据（含文件侧）时 `web_search_tool` 返回顶层 error envelope。"""
        from tools import web_tools
        from agent import web_search_registry

        _clear_web_env(monkeypatch)
        try:
            from plugins.web.bocha import provider as bocha_provider
        except ImportError:  # pragma: no cover
            bocha_provider = None
        if bocha_provider is not None:
            # 关键：真实 ~/.hermes/.env 里有 BOCHA_API_KEY，必须把 loader 打成 None
            monkeypatch.setattr(bocha_provider, "_load_api_key", lambda: None)

        monkeypatch.setattr(web_tools, "_firecrawl_client", None, raising=False)
        monkeypatch.setattr(web_tools, "_firecrawl_client_config", None, raising=False)
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        monkeypatch.setattr(web_search_registry, "_keyless_tier_enabled", lambda: False)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)

        result = json.loads(web_tools.web_search_tool("hello world", limit=3))
        assert "error" in result, f"expected top-level 'error' key, got {result}"
        assert "Error searching web:" in result["error"]
