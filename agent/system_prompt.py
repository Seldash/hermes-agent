"""System-prompt assembly for :class:`AIAgent`.

Built once per session and reused across turns (only context compression
triggers a rebuild) so the upstream prefix cache stays warm.  Three tiers are
joined with ``\\n\\n``: ``stable`` (identity, guidance, env hints, coding brief,
platform hints), ``context`` (workspace snapshot, caller ``system_message``,
context files) and ``volatile`` (skills index, memory, USER.md, external memory
provider, timestamp line).  See ``references/system-prompt-invariant.md``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.delegation_context import owned_kanban_task
from agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY, EXECUTION_GUIDANCE_MODELS, GOOGLE_MODEL_OPERATIONAL_GUIDANCE,
    HERMES_AGENT_HELP_GUIDANCE, HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS, KANBAN_GUIDANCE,
    PARALLEL_TOOL_CALL_GUIDANCE, PLATFORM_HINTS, SESSION_SEARCH_GUIDANCE,
    SKILLS_GUIDANCE, STEER_CHANNEL_NOTE, TASK_COMPLETION_GUIDANCE, TELEGRAM_RICH_MESSAGES_HINT,
    TOOL_USE_ENFORCEMENT_GUIDANCE, TOOL_USE_ENFORCEMENT_MODELS, drain_truncation_warnings,
)
from agent import prompt_builder as _pb
from agent.runtime_cwd import resolve_agent_cwd, resolve_context_cwd
from hermes_constants import get_default_hermes_root, get_hermes_home
from utils import is_truthy_value

logger = logging.getLogger(__name__)
_PLUGIN_SECTION_FRAME_RE = re.compile(
    r"^## Plugin Context: (?P<id>[a-z0-9][a-z0-9._-]{0,127})\n<!-- hermes-plugin-section-chars:(?P<chars>[0-9]{1,4}) -->\n\n",
    re.MULTILINE,
)
_GATE_WORDS = {**dict.fromkeys(("true", "always", "yes", "on"), True), **dict.fromkeys(("false", "never", "no", "off"), False)}


def _model_gate(setting: Any, model: Optional[str], default_models) -> bool:
    """Resolve a config gate: True/"true"-ish -> on, False/"false"-ish -> off,
    list -> case-insensitive model-substring match, anything else ("auto") ->
    match against *default_models*."""
    if setting is True or setting is False:
        return setting
    if isinstance(setting, str) and setting.lower() in _GATE_WORDS:
        return _GATE_WORDS[setting.lower()]
    model_lower = (model or "").lower()
    if isinstance(setting, list):
        return any(p.lower() in model_lower for p in setting if isinstance(p, str))
    return any(p in model_lower for p in default_models)


def _journal_due_delta(when: Any, today: date) -> Optional[int]:
    """First ``YYYY-MM[-DD]`` in ``when`` → days from ``today`` (negative = overdue).

    Three accepted shapes, aligned with ``scripts/wj_triage._when_date`` (one ``when``
    field, one parse): ``YYYY-MM-DD``, ``YYYY-MM`` (journal convention, counts as the
    1st of that month), and year-less ``MM-DD`` (assumes ``today``'s year). Prose
    without any date, like ``挂起(等用户)``, yields ``None``.
    """
    s = str(when or "")
    m = re.search(r"(\d{4})-(\d{2})(?:-(\d{2}))?", s)
    if m:
        try:
            return (date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)) - today).days
        except ValueError:
            return None
    # 缺年 ``MM-DD`` → 补 today 的年份，口径与 scripts/wj_triage._when_date 一致：
    # 闸门 / triage / 注入排序吃同一个 when 字段，三种写法必须解析成同一天，否则同一条目
    # 在不同视图里档位不同（2026-09-21 实测 948091/814010 的 "09-24" 在 triage 有日期、
    # 在注入排序被判无日期而降档第三档）。
    m = re.search(r"(?<!\d)(\d{2})-(\d{2})(?!\d)", s)
    if not m:
        return None
    try:
        return (date(today.year, int(m.group(1)), int(m.group(2))) - today).days
    except ValueError:
        return None


def _journal_rank(t: Dict[str, Any]) -> Tuple[int, int, int, str]:
    """Sort key for the brief: in_progress first, then nearest due date, no-date last.

    Replaces the plain file-order slice (2026-09-21, todo 144665 P0-7): once the
    journal held 100+ active items the "first 8" were always the oldest entries, so
    a fresh session never saw today's or this week's work. Oldest-first stays the
    tie-break inside each bucket, so the block is still stable/deterministic for a
    given journal state (the volatile tier is rebuilt once per session).
    """
    today = date.today()
    delta = _journal_due_delta(t.get("when", ""), today)
    # 日期档：0 = 有日期（能排期）· 1 = 无日期（待补，数据缺陷）· 2 = 挂起/触发式（明确在等外部，
    # 不该挤占"现在能动"的位置）。挂起词表与 scripts/wj_gate.TRIGGER_MARKERS 对齐，
    # 跨仓库无法共享代码 → scripts/tests/test_when_vocab_parity.py 做一致性断言。
    if _journal_suspended(t):
        bucket = 2
    elif delta is not None:
        bucket = 0
    else:
        bucket = 1
    return (
        0 if t.get("status") == "in_progress" else 1,
        bucket,
        delta if delta is not None else 0,
        str(t.get("created", "")),
    )


_JOURNAL_SUSPEND_MARKERS = (
    "挂起", "暂缓", "择机", "暂不处理", "待用户", "用户人工", "用户方便", "用户本地",
    "用户选定", "用户确认", "触发式", "按需", "触发条件", "远期研究池", "管局审批",
    "备案通过后", "待外部", "等用户", "回到办公室",
)


def _journal_suspended(t: Dict[str, Any]) -> bool:
    """挂起/触发式判定：when/task/detail 任一命中词表。

    与 scripts/wj_gate.TRIGGER_MARKERS 同源（跨仓库不共享代码，靠一致性测试锁定）。
    """
    blob = " ".join(str(t.get(k, "")) for k in ("when", "task", "detail"))
    return any(m in blob for m in _JOURNAL_SUSPEND_MARKERS)


def _build_work_journal_block(agent: Any) -> str:
    """Cross-session work-journal brief for the volatile system-prompt tier.

    Reads <HERMES_HOME>/data/.work_journal.json (written by
    log_session_work.py — the Feishu/CLI shared TODO + work log) and
    renders the active items (pending / in_progress) as a compact block so
    a brand-new session starts knowing what other ends of the system have
    on their plates.  Completed / cancelled items are excluded — they are
    finished work and only add noise.

    Cache-safe by construction: this lives in the *volatile* tier, which
    is rebuilt once per session and then byte-stable for its lifetime.
    The journal is only read at prompt-build time; mid-session todo
    changes are visible to the agent through the todo tool instead.

    Fail-open: any missing / corrupt file, or an agent without the flag
    enabled, returns "" so prompt assembly never breaks on this block.
    """
    if not getattr(agent, "_work_journal_inject", False):
        return ""
    max_items = int(getattr(agent, "_work_journal_max_items", 8) or 8)
    try:
        journal_path = get_hermes_home() / "data" / ".work_journal.json"
        if not journal_path.exists():
            return ""
        data = json.loads(journal_path.read_text(encoding="utf-8"))
        todos = data.get("todos", []) if isinstance(data, dict) else []
    except Exception:
        logger.debug("work-journal brief unavailable (fail-open)", exc_info=True)
        return ""
    active = [
        t for t in todos
        if isinstance(t, dict) and t.get("status") in ("pending", "in_progress")
    ]
    if not active:
        return ""
    lines = []
    ranked = sorted(active, key=_journal_rank)
    for t in ranked[:max_items]:
        tid = str(t.get("id", "?"))[:6]
        task = str(t.get("task", "")).strip() or "(no title)"
        tag = ""
        # profile 归属标注（2026-08-21 防跨 profile 误接）：
        # 非 default 显眼标注 [game-dev]，无字段历史条目标 [?]，
        # 与 log_session_work.py todo list 口径一致
        tpfx = t.get("profile", "")
        if tpfx and tpfx != "default":
            tag += " [" + str(tpfx) + "]"
        elif not tpfx:
            tag += " [?]"
        if t.get("when"):
            tag += " [" + str(t["when"]) + "]"
        if t.get("depends_on"):
            tag += " ←" + str(t["depends_on"])
        # 决策项显式标注（2026-10-18）：kind=decision 与执行项同形时，新会话会把
        # 「等用户拍板」当成可开工项（实测 603482/926304 都是 decision/owner=user）。
        if str(t.get("kind", "")) == "decision":
            tag += " ⚖️决策"
        lines.append("- [" + tid + "] " + task + tag)
    if len(active) > max_items:
        lines.append("- … 其余 " + str(len(active) - max_items) +
                     " 条活跃待办（本块按 进行中→近 7 天新建→到期日近者 排序取前 "
                     + str(max_items) + "；三组分诊见 `wj next`，完整清单见 `wj todo list`）")
    # 时间锚 + 交接入口（2026-10-18）：本块是会话构建时快照，会话内 journal 变化不会回写；
    # 无时点则新会话无法判断快照年龄。上轮留痕/到点窗口/待拍板只有 wj-handoff 提供。
    # 用 hermes_time（显示时区）而非系统本地时间：与文件其余时点口径一致。
    from hermes_time import now as _hermes_now
    lines.insert(0, "📌 截至 " + _hermes_now().strftime("%Y-%m-%d %H:%M") +
                 "（本块为构建时快照，会话内不刷新；开工先跑 `wj-handoff` 取上轮留痕/到点窗口/待拍板）")
    # 逾期欠账不占槽位，但必须可见（2026-10-17, 待办 541935）。
    # 动机：排序契约（653694 项1「近 7 天新建」先于日期档）会把「登记早、已逾期」的条目挤到
    # 百位之后（实测 4 条 10-16 逾期项位于第 103–106、1 条当日到期项位于第 186），而本块只取
    # 前 max_items → 欠账对新会话完全隐形。反转排序会破坏已锁定契约（tests/agent/
    # test_work_journal_fresh_tier.py），故在等效层实现：表外单独列出逾期 id，不吃槽位。
    today = date.today()
    shown = {id(t) for t in ranked[:max_items]}
    overdue = []
    for t in active:
        if id(t) in shown:
            continue
        # 本行只看 **when 字段**判挂起，不看 detail 散文：实测 396966 的 when 是硬日期门
        # 2026-10-17（当日到期），却因 detail 里写了「待拍板 5 项（等用户一句话）」被
        # `_journal_suspended` 判成挂起 → 排序沉底、本行也不列 = 该条目在任何一面都看不见。
        # 排序仍沿用 _journal_suspended（口径与 wj_gate 一致，改动面另行评估），
        # 本行只负责「让欠账有名字」。
        if any(m in str(t.get("when", "")) for m in _JOURNAL_SUSPEND_MARKERS):
            continue
        d = _journal_due_delta(t.get("when", ""), today)
        if d is not None and d <= 0:
            overdue.append((d, t))
    if overdue:
        overdue.sort(key=lambda p: (p[0], str(p[1].get("created", ""))))
        # 口径拆分（2026-10-18）：本行原本把「今日到期」也算进「已逾期」（实测 14 条里
        # 9 条是 d==0），且只列 8 个 id → 6 条（含 2 条 decision）完全无入口。
        n_over = sum(1 for d, _ in overdue if d < 0)
        n_today = len(overdue) - n_over
        ids = " ".join("[" + str(x.get("id", "?"))[:6] + "]" for _, x in overdue[:20])
        more = "" if len(overdue) <= 20 else " 等 " + str(len(overdue)) + " 条"
        lines.append("- ⚠️ 表外到期/逾期 " + str(len(overdue)) + " 条（逾期 " + str(n_over) +
                     " · 今日 " + str(n_today) + "，不占上表槽位，最逾期在前）: " + ids + more +
                     "；判据/入口见 `wj-handoff` ② 与 `wj todo show --id <id>`")
    block = "跨端工作待办（work journal，来自 CLI/飞书/cron 各端登记）:\n" + "\n".join(lines)
    return block


def _resolve_platform_hint(agent: Any, platform_key: str, default_hint: str) -> str:
    """Apply the ``platform_hints.<platform>`` config override: ``replace``
    substitutes the default, ``append`` adds text (a bare string is shorthand
    for append). Malformed entries fall back to the unmodified default so bad
    config can never break prompt assembly or leak across platforms."""
    overrides = getattr(agent, "_platform_hint_overrides", None)
    spec = overrides.get(platform_key) if platform_key and isinstance(overrides, dict) else None
    if isinstance(spec, str):
        spec = {"append": spec}
    if not isinstance(spec, dict):
        return default_hint
    replace_text, append_text = (v.strip() if isinstance(v, str) else "" for v in (spec.get("replace"), spec.get("append")))
    base = replace_text or default_hint
    return f"{base}\n\n{append_text}".strip() if append_text else base


_TUI_EMBEDDED_PANE_CLARIFIER = (
    " You're in its embedded terminal pane, beside the GUI chat — the user can "
    "select your output (Option-drag on macOS, Shift-drag elsewhere) and press "
    "Cmd/Ctrl+L to send it to the chat composer."
)


def _tui_embedded_pane_clarifier(hint: str) -> str:
    """Append the desktop embedded-terminal clarifier when ``HERMES_DESKTOP_TERMINAL``
    is set (only the desktop's TUI PTY, never the chat backend). Idempotent."""
    if not hint or _TUI_EMBEDDED_PANE_CLARIFIER in hint or not is_truthy_value(os.getenv("HERMES_DESKTOP_TERMINAL")):
        return hint
    return hint + _TUI_EMBEDDED_PANE_CLARIFIER


def _plugin_session_info(agent: Any) -> dict[str, str]:
    """Return immutable-at-render-time metadata exposed to prompt sections."""
    try:
        cwd = str(resolve_context_cwd() or "")
    except Exception:
        cwd = ""
    info = {k: str(getattr(agent, k, None) or "") for k in ("session_id", "model", "provider", "platform")}
    info.update(profile_name=_active_profile_name(agent, _ambient_plugin_profile_name), cwd=cwd)
    return info


def _ambient_plugin_profile_name() -> str:
    from hermes_cli.profiles import get_active_profile_name
    return str(get_active_profile_name() or "default")


def _active_profile_name(agent: Any, ambient) -> str:
    """Profile name from the agent's OWN home, else *ambient()*; "default" on any
    failure. Ambient resolution misreports on threads that lost the HERMES_HOME
    ContextVar, which is why the agent's home is preferred."""
    try:
        home = _agent_home(agent)
        return _profile_name_for_home(home) if home is not None else ambient()
    except Exception:
        return "default"


def _frozen_plugin_prompt_sections(agent: Any) -> tuple:
    """Render plugin sections once per session and freeze them on the agent.
    A restored ``_cached_system_prompt`` is parsed instead of re-running plugin
    code; a render that raises at a rebuild boundary keeps the previous bytes
    (stashed by ``invalidate_system_prompt``) instead of silently vanishing."""
    if hasattr(agent, "_plugin_system_prompt_sections_snapshot"):
        return agent._plugin_system_prompt_sections_snapshot
    stored_prompt = getattr(agent, "_cached_system_prompt", None)
    if isinstance(stored_prompt, str) and stored_prompt:
        rendered = _restore_plugin_prompt_sections(stored_prompt)
    else:
        try:
            from hermes_cli.plugins import render_system_prompt_sections
            rendered = tuple(render_system_prompt_sections(_plugin_session_info(agent)))
        except Exception as exc:
            rendered = getattr(agent, "_plugin_system_prompt_sections_previous", None)
            if rendered:
                logger.warning("Plugin system prompt sections failed to re-render (%s); keeping the previous frozen sections", exc)
            else:
                logger.warning("Plugin system prompt sections could not be rendered: %s", exc)
                rendered = ()
    agent._plugin_system_prompt_sections_snapshot = rendered
    return rendered


def _restore_plugin_prompt_sections(prompt: str) -> tuple:
    """Recover frozen section bytes from the persisted full prompt.  Only the
    exact canonical container emitted by core is accepted — user/project text
    may resemble a frame."""
    from hermes_cli.plugins import (
        MAX_SYSTEM_PROMPT_SECTION_CHARS, PLUGIN_SECTIONS_END, PLUGIN_SECTIONS_START,
        RenderedPluginSystemPromptSection, format_system_prompt_sections,
    )
    start = prompt.rfind(PLUGIN_SECTIONS_START)
    end = prompt.find(PLUGIN_SECTIONS_END, start + len(PLUGIN_SECTIONS_START)) if start >= 0 else -1
    if end < 0:
        return ()
    after_end = end + len(PLUGIN_SECTIONS_END)
    if not prompt[after_end:].startswith("\n\nConversation started:"):
        return ()
    framed = prompt[start:after_end]
    restored = []
    for match in _PLUGIN_SECTION_FRAME_RE.finditer(framed):
        content_len = int(match.group("chars"))
        content = framed[match.end() : match.end() + content_len]
        if content_len > MAX_SYSTEM_PROMPT_SECTION_CHARS or len(content) != content_len:
            continue
        restored.append(RenderedPluginSystemPromptSection(id=match.group("id"), content=content,
                                                          position="after_memory", plugin="persisted-prompt"))
    return tuple(restored) if format_system_prompt_sections(restored) == framed else ()


def restore_plugin_prompt_sections(agent: Any, prompt: str) -> None:
    """Seed a resumed agent's frozen snapshot from persisted prompt bytes."""
    agent._plugin_system_prompt_sections_snapshot = _restore_plugin_prompt_sections(prompt)


def _plugin_section_blocks(sections: tuple, position: str) -> list[str]:
    from hermes_cli.plugins import format_system_prompt_sections
    block = format_system_prompt_sections([s for s in sections if s.position == position])
    return [block] if block else []


def _session_start_like(agent: Any, now: Any) -> Any:
    """Best-known conversation start time, or ``now`` as a fallback.
    ``Conversation started:`` must be byte-stable across rebuilds (compression,
    resume, fresh gateway turns), so prefer immutable sources in order: the
    lineage-root session id's embedded stamp (compaction rotates ids, each with
    its own mint time), the current session id's stamp, ``agent.session_start``,
    then ``now``.  Stamps are box-local wall-clock: attach that zone first, then
    convert to ``now``'s zone so the date matches the per-turn clock.

    0. the LINEAGE-ROOT session id's embedded timestamp — compaction can rotate the session id, and each
    rotated id embeds its OWN mint time, so after months of compactions rung 1 alone would quietly re-birth
    the conversation at its latest rotation. Walking to the lineage root (same walk as
    ``_conversation_root_id``) recovers the ORIGINAL birth stamp — a Bot Mode forever-chat keeps knowing
    when it was first born, across every compaction (maintainer-directed, #98426); 1. the timestamp embedded
    in ``session_id`` (``YYYYMMDD_HHMMSS_...``) — immutable for the life of the session, so the line is
    byte-stable across every rebuild boundary (preserving prefix-cache KV); 2. 3. ``now`` (initial/legacy
    build without either).
    """
    from datetime import datetime
    def _to_display_tz(dt: Any) -> Any:
        if dt.tzinfo is None:
            try:
                # The offset in force at dt, not today's: a stamp from the other DST half differs by an hour.
                dt = dt.astimezone()
            except (ValueError, OSError):
                pass
        if getattr(now, "tzinfo", None) is not None and dt.tzinfo is not None:
            try:
                dt = dt.astimezone(now.tzinfo)
            except (ValueError, OSError):
                pass
        return dt
    session_id = getattr(agent, "session_id", None)
    db = getattr(agent, "_session_db", None)
    try:
        root_id = db.get_conversation_root(session_id) if db is not None and isinstance(session_id, str) and session_id else None
    except Exception:
        root_id = None
    for candidate in (root_id, session_id):
        m = re.match(r"^(\d{8})_(\d{6})", candidate) if isinstance(candidate, str) else None
        if m:
            try:
                return _to_display_tz(datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S"))
            except ValueError:
                pass
    session_start = getattr(agent, "session_start", None)
    return _to_display_tz(session_start) if hasattr(session_start, "astimezone") else now


def _agent_home(agent: Any) -> Optional[Path]:
    """The agent's OWN profile home, or None to use ambient resolution.
    A bound HERMES_HOME ContextVar override wins (the gateway multiplexes
    profiles over one shared session DB and binds the home per turn); else the
    parent of ``_session_db.db_path`` — ground truth on threads that lost the
    ContextVar, where ambient resolution would leak the launch profile.

    1. Surfaces that multiplex several profiles over ONE shared session DB (the messaging gateway:
    ``gateway/run.py`` hands every agent the launch-home ``state.db`` and binds the profile home per turn
    via ``_profile_runtime_scope`` + ``copy_context``) would otherwise have the db-derived launch home STOMP
    the correctly-bound profile — inverting the leak this helper exists to fix (found by @kshitijk4poor's
    post-merge probe on #86313). 2. Fallback: the home containing the agent's ``_session_db.db_path``
    (``<home>/state.db``) — ground truth on threads that lost the ContextVar (ContextVars don't propagate
    into ``threading.Thread``), where the unbound build previously fell back to the launch home and leaked
    the default profile's skills/identity into a bot prompt.
    """
    try:
        from hermes_constants import get_hermes_home_override
        override = get_hermes_home_override()
        if override:
            return Path(override)
    except Exception:
        pass
    try:
        db_path = getattr(getattr(agent, "_session_db", None), "db_path", None)
        return Path(db_path).parent if db_path else None
    except Exception:
        return None


def _agent_skills_dir(agent: Any) -> Optional[Path]:
    """The agent's own ``<home>/skills`` dir, or None to use ambient home."""
    home = _agent_home(agent)
    return home / "skills" if home is not None else None


def _profile_name_for_home(home: Path) -> str:
    """``<root>/profiles/X`` -> ``"X"``; anything else -> ``"default"``.
    Uses ``get_default_hermes_root()`` (NOT ``get_hermes_home()``): on a bound
    profile session the ambient home IS the profile dir, so every profile
    would misreport as "default"."""
    try:
        from hermes_constants import get_default_hermes_root
        rel = home.resolve().relative_to((get_default_hermes_root() / "profiles").resolve())
        return rel.parts[0] if rel.parts else "default"
    except (ValueError, OSError):
        return "default"


def _tool_guidance_block(agent: Any) -> Optional[str]:
    """Tool-aware behavioral guidance, injected only when the tools are loaded."""
    names = agent.valid_tool_names
    # With both memory stores disabled no store is built, so the full guidance
    # would steer the model at a tool that always answers "Memory is not
    # available"; with only USER.md enabled the narrower block is used.
    memory_guidance = None
    if "memory" in names:
        memory_guidance = _pb.build_memory_guidance(
            getattr(agent, "_memory_enabled", True),
            getattr(agent, "_user_profile_enabled", True),
            skill_manage_available="skill_manage" in names,
        )
    # Kanban lifecycle: resolved once at __init__ (_kanban_worker_guidance);
    # fallback paths must also limit task protocol guidance to dispatcher workers.
    _kanban_guidance = getattr(agent, "_kanban_worker_guidance", None)
    if _kanban_guidance is None and "kanban_show" in names and owned_kanban_task():
        _kanban_guidance = KANBAN_GUIDANCE
    tool_guidance = [
        memory_guidance,
        SESSION_SEARCH_GUIDANCE if "session_search" in names else None,
        SKILLS_GUIDANCE if "skill_manage" in names else None,
        _kanban_guidance,
    ]
    return " ".join(g for g in tool_guidance if g) or None


def _skills_prompt(agent: Any) -> str:
    """Skills index (empty without skills tools).  Focus mode demotes non-coding
    categories to names-only — never hidden, every name stays visible."""
    if not any(name in agent.valid_tool_names for name in ['skills_list', 'skill_view', 'skill_manage']):
        return ""
    import model_tools
    avail_toolsets = {model_tools.get_toolset_for_tool(tool_name) for tool_name in agent.valid_tool_names} - {None, ""}
    try:
        from agent.coding_context import coding_compact_skill_categories
        _compact_cats = coding_compact_skill_categories(platform=agent.platform, cwd=resolve_context_cwd())
    except Exception:
        _compact_cats = frozenset()
    return _pb.build_skills_system_prompt(available_tools=agent.valid_tool_names, available_toolsets=avail_toolsets,
                                         compact_categories=_compact_cats or None, skills_dir_override=_agent_skills_dir(agent))


def _auto_load_parts(agent: Any) -> list[str]:
    """``skills.auto_load`` blocks, resolved once per agent lifecycle (config, skill files and
    HERMES_IGNORE_RULES are read on the first build only) so the prompt stays byte-stable
    across model switches, compression and static-prefix restoration.

    Same gate as ``_skills_prompt``: nothing without the skills toolset, and nothing for agents that skip
    context files (delegate children, curator/review forks, gateway hygiene agents) — pinned skills are
    operator guidance for the user's session, not payload for every internal fork."""
    if getattr(agent, "skip_context_files", False) or not any(
            name in agent.valid_tool_names for name in ("skills_list", "skill_view", "skill_manage")):
        return []
    if not getattr(agent, "_auto_load_skills_resolved", False):
        result: tuple[str, list[str], list[str]] = ("", [], [])
        try:
            if not is_truthy_value(os.environ.get("HERMES_IGNORE_RULES")):
                from agent.skill_commands import build_auto_load_prompt
                result = build_auto_load_prompt(task_id=getattr(agent, "session_id", None), home_override=_agent_home(agent))
            if result[2]:
                logger.warning("skills.auto_load: skill(s) not found or disabled, skipped: %s", ", ".join(result[2]))
        except Exception:
            logger.debug("skills.auto_load: injection skipped", exc_info=True)  # config errors never block session start
        agent._auto_load_skills_result = result
        agent._auto_load_skills_resolved = True
    prompt = agent._auto_load_skills_result[0]
    return [prompt] if prompt else []


def _bot_mode_parts(agent: Any) -> list[str]:
    """Bot Mode teammate protocol — only in a bot's canonical "Bot Chat" session.
    Marks the prompt timeless (the volatile date line is dropped) since a birth
    date pinned in a months-long session is misinformation."""
    parts: list[str] = []
    try:
        from tools.bot_mode_probe import BOT_CHAT_TITLE, epoch_line, get_bot_mode_protocol_section
        _title = str(getattr(agent, "_session_title_hint", "") or "").strip()
        if not _title:
            _sdb = getattr(agent, "_session_db", None)
            _sid = getattr(agent, "session_id", None)
            _title = str((_sdb.get_session_title(_sid) if (_sdb and _sid) else None) or "").strip()
        _bot_section = get_bot_mode_protocol_section(_agent_home(agent)) if _title == BOT_CHAT_TITLE else None
        if _bot_section:
            parts.append(_bot_section)
            # Capability epoch lets the restore path rebuild ONCE per
            # user-initiated capability change in an eternal session.
            parts.append(epoch_line(_agent_home(agent)))
            agent._bot_chat_timeless_prompt = True
    except Exception:
        pass
    return parts


def _ambient_file_safety_profile_name() -> str:
    from agent.file_safety import _resolve_active_profile_name
    return _resolve_active_profile_name()


def _active_profile_line(agent: Any) -> str:
    """Name the running profile so the agent doesn't conflate ``~/.hermes/skills``
    (default) with ``~/.hermes/profiles/<active>/skills``.  Resolved from the
    agent's OWN home first (a build thread that lost the ContextVar would
    otherwise print "default" for a bot profile)."""
    _agent_home_path = _agent_home(agent)
    active_profile = _active_profile_name(agent, _ambient_file_safety_profile_name)
    if active_profile == "default":
        # With an explicit agent home, the default profile's data lives at the
        # ROOT (get_hermes_home() on a bound profile session is the PROFILE dir).
        # Without one, keep the ambient (patchable) resolution byte-identical.
        _root_str = str(get_default_hermes_root() if _agent_home_path is not None else get_hermes_home())
        return (
            "Active Hermes profile: default. Other profiles (if any) live "
            "under " + _root_str + "/profiles/<name>/. Each profile has its own "
            "skills/, plugins/, cron/, and memories/ that affect a different "
            "session than this one. Do not modify another profile's "
            "skills/plugins/cron/memories unless the user explicitly directs "
            "you to."
        )
    # A non-default name is only returned when the resolved home is ALREADY
    # <root>/profiles/<name>, so the profile home is the session home itself.
    profile_home = str(_agent_home_path) if _agent_home_path is not None else str(get_hermes_home())
    # A non-default name is only ever returned when the resolved home is ALREADY <root>/profiles/<name> —
    # that is exactly how both _profile_name_for_home() and _resolve_active_profile_name() derive it. So the
    # profile home is the session home itself; appending /profiles/<name> again doubled it (#72894). The
    # default profile's data sits at the ROOT (get_default_hermes_root()), which in ambient profile mode is
    # NOT get_hermes_home().
    default_root = get_default_hermes_root()
    return (
        f"Active Hermes profile: {active_profile}. This session reads "
        f"and writes {profile_home}/. The default "
        f"profile's data lives at {default_root}/skills/, {default_root}/plugins/, "
        f"{default_root}/cron/, {default_root}/memories/ — those belong to a "
        f"different session run from a different shell. Do NOT modify "
        f"another profile's skills/plugins/cron/memories unless the user "
        f"explicitly directs you to."
    )


def _default_platform_hint(platform_key: str) -> str:
    """Built-in hint, else the plugin adapter's ``platform_hint``, else ``""``."""
    hint = PLATFORM_HINTS.get(platform_key, "")
    if not hint and platform_key:
        try:
            from gateway.platform_registry import platform_registry
            _entry = platform_registry.get(platform_key)
            hint = (_entry and _entry.platform_hint) or ""
        except Exception:
            pass
    if platform_key == "telegram" and hint and _telegram_rich_messages_enabled():
        hint = hint.rstrip() + " " + TELEGRAM_RICH_MESSAGES_HINT
    return hint


def _cron_delivery_hint(agent: Any) -> str:
    """The destination channel's hint (default + its ``platform_hints`` override) for a cron agent.

    A cron agent runs as platform ``cron`` but its final response lands on the job's ``deliver``
    channel, so without this the model never learns that MEDIA: tags become Slack/Telegram
    attachments or that tables do not render there — and a user's ``platform_hints.slack.append``
    never reached scheduled jobs at all. The scheduler publishes the primary auto-deliver target
    into the session ContextVar before the agent runs (same seam ``send_message`` routes by).
    """
    from gateway.session_context import get_session_env
    deliver_key = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").lower().strip()
    if not deliver_key or deliver_key == "cron":
        return ""
    hint = _resolve_platform_hint(agent, deliver_key, _default_platform_hint(deliver_key))
    return f"Delivery destination ({deliver_key}): {hint}" if hint else ""


def platform_hint(agent: Any) -> str:
    """Built-in/plugin platform hint + Telegram rich-messages opt-in + config
    override + desktop TUI clarifier; cron agents also carry their delivery channel's hint."""
    platform_key = (agent.platform or "").lower().strip()
    _effective_hint = _resolve_platform_hint(agent, platform_key, _default_platform_hint(platform_key))
    if platform_key == "tui" and _effective_hint:
        _effective_hint = _tui_embedded_pane_clarifier(_effective_hint)
    if platform_key == "cron":
        _delivery = _cron_delivery_hint(agent)
        if _delivery:
            _effective_hint = f"{_effective_hint}\n\n{_delivery}".strip()
    return _effective_hint


def _telegram_rich_messages_enabled() -> bool:
    """``rich_messages`` from the Telegram ``extra`` config; same precedence the
    adapter uses (top-level ``platforms.telegram.extra`` overrides
    ``gateway.platforms.telegram.extra`` at the leaf). False on any read failure."""
    try:
        from hermes_cli.config import load_config_readonly
        _cfg = load_config_readonly()
        _gw = (((_cfg.get("gateway") or {}).get("platforms") or {}).get("telegram") or {}).get("extra")
        _top = ((_cfg.get("platforms") or {}).get("telegram") or {}).get("extra")
        merged = {**(_gw if isinstance(_gw, dict) else {}), **(_top if isinstance(_top, dict) else {})}
        return bool(merged.get("rich_messages"))
    except Exception:
        return False


def _zone_bits(now: Any, tz: Any) -> list[str]:
    """IANA key, abbreviation (if different) and UTC offset — all constant for
    the day, so the byte-stable date line stays cacheable."""
    _iana = getattr(tz, "key", None)
    from hermes_time import safe_strftime
    _abbrev = safe_strftime(now, "%Z")
    _offset = safe_strftime(now, "%z")  # '-0400' -> 'UTC-04:00'
    bits = [_iana] if _iana else []
    if _abbrev and _abbrev != _iana:
        bits.append(_abbrev)
    if _offset:
        bits.append(f"UTC{_offset[:3]}:{_offset[3:]}")
    return bits


def _timestamp_line(agent: Any) -> str:
    """Date-only so the prompt is byte-stable for the day; zone + offset so
    tools needn't guess EST vs EDT. Long-lived sessions get an "as of" line on
    rebuild days (the cache prefix is already invalidated at that boundary)."""
    from hermes_time import get_timezone as _hermes_tz, now as _hermes_now, safe_strftime
    now = _hermes_now()
    _bits = _zone_bits(now, _hermes_tz())
    _zone_suffix = f" ({', '.join(_bits)})" if _bits else ""
    _start = _session_start_like(agent, now)
    timestamp_line = f"Conversation started: {safe_strftime(_start, '%A, %B %d, %Y')}{_zone_suffix}"
    # Second line (maintainer design, salvaging #96224's anchor): long-lived sessions — Bot Mode
    # forever-chats, messenger channels people never close — span many days and many compactions. A lone
    # birth date leads the model to believe it is still living in that old day. The prompt is rebuilt at
    # every compaction boundary, so stamp the rebuild day too: 'started' stays anchored and byte-stable, 'as
    # of' refreshes exactly when the cache prefix is already being invalidated (compaction), so the added
    # line costs no extra cache churn. Same-day sessions skip the second line entirely — nothing to correct,
    # and the single-line shape stays byte-identical for the day (prefix-cache safe).
    if now.strftime("%Y%m%d") != _start.strftime("%Y%m%d"):
        timestamp_line += (f"\nToday's date (as of the last context rebuild): {safe_strftime(now, '%A, %B %d, %Y')} "
                           "— trust this over the start date for what day it is now; query tools for exact time.")
    if getattr(agent, "_bot_chat_timeless_prompt", False):
        timestamp_line = f"Timezone: {', '.join(_bits)}" if _bits else ""
    trailer = (("Session ID", agent.session_id if agent.pass_session_id else None), ("Model", agent.model),
               ("Provider", agent.provider), ("Platform", agent.platform))
    return timestamp_line + "".join(f"\n{label}: {value}" for label, value in trailer if value)


def _memory_parts(agent: Any) -> list[str]:
    """Built-in memory/USER.md blocks plus the external provider block (gated on
    the same check ``inject_memory_provider_tools`` uses, so we never advertise
    tools the toolset config gated off)."""
    parts: list[str] = []
    # Cross-session work-journal brief (P16: active todos from CLI/Feishu/cron
    # ends).  Volatile tier = rebuilt once per session, byte-stable after —
    # safe for prompt caching.  Fail-open; empty journal → no block.
    try:
        _wj_block = _build_work_journal_block(agent)
        if _wj_block:
            parts.append(_wj_block)
    except Exception:
        logger.debug("work-journal block skipped (fail-open)", exc_info=True)
    if agent._memory_store:
        for enabled, kind in ((agent._memory_enabled, "memory"), (agent._user_profile_enabled, "user")):
            block = agent._memory_store.format_for_system_prompt(kind) if enabled else None
            if block:
                parts.append(block)
    # External memory provider system prompt block (additive to built-in). Gated on the same check
    # ``inject_memory_provider_tools`` uses so we never advertise provider tools that the agent's toolset
    # configuration has already gated off (#81014).
    if agent._memory_manager:
        try:
            from agent.memory_manager import memory_provider_tools_exposed as _mem_exposed
        except Exception:
            _mem_exposed = None
        if _mem_exposed is None or _mem_exposed(agent):
            try:
                _ext_mem_block = agent._memory_manager.build_system_prompt()
            except Exception:
                _ext_mem_block = None
            if _ext_mem_block:
                parts.append(_ext_mem_block)
    return parts


def _identity_parts(agent: Any, ctx_len: Optional[int]) -> tuple[list[str], bool]:
    """SOUL.md (primary identity; cron keeps the persona while skipping cwd
    instructions, scoped to the agent's OWN home) or the default identity.
    Returns ``(parts, soul_loaded)``."""
    wants_soul = agent.load_soul_identity or not agent.skip_context_files
    _soul_content = _pb.load_soul_md(ctx_len, home_override=_agent_home(agent)) if wants_soul else None
    return ([_soul_content], True) if _soul_content else ([DEFAULT_AGENT_IDENTITY], False)


def _guidance_parts(agent: Any) -> list[str]:
    """Universal + tool-aware + model-gated guidance blocks, each gated by its config.yaml key."""
    parts: list[str] = []
    if agent.valid_tool_names:
        parts += [
            text for flag, text in (
                ("_task_completion_guidance", TASK_COMPLETION_GUIDANCE),
                ("_parallel_tool_call_guidance", PARALLEL_TOOL_CALL_GUIDANCE),
            ) if getattr(agent, flag, True)
        ]
    parts.append(_tool_guidance_block(agent))  # None/empty entries are dropped by _join_tier
    if not agent.valid_tool_names:
        return parts
    # Steering only lands inside tool results, so only reachable with tools.
    parts.append(STEER_CHANNEL_NOTE)
    # agent.tool_use_enforcement / agent.execution_guidance: "auto" (default)
    # matches the hardcoded model lists; true/false force; a list gives custom
    # model-name substrings.  Execution guidance is an independent gate so
    # DeepSeek/Kimi/Qwen-class models get it even with enforcement off.
    if _model_gate(agent._tool_use_enforcement, agent.model, TOOL_USE_ENFORCEMENT_MODELS):
        parts.append(TOOL_USE_ENFORCEMENT_GUIDANCE)
        if any(g in (agent.model or "").lower() for g in ("gemini", "gemma")):
            parts.append(GOOGLE_MODEL_OPERATIONAL_GUIDANCE)
    if _model_gate(getattr(agent, "_execution_guidance", "auto"), agent.model, EXECUTION_GUIDANCE_MODELS):
        from agent.prompt_builder import execution_guidance_text
        parts.append(execution_guidance_text(agent.valid_tool_names))
    return parts


def _alibaba_identity_part(agent: Any) -> list[str]:
    """Alibaba Coding Plan always reports "glm-4.7" as the model name; inject
    the real identity so the agent can answer correctly."""
    if agent.provider != "alibaba":
        return []
    _model_short = agent.model.rsplit("/", 1)[-1]
    return [
        f"You are powered by the model named {_model_short}. "
        f"The exact model ID is {agent.model}. "
        f"When asked what model you are, always answer based on this information, "
        f"not on any model name returned by the API."
    ]


def _workspace_pin_key() -> str:
    """The directory the workspace probe inspects, which is also the prompt's ``Current working
    directory``: a build with no cwd bound (launch dir) and a later one binding that same dir
    (TUI ``/compress``) are one workspace, not two."""
    try:
        return str(resolve_context_cwd() or resolve_agent_cwd())
    except OSError:  # deleted cwd
        return ""


def _persisted_workspace_block(prompt: str, key: str) -> Optional[str]:
    """The workspace snapshot inside ``prompt`` taken for ``key`` (its ``- Root:`` is ``key`` or an
    ancestor); "" when the prompt has none; None when it has one for another root."""
    from agent.coding_context import WORKSPACE_BLOCK_HEADER
    head = f"\n\n{WORKSPACE_BLOCK_HEADER}\n- Root: "
    start = prompt.find(head)
    if start < 0:
        return ""
    cwd = Path(key).resolve()
    while start >= 0:
        block = prompt[start + 2:].split("\n\n", 1)[0]
        root = Path(block.split("\n", 2)[1][len("- Root: "):]).resolve()
        if root == cwd or root in cwd.parents:
            return block
        start = prompt.find(head, start + 2)
    return None


def _session_prompt(agent: Any) -> Optional[str]:
    """Prompt bytes this session already sends: the cached copy, else its persisted row."""
    cached = getattr(agent, "_cached_system_prompt", None)
    if isinstance(cached, str) and cached:
        return cached
    db, session_id = getattr(agent, "_session_db", None), getattr(agent, "session_id", None)
    if db is None or not isinstance(session_id, str) or not session_id:
        return None
    try:
        row = db.get_session(session_id)
    except Exception:
        logger.debug("workspace snapshot: session row read failed (session=%s)", session_id, exc_info=True)
        return None
    prompt = row.get("system_prompt") if isinstance(row, dict) else None
    return prompt if isinstance(prompt, str) and prompt else None


def _seed_workspace_pin(agent: Any, key: str) -> None:
    """Pin the snapshot the session's existing prompt already carries.  An agent that did not
    build those bytes (resumed, or a fresh gateway/TUI agent whose first act is ``/compress``)
    would otherwise re-probe git at its first rebuild and rewrite the prompt for any repo that
    moved since session start.  Only a snapshot provably taken in this cwd is adopted."""
    from agent.surface_switch import runtime_host_value
    prompt = _session_prompt(agent)
    if not prompt:
        return
    stored_cwd = runtime_host_value(prompt, "Current working directory")
    if stored_cwd and stored_cwd != key:
        return
    block = _persisted_workspace_block(prompt, key)
    # Only a real snapshot is adopted: a prompt without one (built on a surface without the
    # coding posture, or with tools off) leaves the pin open so this build captures one.
    if block:
        agent._frozen_workspace_snapshot = (key, block)


def _coding_parts(agent: Any) -> tuple[list[str], list[str], list[str]]:
    """``(prefix, workspace, trailing)`` coding-posture blocks; all empty
    without tools or when probing fails (it must never block prompt build).

    The workspace block is a live git probe after project context, ahead of the whole
    volatile band; re-probing at the compaction rebuild re-emits different bytes for any
    repo that moved and defeats the keep-prompt fast path.  So the bytes are pinned per
    session on the agent, keyed by the probed cwd (a gateway serves many cwds), seeded from
    the session's existing prompt when this agent did not build it, and replayed on
    rebuilds; ``reset_session_state`` drops the pin at a session boundary.
    """
    try:
        from agent.coding_context import coding_system_prompt_parts
        if not agent.valid_tool_names:
            return [], [], []
        cwd = resolve_context_cwd()
        cwd_key = _workspace_pin_key()
        if getattr(agent, "_frozen_workspace_snapshot", None) is None:
            _seed_workspace_pin(agent, cwd_key)
        pinned = getattr(agent, "_frozen_workspace_snapshot", None)
        # "" is a real pinned value (no workspace here) — only a cwd mismatch re-probes.
        replay = pinned[1] if pinned is not None and pinned[0] == cwd_key else None
        parts = coding_system_prompt_parts(platform=agent.platform, cwd=cwd, model=agent.model,
                                           valid_tool_names=agent.valid_tool_names, workspace_block=replay)
        if replay is None:
            agent._frozen_workspace_snapshot = (cwd_key, parts[1][0] if parts[1] else "")
        return parts
    except Exception:
        pass
    return [], [], []


def _post_workspace_parts(agent: Any) -> list[str]:
    """Blocks that follow the worktree-specific context: environment probe
    (config.yaml agent.environment_probe; one line, nothing when clean, skipped
    for remote backends), bot-mode protocol, platform hint."""
    parts: list[str] = []
    if getattr(agent, "_environment_probe", True):
        try:
            from tools.env_probe import get_environment_probe_line
            parts.append(get_environment_probe_line())
        except Exception:
            pass  # Probe failure must never block prompt build.
    if getattr(agent, "_bot_mode_protocol", True):
        parts.extend(_bot_mode_parts(agent))
    parts.append(platform_hint(agent))
    return parts


def _context_files_part(agent: Any, ctx_len: Optional[int], soul_loaded: bool) -> list[str]:
    """Project context files (AGENTS.md etc.) for the context tier. TERMINAL_CWD
    when set (gateway); None lets discovery fall back to the launch dir.  The
    install-tree fallback is only legitimate for cli/tui where the launch dir
    IS the user's shell cwd. Desktop launch artifacts skip the session cwd but
    still honor the profile-scoped TERMINAL_CWD; without one, the fallback guard
    can reject Hermes's bundled AGENTS.md."""
    if agent.skip_context_files:
        return []
    launch_artifact = getattr(agent, "_context_cwd_is_launch_artifact", False)
    cwd = resolve_context_cwd(include_session_override=not launch_artifact)
    return [_pb.build_context_files_prompt(
        cwd=cwd, skip_soul=soul_loaded, context_length=ctx_len,
        allow_install_tree_fallback=agent.platform in ("cli", "tui"), home_override=_agent_home(agent))]


def _join_tier(parts: list[Optional[str]]) -> str:
    """Join non-empty parts; None/blank entries are dropped."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def build_system_prompt_parts(agent: Any, system_message: Optional[str] = None) -> dict[str, str]:
    """Assemble the system prompt as three ordered cache tiers: ``stable`` (identity,
    guidance and the coding brief), ``context`` (caller ``system_message``, project
    context files, workspace snapshot and remaining workspace guidance) and
    ``volatile`` (skills index, memory, user profile, external memory block,
    timestamp line, runtime environment hints).  Worktree-dependent blocks follow project context so a
    shared context file can remain in the longest common prefix across worktrees.
    Never re-rendered mid-session."""
    # Model context window scales the context-file caps; stable per conversation.
    _cc_len = getattr(getattr(agent, "context_compressor", None), "context_length", None)
    _ctx_len = _cc_len if isinstance(_cc_len, int) and _cc_len > 0 else None
    # ── Stable tier ────────────────────────────────────────────────
    stable_parts, _soul_loaded = _identity_parts(agent, _ctx_len)
    # The skill_view() pointer dangles without skill tools OR without the
    # hermes-agent skill installed, so the variant is chosen after the skills
    # index is built; this slot holds its position.
    _help_guidance_slot = len(stable_parts)
    stable_parts.append(HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS)
    stable_parts.extend(_guidance_parts(agent))
    skills_prompt = _skills_prompt(agent)
    # Skill-pointer variant requires BOTH skill_view AND the hermes-agent skill
    # in the rendered index (pure string check — inherits the index's stability).
    if "skill_view" in (agent.valid_tool_names or set()) and "- hermes-agent:" in skills_prompt:
        stable_parts[_help_guidance_slot] = HERMES_AGENT_HELP_GUIDANCE
    stable_parts.extend(_alibaba_identity_part(agent))
    # Pinned skills are per-agent constants (resolved once), so they live in the stable prefix.
    stable_parts.extend(_auto_load_parts(agent))
    # Coding posture: the operating brief stays in the stable prefix. The
    # environment block contains the current cwd/backend and belongs after
    # project context, not ahead of a large shared AGENTS.md block.
    environment_hints = _pb.build_environment_hints()
    coding_prefix_parts, coding_workspace_parts, coding_trailing_parts = _coding_parts(agent)
    stable_parts.extend(coding_prefix_parts)
    post_workspace_parts = _post_workspace_parts(agent)
    # ── Context tier (project/worktree-dependent, may change between sessions) ──
    context_parts: list[str] = []
    # ephemeral_system_prompt is injected at API-call time only, never cached.
    if system_message is not None:
        context_parts.append(system_message)
    context_parts.extend(_context_files_part(agent, _ctx_len, _soul_loaded))
    if coding_workspace_parts:
        context_parts.extend([*coding_workspace_parts, *coding_trailing_parts, *post_workspace_parts])
    else:
        # Preserve the stable placement for non-workspace sessions; there is no
        # worktree snapshot whose later position would improve their prefix.
        stable_parts.extend([*coding_trailing_parts, *post_workspace_parts])
    # ── Volatile tier (most likely to differ on a rebuild; kept last so the stable prefix stays reusable) ──
    # Skills are runtime-mutable, so the index leads the volatile band: on a longest-prefix
    # backend an unchanged index stays inside the reused prefix; a changed one re-prefills from here.
    volatile_parts: list[str] = [skills_prompt, *_memory_parts(agent)]
    # Plugin sections are confined to one coarse anchor in the volatile tail so
    # a resumed process can reconstruct the stable prefix without re-running plugins.
    volatile_parts.extend(_plugin_section_blocks(_frozen_plugin_prompt_sections(agent), "after_memory"))
    # The profile line names this home's path, so it rides in the volatile tier: the stable
    # prefix then stays byte-identical across every profile (and home) on the host.
    volatile_parts.append(_active_profile_line(agent))
    volatile_parts.append(_timestamp_line(agent))
    # Keep the renderer-owned runtime anchor after all user/plugin prose so quoted
    # host examples cannot shadow it during persisted-prompt validation.
    if environment_hints:
        # Embedder hints are prose too; reserve the delimiter for the renderer.
        environment_hints = environment_hints.replace(_pb.RUNTIME_ENVIRONMENT_HEADING, "> " + _pb.RUNTIME_ENVIRONMENT_HEADING)
        volatile_parts.append(f"{_pb.RUNTIME_ENVIRONMENT_HEADING}\n\n{environment_hints}\n\n{_pb.RUNTIME_ENVIRONMENT_END}")
    return {"stable": _join_tier(stable_parts), "context": _join_tier(context_parts), "volatile": _join_tier(volatile_parts)}


def build_system_prompt(agent: Any, system_message: Optional[str] = None) -> str:
    """Assemble the full prompt; cached on ``agent._cached_system_prompt`` and
    only rebuilt after compression.  Tiers are ordered stable -> context ->
    volatile so implicit longest-prefix caches keep the unchanged scaffold."""
    parts = build_system_prompt_parts(agent, system_message=system_message)
    agent._cached_system_prompt_static = parts["stable"]
    # Surface context-file truncation warnings in chat, not only in logs.
    for warning in drain_truncation_warnings():
        agent._emit_diagnostic_status(warning)
    return "\n\n".join(p for p in (parts["stable"], parts["context"], parts["volatile"]) if p)


def invalidate_system_prompt(agent: Any) -> None:
    """Force a rebuild on the next turn (after compression): reload memory from
    disk and clear the frozen plugin snapshot (previous bytes stashed as the
    fail-open fallback) so plugins re-render at the same boundary.

    Called after context compression events. Also reloads memory from disk so the rebuilt prompt captures
    any writes from this session, and clears the frozen plugin-section snapshot so plugins re-render at the
    same boundary (maintainer-directed, #95681 arc): a plugin section is just another prompt block carrying
    state — freezing it while memory, skills, and guidance refresh would recreate the stale-block disease
    inside plugin-land. The previous bytes are stashed so a plugin whose render RAISES falls back to its
    last good section instead of vanishing (fail-open guard, not a freeze).
    """
    agent._cached_system_prompt = None
    agent._cached_system_prompt_static = None
    if hasattr(agent, "_plugin_system_prompt_sections_snapshot"):
        agent._plugin_system_prompt_sections_previous = agent._plugin_system_prompt_sections_snapshot
        del agent._plugin_system_prompt_sections_snapshot
    if agent._memory_store:
        agent._memory_store.load_from_disk()


def reconstruct_static_prefix(agent: Any, system_message: Optional[str] = None, *, log_label: str = "restore") -> None:
    """Reconstruct ``_cached_system_prompt_static`` for a stored prompt.
    Only the full prompt is persisted, so restore / keep-prompt compression /
    mid-turn failover to a cache-on provider must rebuild the stable tier to
    regain the ``[static, volatile]`` layout.  The rebuilt tier is used ONLY
    when the stored prompt literally starts with it; otherwise static stays
    None and the stored bytes are sent untouched.  A failed rebuild is memoized
    per stored prompt so the retry-loop hot path doesn't redo the file I/O."""
    stored = getattr(agent, "_cached_system_prompt", None)
    existing = getattr(agent, "_cached_system_prompt_static", None)
    if (
        not getattr(agent, "_use_prompt_caching", False)
        or not isinstance(stored, str) or not stored
        or (isinstance(existing, str) and existing and stored.startswith(existing))
        or getattr(agent, "_static_rebuild_failed_for", None) == stored
    ):
        return
    try:
        static = build_system_prompt_parts(agent, system_message=system_message)["stable"]
        if static and stored.startswith(static):
            agent._cached_system_prompt_static = static
            agent._static_rebuild_failed_for = None
            return
    except Exception:
        logger.debug("static system-prefix reconstruction failed on %s", log_label, exc_info=True)
    agent._cached_system_prompt_static = None
    agent._static_rebuild_failed_for = stored


def format_tools_for_system_message(agent: Any) -> str:
    """JSON tool definitions in the trajectory format."""
    if not agent.tools:
        return "[]"
    return json.dumps([{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                        "parameters": t["function"].get("parameters", {}),
                        "required": None}  # Match the format in the example
                       for t in agent.tools], ensure_ascii=False)


__all__ = [
    "build_system_prompt",
    "build_system_prompt_parts",
    "format_tools_for_system_message",
    "invalidate_system_prompt",
    "platform_hint",
    "restore_plugin_prompt_sections",
]
