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
import tempfile
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider
from hermes_constants import get_process_hermes_home

logger = logging.getLogger(__name__)

BOCHA_API_URL = "https://api.bochaai.com/v1/web-search"
# 2026-11-03 (P14 修订): 配额是**进程级全局**口径 —— 网关多 profile 单进程共用一个
# Bocha 账号，按 profile 分账会让每个 profile 各自拿到 60/1000 的余量，等于放大总量。
# 故取 get_process_hermes_home()（与请求上下文里的 profile override 无关），不吃
# get_hermes_home()（它会跟随 routed-profile 作用域）。同源真身 ~/.hermes/data/.bocha_daily.json。
_HERMES_HOME = get_process_hermes_home()


def _account_home() -> Path:
    """Bocha 账号级配置根：profile 进程要向上回到安装根。

    Bocha 的 API key 与日/月配额属于**账号**，不属于某个 profile。``hermes --profile X``
    启动时 ``HERMES_HOME`` 被指到 ``profiles/X``，于是 ``get_process_hermes_home()`` 返回
    profile 目录：账本被切到 ``profiles/X/data/``（与网关进程分裂成两本账），且
    ``profiles/X/.env`` 通常没有 ``BOCHA_API_KEY`` ⇒ ``is_available()`` 为 False，
    web_search 静默回落别的后端（2026-11-06 实测：auditor 配 bocha 却拿到 exa 结果）。
    这里只对 ``profiles/<name>`` 形态向上取两级，其余（网关/单进程）行为不变。
    """
    h = _HERMES_HOME
    return h.parent.parent if h.parent.name == "profiles" else h


_ACCOUNT_HOME = _account_home()
QUOTA_FILE = str(_ACCOUNT_HOME / "data" / ".bocha_daily.json")
DEFAULT_DAILY_LIMIT = 50
DEFAULT_MONTHLY_LIMIT = 1000


def _load_api_key() -> str | None:
    """Resolve BOCHA_API_KEY: process env → profile .env → install-root .env.

    进程环境优先，因为 Hermes 启动时已按「profile .env 覆盖根 .env」的优先级合并注入；
    文件回退用于未注入的独立调用方。
    """
    key = (os.environ.get("BOCHA_API_KEY") or "").strip()
    if key:
        return key
    for env_path in (_HERMES_HOME / ".env", _ACCOUNT_HOME / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("BOCHA_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _load_quota() -> dict:
    """Load daily + monthly Bocha usage from tracking file."""
    today = datetime.now().date().isoformat()
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
    except FileNotFoundError:
        return _fresh_quota(today, current_month)
    except json.JSONDecodeError as exc:
        # 2026-11-03 (P14 修订): 解析失败**不许静默清零**——旧实现直接 return fresh，
        # 本地限额闸门随之从 0 重新计数，而没有任何一条日志说明「计数被丢了」。
        # 现在：① 打 warning（含原始文件路径与解码错误）② 把坏文件改名留证
        # （.corrupt-<YYYYmmddTHHMMSS>），下一位能拿原件复现而不是只剩一个猜测。
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        corrupt_path = f"{QUOTA_FILE}.corrupt-{stamp}"
        logger.warning(
            "Bocha quota file %s is not valid JSON (%s); keeping evidence at %s and "
            "starting a fresh counter", QUOTA_FILE, exc, corrupt_path,
        )
        try:
            os.replace(QUOTA_FILE, corrupt_path)
        except OSError as rename_exc:  # 留证失败不阻断搜索，但必须留痕
            logger.warning("Could not move corrupt quota file %s aside: %s",
                           QUOTA_FILE, rename_exc)
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
    """Write the quota file atomically (tmp in the same dir + os.replace).

    2026-11-03 (P14 修订): 旧实现 `open(QUOTA_FILE,"w") + json.dump` 中途失败会留下
    半截文件（下一次读取走 JSONDecodeError → 静默重置计数，历史口径整段丢失）。
    同目录 tmp + fsync + `os.replace` 让「旧完整文件」或「新完整文件」二者必居其一；
    任何异常路径都清掉 tmp，不留 `.tmp` 垃圾。
    """
    directory = os.path.dirname(QUOTA_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".bocha_daily.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, QUOTA_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
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

    def get_setup_schema(self) -> dict[str, Any]:
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
            check=False,
        )
    except Exception:
        pass
