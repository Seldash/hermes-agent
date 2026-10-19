"""Bridge: in-memory builtin ``todo`` tool <-> persistent work journal.

The builtin todo tool (``tools/todo_tool.py``) keeps its list in memory on
the AIAgent instance — one per session, lost when the session ends.  The
cross-session work journal (``<HERMES_HOME>/data/.work_journal.json``,
written by ``log_session_work.py``) is the durable TODO + work-log store
shared across CLI / Feishu / cron.

This module mirrors the builtin todo list into the journal's dedicated
``builtin_todos`` section after every write, so a session's task plan is
not lost when the session ends.  It deliberately does NOT touch the
journal's main ``todos`` array — that space is reserved for cross-end
items with when/depends_on semantics; a session-scoped task plan would
pollute it.  The mirror is fail-open: any read/write error is logged at
debug and never breaks the agent loop.

Design constraints (2026-08-07, B1):
- Core-waist: ``tools/todo_tool.py`` and the tool schema are untouched.
  The only hook is a two-line post-write call in
  ``agent/agent_runtime_helpers.py`` (the todo branch of ``invoke_tool``).
- Config-gated: ``agent.todo_mirror_to_journal`` (default True).
- The mirror is write-only from the agent's perspective.  Restoration is
  intentionally NOT automatic — a session-scoped plan from a previous
  session is stale by definition; agents that want it can read the
  ``builtin_todos`` section explicitly.
"""

from __future__ import annotations

import fcntl
import json
import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MIRROR_KEY = "builtin_todos"


@contextmanager
def _locked():
    """跨进程互斥锁，与 scripts/log_session_work.py 同协议、同锁路径。

    journal 用 write-then-rename 原子替换，锁不能放在 journal 文件上
    （rename 换 inode 会丢锁），必须用独立的 .work_journal.lock。
    gateway 内 todo 工具镜像 与 wj CLI / cron 三路并发写由此串行化。
    """
    from hermes_constants import get_hermes_home
    lock_path = get_hermes_home() / "data" / ".work_journal.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def _journal_path():
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "data" / ".work_journal.json"


def _read_journal() -> Dict[str, Any]:
    path = _journal_path()
    if not path.exists():
        return {"log": [], "todos": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"log": [], "todos": []}


def _write_journal(data: Dict[str, Any]) -> None:
    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def mirror_builtin_todos(agent: Any) -> bool:
    """Mirror the agent's in-memory todo list into the journal.

    Call after a builtin todo write (see invoke_tool in
    agent/agent_runtime_helpers.py).  Fail-open: returns False on any
    error without raising.  No-op when the config gate is off.
    """
    if not getattr(agent, "_todo_mirror_to_journal", True):
        return False
    store = getattr(agent, "_todo_store", None)
    if store is None:
        return False
    try:
        with _locked():
            items = store.read()
            data = _read_journal()
            data.setdefault(_MIRROR_KEY, [])
            data[_MIRROR_KEY] = items
            data["updated_at"] = __import__("datetime").datetime.now().isoformat()
            _write_journal(data)
        return True
    except Exception:
        logger.debug("builtin todo mirror failed (fail-open)", exc_info=True)
        return False


def read_builtin_todos() -> List[Dict[str, str]]:
    """Read the persisted builtin todo mirror (for explicit restoration)."""
    try:
        data = _read_journal()
        mirror = data.get(_MIRROR_KEY, [])
        return mirror if isinstance(mirror, list) else []
    except Exception:
        return []
