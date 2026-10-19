"""Bocha AI Search — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Calls
the Bocha AI Web Search API (``api.bochaai.com/v1/web-search``) with
daily + monthly quota tracking and auto-pause on exhaustion.

Config keys this provider responds to::

    web:
      search_backend: "bocha"     # explicit per-capability
      backend: "bocha"            # shared fallback

Env var::

    BOCHA_API_KEY=sk-...
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

BOCHA_API_URL = "https://api.bochaai.com/v1/web-search"
QUOTA_FILE = os.path.expanduser("~/.hermes/data/.bocha_daily.json")
DEFAULT_DAILY_LIMIT = 50
DEFAULT_MONTHLY_LIMIT = 1000


def _load_api_key() -> str | None:
    """Load BOCHA_API_KEY from .env file."""
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("BOCHA_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _load_quota() -> dict:
    """Load daily + monthly Bocha usage from tracking file."""
    today = __import__("datetime").date.today().isoformat()
    current_month = today[:7]
    try:
        with open(QUOTA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _fresh_quota(today, current_month)
        if data.get("date") != today:
            # 2026-09-15 修: 日期变化只重置当日 count，monthly_count 必须跨日保留。
            # 原实现整块 return _fresh_quota() → monthly_count 每天归零，
            # 月度计数永远只有当天的量（.bocha_daily.json monthly_count=7 而官方同期 2,921）。
            fresh = _fresh_quota(today, current_month)
            if data.get("month") == current_month:
                fresh["monthly_count"] = int(data.get("monthly_count", 0) or 0)
                fresh["monthly_results"] = int(data.get("monthly_results", 0) or 0)
            return fresh
        if data.get("month") != current_month:
            data["month"] = current_month
            data["monthly_count"] = 0
            data["monthly_results"] = 0
        data.setdefault("limit", DEFAULT_DAILY_LIMIT)
        data.setdefault("monthly_limit", DEFAULT_MONTHLY_LIMIT)
        data.setdefault("count", 0)
        data.setdefault("results", 0)
        data.setdefault("monthly_count", 0)
        data.setdefault("monthly_results", 0)
        data.setdefault("month", current_month)
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return _fresh_quota(today, current_month)


def _fresh_quota(today: str, month: str) -> dict:
    return {
        "date": today,
        "count": 0,
        "results": 0,
        "limit": DEFAULT_DAILY_LIMIT,
        "month": month,
        "monthly_count": 0,
        "monthly_results": 0,
        "monthly_limit": DEFAULT_MONTHLY_LIMIT,
    }


def _save_quota(data: dict) -> None:
    os.makedirs(os.path.dirname(QUOTA_FILE), exist_ok=True)
    with open(QUOTA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


class BochaWebSearchProvider(WebSearchProvider):
    """Search via Bocha AI multi-source Chinese search engine."""

    @property
    def name(self) -> str:
        return "bocha"

    @property
    def display_name(self) -> str:
        return "Bocha / 博查"

    def is_available(self) -> bool:
        return bool(_load_api_key())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a search against the Bocha AI Web Search API.

        Returns the standard ``{"success": ..., "data": {"web": [...]}}`` shape
        expected by ``web_search_tool``.
        """
        api_key = _load_api_key()
        if not api_key:
            return {
                "success": False,
                "error": "BOCHA_API_KEY not set in ~/.hermes/.env",
            }

        # ── Daily + monthly quota check ──
        quota = _load_quota()
        if quota["count"] >= quota["limit"]:
            return {
                "success": False,
                "error": (
                    f"Bocha daily quota exhausted "
                    f"({quota['count']}/{quota['limit']})"
                ),
            }
        if quota["monthly_count"] >= quota["monthly_limit"]:
            _auto_pause(quota)
            return {
                "success": False,
                "error": (
                    f"Bocha monthly quota exhausted "
                    f"({quota['monthly_count']}/{quota['monthly_limit']}). "
                    f"Bocha has been auto-paused."
                ),
            }

        quota["count"] += 1
        quota["monthly_count"] += 1
        _save_quota(quota)

        # ── API call ──
        payload = {
            "query": query,
            "freshness": "noLimit",
            "summary": True,
            "count": min(limit, 50),
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            BOCHA_API_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
            logger.warning("Bocha HTTP %s: %s", e.code, err_body)
            return {
                "success": False,
                "error": f"Bocha returned HTTP {e.code}",
            }
        except Exception as e:
            logger.warning("Bocha request error: %s", e)
            return {
                "success": False,
                "error": f"Bocha request failed: {e}",
            }

        # Bocha wraps response in {"code": 200, "data": {...}}
        data = raw.get("data", raw)
        web_pages = data.get("webPages", {})
        raw_results = web_pages.get("value", [])

        web_results = []
        for i, r in enumerate(raw_results):
            item = {
                "title": str(r.get("name", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("snippet", "")),
                "position": i + 1,
            }
            # Include AI summary if available
            if r.get("summary"):
                item["description"] = (
                    f"[AI摘要] {r['summary']}\n{r.get('snippet', '')}"
                )
            if r.get("siteName"):
                item["site"] = str(r["siteName"])
            web_results.append(item)

        # 官方按「返回条数」计费 → 条数才是配额口径（2026-09-16 起与脚本侧同文件累计）
        quota["results"] = int(quota.get("results", 0) or 0) + len(web_results)
        quota["monthly_results"] = (
            int(quota.get("monthly_results", 0) or 0) + len(web_results)
        )
        _save_quota(quota)

        logger.info(
            "Bocha search '%s': %d results (limit %d)",
            query, len(web_results), limit,
        )

        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Bocha / 博查",
            "badge": "free · 1000/month · ¥0.06/query",
            "tag": (
                "Chinese-native AI search aggregating Baidu, Sogou, 360, Bing, "
                "WeChat, and news with AI summaries. WeChat scan to register."
            ),
            "env_vars": [
                {
                    "key": "BOCHA_API_KEY",
                    "prompt": "Bocha API key (get from https://open.bochaai.com)",
                    "url": "https://open.bochaai.com/",
                },
            ],
        }


def _auto_pause(quota: dict) -> None:
    """Auto-pause Bocha when monthly quota exhausted. Idempotent."""
    paused_file = os.path.expanduser("~/.hermes/data/.paused_tools.json")
    try:
        if os.path.exists(paused_file):
            with open(paused_file, encoding="utf-8") as f:
                paused = json.load(f)
            if "Bocha" in paused:
                return
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    import subprocess
    import sys

    tool_script = os.path.join(
        os.path.dirname(__file__),
        "..", "..", "..", "..", "..", ".hermes", "scripts", "tool_health_check.py",
    )
    # Resolve to absolute
    tool_script = os.path.abspath(os.path.expanduser(
        "~/.hermes/scripts/tool_health_check.py"
    ))
    try:
        subprocess.run(
            [sys.executable, tool_script, "--pause", "Bocha"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass
