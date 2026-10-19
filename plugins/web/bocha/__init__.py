"""Bocha AI Search plugin — bundled, auto-loaded.

Chinese-native web search powered by Bocha (博查). Aggregates results from
Baidu, Sogou, 360, Bing, WeChat, and news sources with AI-generated summaries.
Requires ``BOCHA_API_KEY`` in ``~/.hermes/.env``.
"""

from __future__ import annotations

from plugins.web.bocha.provider import BochaWebSearchProvider


def register(ctx) -> None:
    """Register the Bocha provider with the plugin context."""
    ctx.register_web_search_provider(BochaWebSearchProvider())
