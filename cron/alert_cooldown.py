"""Business-alert cooldown — repeat suppression for ``EXIT_ALERT`` (rc=2) runs.

Contract SSOT: ``~/.hermes/scripts/cron_exit_codes.py``. A run that ends with rc=2 *completed*:
the script already reported what it found, so it is not a failure (discipline: "unknown → treat as
failure", but rc=2 is known and named). Discipline 2 says the alert must still reach the user — so
this module never hides a *first* alert; it only stops the *same* alert from being re-delivered on
every run of the same job.

Key = ``job_id + sha1(normalized alert text)``. The window is passed in by the caller
(``cron.alert_repeat_suppress_hours``, default 6h; ``0`` disables suppression entirely).

Two properties keep the store from becoming a liability of its own:

* **Every entry ages out** — entries whose ``last_at`` is older than :data:`RETENTION_DAYS` are
  dropped on write, and the store is capped at :data:`MAX_KEYS` (oldest evicted first).
* **Suppressions are logged** — each suppressed repeat appends one line to
  ``alert_cooldown_suppressed.jsonl`` so the weekly governance report can show "N suppressed this
  week". Nothing is silently swallowed.

Writes are atomic (temp file in the same directory + ``os.replace``) and serialized with an
``flock`` on a sidecar lock file: two rc=2 jobs can finish at the same moment.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

#: Test override (mirrors ``cron.incidents.EXECUTIONS_FILE``): when set, the store lives here.
ALERT_COOLDOWN_FILE: Optional[Path] = None

#: Suppressed repeats older than this are pruned on every write.
RETENTION_DAYS = 30
#: Hard cap on tracked (job, alert) keys; the oldest ``last_at`` goes first.
MAX_KEYS = 500
#: Alert text is truncated to this many characters before hashing (a long stack of digits must not
#: mint a new key on every run) — deliberately NOT stripping digits/timestamps: over-suppressing a
#: changing alert is worse than an extra ping.
TEXT_LIMIT = 200


def cooldown_path() -> Path:
    """Path of the cooldown store (override first, then the profile home)."""
    if ALERT_COOLDOWN_FILE is not None:
        return Path(ALERT_COOLDOWN_FILE)
    return get_hermes_home().resolve() / "data" / "alert_cooldown.json"


def suppressed_log_path() -> Path:
    """Append-only log of suppressed repeats (feeds the weekly "N suppressed" line)."""
    return cooldown_path().with_name("alert_cooldown_suppressed.jsonl")


@contextmanager
def _locked() -> Iterator[None]:
    """Serialize read-modify-write across processes (best effort: an unlocked FS still works)."""
    lock_path = cooldown_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:                                  # pragma: no cover - non-POSIX / odd FS
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:                                  # pragma: no cover
            pass
        handle.close()


def normalize(text: Any) -> str:
    """Whitespace-collapsed, length-capped alert text (the identity of "the same alert")."""
    return " ".join(str(text or "").split())[:TEXT_LIMIT]


def fingerprint(job_id: str, text: Any) -> str:
    """``job_id`` + hash of the normalized alert text."""
    digest = hashlib.sha1(normalize(text).encode("utf-8", "replace")).hexdigest()[:12]
    return f"{job_id}:{digest}"


def load_store() -> Dict[str, dict]:
    """Parse the store; a missing/corrupt file reads as empty (never breaks delivery)."""
    path = cooldown_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_store(store: Dict[str, dict]) -> None:
    """Atomic replace inside the store's own directory."""
    path = cooldown_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".alert_cooldown.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def prune(store: Dict[str, dict], *, now: datetime) -> Dict[str, dict]:
    """Drop entries past :data:`RETENTION_DAYS`, then cap at :data:`MAX_KEYS` (oldest first)."""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    fresh = {
        key: entry for key, entry in store.items()
        if (ts := _parse_ts(entry.get("last_at"))) is not None and ts >= cutoff
    }
    if len(fresh) > MAX_KEYS:
        ordered = sorted(fresh.items(), key=lambda kv: str(kv[1].get("last_at") or ""), reverse=True)
        fresh = dict(ordered[:MAX_KEYS])
    return fresh


def note(
    job_id: str, text: Any, *, hours: float, now: Optional[datetime] = None,
) -> Tuple[bool, int]:
    """Record one business alert. Returns ``(suppressed, count)``.

    ``suppressed`` is True when the *same* alert already went out inside the window, i.e. this run
    must not re-deliver it; ``count`` is how many times the alert has been seen inside the window
    (including this one). ``hours <= 0`` disables suppression (always returns ``(False, 1)``) and
    still refreshes the entry so the window starts from the latest occurrence.
    """
    if not job_id:
        return False, 1
    now = now or _hermes_now()
    key = fingerprint(job_id, text)
    with _locked():
        store = prune(load_store(), now=now)
        entry = store.get(key) or {}
        count = int(entry.get("count") or 0) + 1
        first_at = entry.get("first_at") or now.isoformat()
        last = _parse_ts(entry.get("last_at"))
        suppressed = (
            hours > 0 and last is not None
            and (now - last) < timedelta(hours=float(hours))
        )
        store[key] = {"job_id": job_id, "first_at": first_at, "last_at": now.isoformat(),
                      "count": count}
        store = prune(store, now=now)
        try:
            _save_store(store)
        except Exception:                                  # pragma: no cover - disk trouble
            return False, count                            # fail-open: an alert is never dropped
    if suppressed:
        _log_suppressed(job_id, text, count, now)
    return suppressed, count


def _log_suppressed(job_id: str, text: Any, count: int, now: datetime) -> None:
    """Append one line per suppressed repeat (best effort — never breaks delivery)."""
    path = suppressed_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"at": now.isoformat(), "job_id": job_id, "count": count,
                 "text": normalize(text)[:120]}, ensure_ascii=False) + "\n")
    except Exception:                                      # pragma: no cover
        pass


def suppressed_since(since: datetime) -> int:
    """How many repeats were suppressed at/after ``since`` (weekly report line)."""
    path = suppressed_log_path()
    total = 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ts = _parse_ts(row.get("at"))
                if ts is not None and ts >= since:
                    total += 1
    except FileNotFoundError:
        return 0
    except Exception:                                      # pragma: no cover
        return total
    return total
