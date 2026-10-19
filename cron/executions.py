"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue. Interrupted attempts
become ``unknown`` only after their owner process is proved gone — a start-time reading that fails
to match the claim-time fingerprint is not proof of death. Terminal states are immutable.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from cron.constants import CLAIM_TTL_INACTIVITY_HEADROOM
from hermes_cli.observability.shared_metrics_gateway import record_cron_finish

logger = logging.getLogger(__name__)

# Optional test override. Production resolves the path at transaction time so dashboard operations
# that temporarily enter another profile cannot leak that profile's records into the import-time
# home.
EXECUTIONS_FILE: Optional[Path] = None
# 2026-09-09 审计 D3-P1-1 恢复: 运行时 env 覆盖曾于 2026-08-10 落地, 被后续升级覆盖丢失
# → 保留窗口硬编码回归 1000 行(≈6.5h), interrupted_detector 24h 窗口/长停机恢复失效。
# .env 与 systemd drop-in 均配 HERMES_CRON_EXECUTIONS_MAX=30000（2026-10-12 实测 .env; 注释旧值 10000 已过期）。
# import 时读 env → 模块属性: 运行时生效且保留测试 monkeypatch 可注入性(勿改回函数内 getenv)
MAX_TERMINAL_EXECUTIONS = int(os.getenv("HERMES_CRON_EXECUTIONS_MAX", "1000"))
HANDOFF_ADOPTION_GRACE_SECONDS = 30.0
# Floor for the live-owner stale-claim bound (#115692); see _live_owner_stale_after_seconds.
LIVE_OWNER_STALE_CLAIM_FLOOR_SECONDS = 7200.0



# 2026-09-27 待办 677072 分级保留 + prune 节流（本地补丁, 见 CUSTOM_PATCHES.md P9 段的 09-27 扩写）。
# 病: 旧的「只保留最新 N 条终端行」是**全局配额**——单个分钟级 job(2e3be3f33c61)实测占 40.6%,
#     把周/月级任务的派发记录一并挤出(10000 行 = 2.86 天, cron_missed_detector 需靠产出文件兜底,
#     待办 250244 的 30/191 缺口即此)。且每次终态写入都做一次全表排序删(EXPLAIN: TEMP B-TREE)。
# 改: ①失败/未知行按时间保留(审计价值最高、体积可忽略 ~8.5 行/天)
#     ②成功行: 时间下限内全留 + 每个 job 至少保留 EXEC_PER_JOB_KEEP 条 + 全局硬顶 MAX_TERMINAL_EXECUTIONS
#     ③硬顶序(2026-10-12 待办 846814 修订, 与上游 PR #124830 同语义): 先回收各 job 超出保底的溢出
#       （最旧优先）, 仅当所有 job 都已在保底内才退回全局最旧兜底; 硬顶仍按 completed 度量
#     ④prune 节流: 未超硬顶时按间隔/累计次数执行(超硬顶立即执行, 保住上游契约测试语义)
EXEC_SUCCESS_FLOOR_DAYS = float(os.getenv("HERMES_CRON_SUCCESS_FLOOR_DAYS", "7"))
EXEC_FAILURE_RETENTION_DAYS = float(os.getenv("HERMES_CRON_FAILURE_RETENTION_DAYS", "30"))
EXEC_PER_JOB_KEEP = int(os.getenv("HERMES_CRON_PER_JOB_KEEP", "200"))
PRUNE_MIN_INTERVAL_SECONDS = float(os.getenv("HERMES_CRON_PRUNE_MIN_INTERVAL_SECONDS", "60"))
PRUNE_EVERY_N_FINISHES = int(os.getenv("HERMES_CRON_PRUNE_EVERY_N_FINISHES", "20"))
# 节流状态（进程内; prune 失败不影响写入）
_prune_state: Dict[str, float] = {"last": 0.0, "finishes": 0}
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex
logger = logging.getLogger(__name__)


