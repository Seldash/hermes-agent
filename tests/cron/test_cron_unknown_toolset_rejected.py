"""An unknown `enabled_toolsets` name must fail the create/update, not silently resolve to nothing.

``resolve_toolset('files')`` returns ``[]`` (the real name is ``file``), so a near-miss name
used to strip a job's whole tool face with no error and no log line — jobs kept "succeeding"
while running with zero file tools. The validator fails closed and names the likely intent;
an explicit ``[]`` (zero-tool allowlist by design) must keep working.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs don't leak between tests."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib

    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return home


def test_validator_accepts_known_names_and_empty():
    from tools.cronjob_job_args import _validate_toolsets

    assert _validate_toolsets(None) is None
    assert _validate_toolsets([]) is None          # explicit zero-tool allowlist, not a typo
    assert _validate_toolsets(["terminal", "file"]) is None
    assert _validate_toolsets(["context_engine"]) is None   # valid even though it resolves empty


def test_validator_rejects_unknown_with_near_miss_hint():
    from tools.cronjob_job_args import _validate_toolsets

    error = _validate_toolsets(["files"])
    assert error and "'files'" in error and "'file'" in error
    # A name with no plausible neighbour still fails, just without a suggestion.
    assert _validate_toolsets(["zzz_not_a_toolset"])


def test_create_rejects_unknown_toolset_without_creating_a_job(hermes_env):
    from cron.jobs import list_jobs
    from tools.cronjob_tools import cronjob

    before = len(list_jobs())
    result = json.loads(cronjob(
        action="create", prompt="weekly digest", schedule="every 1h",
        enabled_toolsets=["terminal", "files"]))
    assert result["success"] is False, result
    assert "files" in result["error"]
    assert len(list_jobs()) == before, "a rejected create must not persist a job"


def test_update_rejects_unknown_toolset_and_leaves_the_job_unchanged(hermes_env):
    from cron.jobs import create_job, get_job
    from tools.cronjob_tools import cronjob

    job = create_job(prompt="audit sweep", schedule="every 1h", enabled_toolsets=["terminal"])
    result = json.loads(cronjob(
        action="update", job_id=job["id"], enabled_toolsets=["terminal", "files"]))
    assert result["success"] is False, result
    assert get_job(job["id"])["enabled_toolsets"] == ["terminal"]


def test_valid_toolsets_still_round_trip(hermes_env):
    from cron.jobs import get_job
    from tools.cronjob_tools import cronjob

    result = json.loads(cronjob(
        action="create", prompt="audit sweep", schedule="every 1h",
        enabled_toolsets=["terminal", "file"]))
    assert result["success"] is True, result
    assert get_job(result["job_id"])["enabled_toolsets"] == ["terminal", "file"]
