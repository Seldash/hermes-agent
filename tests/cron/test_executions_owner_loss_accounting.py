"""Owner-loss accounting for the cron execution ledger.

An attempt whose owner died (gateway restart, kill -9) is marked ``unknown`` in the ledger — but
the ledger alone is not an operator surface: ``hermes cron incidents`` stayed empty and the job kept
the PREVIOUS run's green ``last_status``, so a killed run read as "nothing happened" until the 24h
detector window closed. These tests pin the two durable writes that close that gap, plus the two
ways they must stay silent: a live owner, and a loss superseded by a newer completed attempt.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def _make_job(name: str = "owner-loss-probe") -> dict:
    from cron import jobs

    return jobs.create_job(
        prompt=None,
        schedule="every 24h",
        name=name,
        no_agent=True,
        script="echo probe",
        deliver="local",
    )


def _dead_owner_attempt(executions, job_id: str):
    """One claimed attempt owned by a process that is gone by the time recovery runs."""
    record = executions.create_execution(job_id, source="builtin")
    assert record["status"] == "claimed"
    return record


def _recover_as_new_process(monkeypatch, executions) -> int:
    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: False)
    return executions.recover_interrupted_executions()


def test_owner_loss_records_incident_and_non_ok_status(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    job = _make_job()
    attempt = _dead_owner_attempt(executions, job["id"])

    assert _recover_as_new_process(monkeypatch, executions) == 1

    from cron import jobs
    from cron.incidents import list_incidents

    recovered = executions.get_execution(attempt["id"])
    assert recovered["status"] == "unknown"
    assert executions._OWNER_GONE_REASON in recovered["error"]

    incidents = [i for i in list_incidents() if i["job_id"] == job["id"]]
    assert len(incidents) == 1, "the loss must be acknowledgeable as an incident"
    assert executions._OWNER_GONE_REASON in incidents[0]["error"]
    assert incidents[0]["state"] in ("detected", "alerted")

    after = jobs.get_job(job["id"])
    assert after["last_status"] == executions.INTERRUPTED_JOB_STATUS
    assert after["last_status"] != "ok", "a killed run must not keep the previous run's green"
    assert executions._OWNER_GONE_REASON in after["last_error"]
    assert after["last_interrupted_execution"] == attempt["id"]
    assert after["last_interrupted_at"]
    # The attempt never completed, so the schedule bookkeeping must be untouched.
    assert after["next_run_at"] == job["next_run_at"]


def test_owner_loss_ack_suppresses_repeat_alert_for_same_signature(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    job = _make_job("owner-loss-ack")
    _dead_owner_attempt(executions, job["id"])
    assert _recover_as_new_process(monkeypatch, executions) == 1

    from cron import jobs
    from cron.incidents import list_incidents, set_incident_state
    from cron.scheduler import _upsert_incident_for_failure

    incident = next(i for i in list_incidents() if i["job_id"] == job["id"])
    assert set_incident_state(incident["id"], "closed") is True

    withheld, incident_id = _upsert_incident_for_failure(
        jobs.get_job(job["id"]), executions._OWNER_GONE_REASON
    )
    assert withheld is True, "an acked owner-loss must not re-alert on the next occurrence"
    assert incident_id == incident["id"]


def test_owner_loss_is_not_resurrected_over_a_newer_completed_run(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    job = _make_job("owner-loss-superseded")
    _dead_owner_attempt(executions, job["id"])

    from cron import jobs

    newer = (datetime.now().astimezone() + timedelta(hours=1)).isoformat()
    jobs.update_job(job["id"], {"last_status": "ok", "last_run_at": newer})

    assert _recover_as_new_process(monkeypatch, executions) == 1

    after = jobs.get_job(job["id"])
    assert after["last_status"] == "ok", "a newer completed attempt owns the job's current status"
    assert after.get("last_interrupted_execution") is None

    from cron.incidents import list_incidents

    assert [i for i in list_incidents() if i["job_id"] == job["id"]] == []


def test_live_owner_attempt_is_not_accounted(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    job = _make_job("owner-loss-live")
    _dead_owner_attempt(executions, job["id"])

    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: True)

    assert executions.recover_interrupted_executions() == 0

    from cron import jobs
    from cron.incidents import list_incidents

    assert jobs.get_job(job["id"]).get("last_status") != executions.INTERRUPTED_JOB_STATUS
    assert [i for i in list_incidents() if i["job_id"] == job["id"]] == []


def test_doctor_reports_interrupted_run_as_neither_green_nor_agent_failure(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    job = _make_job("owner-loss-doctor")
    _dead_owner_attempt(executions, job["id"])
    assert _recover_as_new_process(monkeypatch, executions) == 1

    from cron import jobs
    from hermes_cli.cron import _cron_doctor_issues_for_job

    issues = _cron_doctor_issues_for_job(jobs.get_job(job["id"]))
    assert any("interrupted" in issue for issue in issues), issues
    assert not any(issue.startswith("last run failed") for issue in issues), issues
    assert not any("alerted (rc 2" in issue for issue in issues), issues