# --- executions ledger --------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    # Late imports: a scheduler daemon that outlives an on-disk upgrade already has the OLD
    # ``hermes_cli.sqlite_util`` / ``cron.jobs`` cached, so new names must be resolved at call time,
    # not at import time (the guarantee cron/ledger.py used to carry, see e24c8499).
    from cron.jobs import _ensure_cron_dir
    from hermes_cli.sqlite_util import open_db

    path = EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")
    _ensure_cron_dir(path.parent)
    return open_db(path, db_label="cron/executions.db", synchronous_full=True, initialize=_initialize_schema)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_cli.sqlite_util import add_column_if_missing

    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             handoff_pending INTEGER NOT NULL DEFAULT 0,
             handoff_started_at REAL,
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    add_column_if_missing(
        conn, "executions", "handoff_pending",
        "handoff_pending INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        conn, "executions", "handoff_started_at", "handoff_started_at REAL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    add_column_if_missing(conn, "executions", "delivery_outcome", "delivery_outcome TEXT")
    add_column_if_missing(conn, "executions", "scheduled_instant", "scheduled_instant TEXT")
    add_column_if_missing(conn, "executions", "progress_at", "progress_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_occurrence "
        "ON executions(job_id, scheduled_instant) WHERE status='completed'"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    from hermes_cli.sqlite_util import transaction

    with _lock, transaction(_connect()) as conn:
        yield conn


def _fetch(conn: sqlite3.Connection, execution_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    return dict(row) if row is not None else None


def _emit_execution_state(
    record: Optional[dict[str, Any]], *, delivery_outcome: Optional[str] = None
) -> None:
    """Project durable state to monitoring without affecting ledger behavior."""
    try:
        from agent.monitoring.cron_health import emit_execution_state

        emit_execution_state(record, delivery_outcome=delivery_outcome)
    except Exception:
        pass


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    if current is None:
        return True  # cannot compare -> cannot prove death; a misread must not rewrite state
    # Drifted same-host readings (#117505) are not proof of death; a live misread is still
    # bounded by the stale-claim sweep below.
    from gateway.status import start_time_fingerprints_match
    return start_time_fingerprints_match(started_at, current)


def _live_owner_stale_after_seconds() -> Optional[float]:
    """Age past which a claimed/running row with a LIVE owner is treated as wedged.

    Derived from the existing knobs, never a bare wall-clock constant:
    ``max(3 × HERMES_CRON_TIMEOUT, cron script timeout, 7200)``. Returns ``None`` (never reclaim
    live owners — today's behaviour) when the inactivity timeout is 0/unlimited or not a finite
    positive number: with no bound to derive from, fail closed.
    """
    from cron.scheduler import _cron_inactivity_seconds
    from cron.scheduler_script import _get_script_timeout

    inactivity = float(_cron_inactivity_seconds())
    if not math.isfinite(inactivity) or inactivity <= 0:
        return None
    return max(
        inactivity * CLAIM_TTL_INACTIVITY_HEADROOM,
        float(_get_script_timeout()),
        LIVE_OWNER_STALE_CLAIM_FLOOR_SECONDS,
    )


def _claim_age_seconds(claimed_at: str) -> float:
    """Seconds since ``claimed_at`` (NOT NULL, always the aware ISO string from hermes_time.now)."""
    return (_hermes_now() - datetime.fromisoformat(claimed_at)).total_seconds()


def _stale_age_seconds(claimed_at: str, progress_at: Optional[str]) -> float:
    """Seconds since the owner last proved it was making progress.

    A wedged worker (#115692) stops stamping ``progress_at``; a long but healthy run keeps
    stamping it, so the derived stale bound measures silence, not run length. Rows written
    before the column existed have no stamp and fall back to claim age.
    """
    return _claim_age_seconds(progress_at or claimed_at)


def touch_execution_progress(execution_id: str) -> bool:
    """Stamp ``progress_at`` on a running attempt this process owns. Returns False when the row
    is no longer ours or no longer running (the caller treats that as informational only)."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET progress_at=?
               WHERE id=? AND status IN ('claimed','running') AND process_id=? AND pid=?""",
            (now, execution_id, _PROCESS_ID, os.getpid()),
        )
        return cur.rowcount == 1


def _prune_unlocked(conn: sqlite3.Connection, *, force: bool = False) -> None:
    """分级保留（2026-09-27 待办 677072）:

    ① failed/unknown: 保留 EXEC_FAILURE_RETENTION_DAYS 天（按时间, 不受配额影响）
    ② completed: 时间下限 EXEC_SUCCESS_FLOOR_DAYS 内全留; 超出者每个 job 至少留
       EXEC_PER_JOB_KEEP 条; 再受全局硬顶 MAX_TERMINAL_EXECUTIONS 约束
    ③ 非终态(claimed/running) 永不删
    节流: 未超硬顶时, 距上次 prune < PRUNE_MIN_INTERVAL_SECONDS 且累计终态写入
    < PRUNE_EVERY_N_FINISHES 则跳过; 超硬顶立即执行。
    """
    from datetime import timedelta

    total = conn.execute(
        "SELECT COUNT(*) FROM executions "
        "WHERE status IN ('completed','failed','unknown')"
    ).fetchone()[0]
    over_cap = int(total) > max(0, int(MAX_TERMINAL_EXECUTIONS))
    if not force and not over_cap:
        _prune_state["finishes"] += 1
        due = time.monotonic() - _prune_state["last"] >= PRUNE_MIN_INTERVAL_SECONDS
        if not due and _prune_state["finishes"] < PRUNE_EVERY_N_FINISHES:
            return
    _prune_state["last"] = time.monotonic()
    _prune_state["finishes"] = 0

    now = _hermes_now()
    cut_success = (now - timedelta(days=EXEC_SUCCESS_FLOOR_DAYS)).isoformat()
    cut_failure = (now - timedelta(days=EXEC_FAILURE_RETENTION_DAYS)).isoformat()

    # ① 超期失败/未知行
    conn.execute(
        """DELETE FROM executions
           WHERE status IN ('failed','unknown')
             AND COALESCE(finished_at, claimed_at) < ?""",
        (cut_failure,),
    )
    # ② 成功行: 超时间下限 且 本 job 排名超出保底条数
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM (
               SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY job_id
                        ORDER BY finished_at DESC, claimed_at DESC, id DESC) AS rn
               FROM executions WHERE status='completed'
             ) WHERE rn > ?
           ) AND COALESCE(finished_at, claimed_at) < ?""",
        (max(0, int(EXEC_PER_JOB_KEEP)), cut_success),
    )
    # ③ 全局硬顶（安全阀）: 只作用于 completed（失败行不受硬顶影响, 既有契约不变量）。
    #    先回收各 job 超出保底的溢出（最旧优先）; 仅当所有 job 都已在保底内仍超顶时,
    #    才退回「全局最旧」兜底（保底让位于硬顶）。
    #    2026-10-12 待办 846814 / 上游 PR #124830 同语义修订:
    #      旧实现 = 「全局最旧 OFFSET 硬顶」, 不带 per-job 保底 ⇒ 高频 job 顶到硬顶后,
    #      低频 job 的行（最老）被整体抹掉（演练: 3 个周级 job 的 200 条保底行实剩 0 条）,
    #      本模块自述的「每个 job 至少保留 EXEC_PER_JOB_KEEP 条」在硬顶绑定下成为 no-op。
    #    保留的既有口径: 硬顶按 completed 度量、修剪只动 completed（⑥ 失败/非终态行不变量）。
    completed_total = conn.execute(
        "SELECT COUNT(*) FROM executions WHERE status='completed'"
    ).fetchone()[0]
    excess = int(completed_total) - max(0, int(MAX_TERMINAL_EXECUTIONS))
    if excess > 0:
        # 3a 各 job 超保底的 completed 溢出（最旧优先）: 只 shed 必要的量, 不碰别人的保底
        conn.execute(
            """DELETE FROM executions WHERE id IN (
                 SELECT id FROM (
                   SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY job_id
                            ORDER BY finished_at DESC, claimed_at DESC, id DESC) AS rn
                   FROM executions WHERE status='completed'
                 ) WHERE rn > ?
                 ORDER BY COALESCE(finished_at, claimed_at) ASC, id ASC
                 LIMIT ?
               )""",
            (max(0, int(EXEC_PER_JOB_KEEP)), excess),
        )
        # 3b 兜底: 所有 job 都已在保底内仍超顶 → 全局最旧的 completed 行（保底让位于硬顶）
        still = conn.execute(
            "SELECT COUNT(*) FROM executions WHERE status='completed'"
        ).fetchone()[0] - max(0, int(MAX_TERMINAL_EXECUTIONS))
        if still > 0:
            conn.execute(
                """DELETE FROM executions WHERE id IN (
                     SELECT id FROM executions WHERE status='completed'
                     ORDER BY COALESCE(finished_at, claimed_at) ASC, id ASC LIMIT ?
                   )""",
                (int(still),),
            )


def create_execution(
    job_id: str, *, source: str, scheduled_instant: Optional[str] = None,
) -> dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    from cron.occurrences import scheduled_instant as canonical_instant

    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, scheduled_instant)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now, canonical_instant(scheduled_instant)),
        )
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record  # type: ignore[return-value]


def set_execution_occurrence(execution_id: str, instant: Optional[str]) -> None:
    """Bind the store-claimed snapshot before a provider hands it to a worker."""
    from cron.occurrences import scheduled_instant

    with _transaction() as conn:
        cur = conn.execute(
            "UPDATE executions SET scheduled_instant=? WHERE id=? AND status='claimed' "
            "AND handoff_pending=0 AND process_id=? AND pid=?",
            (scheduled_instant(instant), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Cron occurrence could not be bound before dispatch")


def mark_execution_handoff_pending(execution_id: str) -> Optional[dict[str, Any]]:
    """Fence restart recovery while an external worker is adopting a claim."""
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET handoff_pending=1, handoff_started_at=?
               WHERE id=? AND status='claimed'
                 AND process_id=? AND pid=?""",
            (time.time(), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def adopt_claimed_execution(execution_id: str) -> Optional[dict[str, Any]]:
    """Atomically transfer and start an attempt in its worker process.

    The dispatching gateway creates the row before spawning a restart-safe
    worker.  Adoption is the single ``claimed`` → ``running`` gate: only the
    winner may acknowledge ownership or run side effects.
    """
    pid = os.getpid()
    process_started_at = _process_start_time(pid)
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET process_id=?, pid=?, process_started_at=?,
                   status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=1""",
            (_PROCESS_ID, pid, process_started_at, now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def mark_execution_running(execution_id: str) -> Optional[dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=0
                 AND process_id=? AND pid=?""",
            (now, execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    delivery_outcome: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, error=?, handoff_pending=0,
                   handoff_started_at=NULL, delivery_outcome=?
               WHERE id=? AND status IN ('claimed','running')
                 AND process_id=? AND pid=?""",
            (status, now, detail, delivery_outcome, execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _fetch(conn, execution_id)
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    record_cron_finish(record, delivery_outcome)
    return record


_OWNER_GONE_REASON = (
    "Scheduler restarted after this execution's owner exited before a durable "
    "terminal state; whether side effects ran is unknown."
)
_OWNER_WEDGED_REASON = (
    "Owner process is still alive but the claim outlived the derived stale bound; "
    "treated as wedged (#115692). The process was not terminated; whether side effects "
    "ran is unknown."
)
#: Persisted job status for "this attempt ran under an owner that then died". Deliberately NOT
#: ``error``: nothing is known to have failed, and the stale-error re-arm path
#: (``cron.jobs._job_is_stale_error_recurring``) must not treat an interrupted attempt as a wedged
#: failure and shift ``next_run_at`` under a job that is still legitimately scheduled.
INTERRUPTED_JOB_STATUS = "unknown"


def _iso_after(later: Any, earlier: Any) -> bool:
    """True when both ISO-8601 timestamps parse and *later* is strictly after *earlier*."""
    try:
        return datetime.fromisoformat(str(later)) > datetime.fromisoformat(str(earlier))
    except (TypeError, ValueError):
        return False


def _record_owner_loss(record: Dict[str, Any]) -> None:
    """Account durably for one attempt whose owner died before a terminal state was written.

    The ledger row alone is near-invisible: without this, ``hermes cron incidents`` carried
    nothing an operator could acknowledge, and the job kept its PREVIOUS run's ``last_status``
    (usually ``ok``) until its next fire — so a killed run read as "nothing happened". Two stores
    are updated, both best-effort: recovery runs inside the gateway tick and must never abort the
    sweep on a store hiccup.

    * an incident under the same error signature the in-process failure path uses, so an operator
      ``ack`` (``closed``) suppresses repeat alerts for that loss exactly as for a failed run;
    * ``last_status='unknown'`` + the reason on the job, so ``hermes cron list`` / doctor report
      the interrupted attempt instead of the last green one.

    A job that already completed a NEWER attempt keeps its newer status: the loss is recorded for
    the ledger/incident trail, but not resurrected as the job's current state.
    """
    job_id = str(record.get("job_id") or "")
    if not job_id:
        return
    error = str(record.get("error") or _OWNER_GONE_REASON)
    job: Optional[Dict[str, Any]] = None
    try:
        from cron.jobs import get_job

        job = get_job(job_id)
    except Exception as exc:
        logger.debug("Job store unreadable while accounting for interrupted %s: %s", job_id, exc)
    superseded = bool(job) and job.get("last_status") in ("ok", "completed") and _iso_after(
        job.get("last_run_at"), record.get("started_at") or record.get("claimed_at")
    )
    if not superseded:
        try:
            from cron.incidents import upsert_incident

            upsert_incident(job_id, error, job_name=(job or {}).get("name"))
        except Exception as exc:
            logger.debug("Incident store unavailable for interrupted %s: %s", job_id, exc)
    if not job or superseded:
        return
    try:
        from cron.jobs import update_job

        update_job(job_id, {
            "last_status": INTERRUPTED_JOB_STATUS,
            "last_error": error,
            "last_interrupted_at": _hermes_now().isoformat(),
            "last_interrupted_execution": record.get("id"),
        })
    except Exception as exc:
        logger.debug("Could not persist interrupted status for %s: %s", job_id, exc)


def settle_unstarted_execution(execution_id: str, job_id: str, error: str) -> None:
    """Close the receipt of a run that never started: a ``claimed`` row never resolves. Best-effort
    so a ledger write cannot mask the failure the caller logs or re-raises."""
    try:
        finish_execution(execution_id, success=False, error=error)
    except (sqlite3.Error, OSError) as record_err:
        logger.error("Job '%s': failed to close execution receipt %s (%s): %s",
                     job_id, execution_id, error, record_err)


def recover_interrupted_executions() -> int:
    """Mark abandoned attempts unknown without scheduling retries: rows whose owner is provably
    dead, plus rows whose live owner holds a claim older than the derived stale bound (the
    process is not killed)."""
    now = _hermes_now().isoformat()
    changed = 0
    recovered: list[dict[str, Any]] = []
    # Derived on the first live-owned row only: the bound reads config, and the idle gateway
    # tick must stay config-free (tests/cron/test_idle_tick_config_skip.py).
    stale_after: Optional[float] = None
    stale_after_resolved = False
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at, claimed_at, progress_at
               FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            reason = _OWNER_GONE_REASON
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                # A live owner is normally a legitimately running job. A worker permanently
                # deadlocked (e.g. futex_wait behind a route/proxy flip, #115692) also passes
                # this check, so a claim SILENT for longer than the derived bound is treated
                # as wedged and released — the external-worker wait loop polls this ledger
                # for a terminal status, so the job can fire again. Silence is measured from
                # the owner's last ``progress_at`` stamp (the run monitor refreshes it while
                # the agent is active), so a healthy multi-hour run is never reclaimed while
                # it is still working. The wedged worker PROCESS is NOT terminated here
                # (leaked until host restart); rows owned by this process (process_id ==
                # _PROCESS_ID, in-process runs) are skipped above and remain out of scope.
                if not stale_after_resolved:
                    stale_after = _live_owner_stale_after_seconds()
                    stale_after_resolved = True
                if (
                    stale_after is None
                    or _stale_age_seconds(row["claimed_at"], row["progress_at"]) <= stale_after
                ):
                    continue
                reason = _OWNER_WEDGED_REASON
            handoff_started_at = row["handoff_started_at"]
            if (
                row["handoff_pending"]
                and handoff_started_at is not None
                and time.time() - float(handoff_started_at)
                < HANDOFF_ADOPTION_GRACE_SECONDS
            ):
                continue
            cur = conn.execute(
                """UPDATE executions
                   SET status='unknown', finished_at=?, error=?,
                       handoff_pending=0, handoff_started_at=NULL
                   WHERE id=? AND status=? AND process_id=? AND pid=?
                     AND handoff_pending=?
                     AND handoff_started_at IS ?""",
                (now, reason, row["id"], row["status"], row["process_id"], row["pid"],
                 row["handoff_pending"], row["handoff_started_at"]),
            )
            changed += cur.rowcount
            if cur.rowcount:
                record = _fetch(conn, row["id"])
                if record is not None:
                    recovered.append(record)
        if changed:
            _prune_unlocked(conn)
    for record in recovered:
        _emit_execution_state(record)
        # Only after the ledger transaction closed: these touch two other stores (incidents,
        # jobs.json) and must not hold the ledger lock or abort the sweep.
        _record_owner_loss(record)
    return changed


def terminalize_dead_owner(execution_id: str, *, reason: str) -> bool:
    """Record one attempt as ``unknown`` with a cause this process actually observed.

    ``recover_interrupted_executions`` sweeps every attempt whose owner is provably
    dead, and knows nothing but that absence — so all it can write is
    ``_OWNER_GONE_REASON``, which asserts a scheduler restart. A waiter that held the
    worker's ``Popen`` knows more: the owner was that external worker, and it exited
    with a known status. Without this, a manual run whose worker dies is filed as
    "Scheduler restarted ..." (a restart that never happened) and, because the sweep
    leaves the row terminal, the waiter reports success and never records the run —
    the job's ``fire_claim`` then blocks the next manual fire for the whole lease
    (#128509).

    The attempt stays ``unknown``, not ``failed``: whether side effects ran is still
    unknown. Only the CAUSE becomes truthful. Returns False — leaving the caller to
    fall back to the generic sweep — when the row is absent, already terminal, owned by
    this process, inside the handoff adoption grace, or owned by a live process: a
    worker that is still running must never be terminalized out from under itself.
    """
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        row = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at
               FROM executions WHERE id=?""",
            (execution_id,),
        ).fetchone()
        if row is None or row["status"] not in ("claimed", "running"):
            return False
        if row["process_id"] == _PROCESS_ID:
            return False
        if _owner_is_live(int(row["pid"]), row["process_started_at"]):
            return False
        handoff_started_at = row["handoff_started_at"]
        if (
            row["handoff_pending"]
            and handoff_started_at is not None
            and time.time() - float(handoff_started_at) < HANDOFF_ADOPTION_GRACE_SECONDS
        ):
            return False
        cur = conn.execute(
            """UPDATE executions
               SET status='unknown', finished_at=?, error=?,
                   handoff_pending=0, handoff_started_at=NULL
               WHERE id=? AND status=? AND process_id=? AND pid=?""",
            (now, reason, row["id"], row["status"], row["process_id"], row["pid"]),
        )
        if cur.rowcount != 1:
            return False
        record = _fetch(conn, execution_id)
        _prune_unlocked(conn)
    _emit_execution_state(record)
    return True


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50, before_claimed_at: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: list[str] = []
    params: list[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        # Same (instant, text) key as the ORDER BY, so a page never skips or repeats a row.
        clauses.append("(julianday(claimed_at), claimed_at) < (julianday(?), ?)")
        params.extend([str(before_claimed_at)] * 2)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    # Stamps carry the local offset, which changes at DST and on a timezone change, so text order
    # is not time order. julianday() compares instants (ms); the text breaks same-ms ties.
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_execution(execution_id: str) -> Optional[dict[str, Any]]:
    """Return one exact execution attempt, or ``None`` when it is absent."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?",
            (str(execution_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def latest_execution(job_id: str) -> Optional[dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def live_inflight_execution(job_id: str) -> Optional[dict[str, Any]]:
    """The job's latest attempt while it is still claimed/running under a LIVE owner, else ``None``.

    This is scheduler OWNERSHIP, not recent activity: a run inside a long tool call writes no
    heartbeat yet stays owned, while a run whose process died (watchdog kill, crash) does not.
    Read-only — unlike ``recover_interrupted_executions`` it never rewrites a row.
    """
    record = latest_execution(job_id)
    if not record or record.get("status") not in ("claimed", "running"):
        return None
    if not _owner_is_live(int(record["pid"]), record.get("process_started_at")):
        return None
    return record


def latest_executions(job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load latest execution for many jobs in one query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    # One windowed sort: a per-row correlated ORDER BY julianday() cannot use the index and
    # grows quadratically with history (~90 ms at 1000 rows).
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e WHERE e.id IN (
                  SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                             PARTITION BY job_id
                             ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC
                           ) AS rn
                    FROM executions WHERE job_id IN ({placeholders}))
                  WHERE rn=1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
