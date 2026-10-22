import datetime
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.exc import OperationalError

from src.scheduler import runner as scheduler_module


UTC = datetime.timezone.utc


def test_inventory_monitor_jobs_coalesce_missed_intervals_without_parallel_runs(monkeypatch):
    scheduler = MagicMock()
    monkeypatch.setattr(scheduler_module, "reload_config", lambda: None)
    monkeypatch.setattr(scheduler_module, "get_brand_business_config", lambda _brand: {
        "top_query_inventory_check_interval_minutes": 5,
    })
    monkeypatch.setattr(scheduler_module, "get_brand_business_config", lambda _brand: {
        "top_query_inventory_check_interval_minutes": 5,
    })
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {"atlas": {"enabled": True}})
    monkeypatch.setattr("src.config.is_brand_enabled", lambda _brand: True)

    scheduler_module.sync_inventory_monitor_jobs(scheduler)

    call = next(
        call for call in scheduler.add_job.call_args_list
        if call.kwargs.get("id") == "inventory_monitor_job:atlas"
    )
    assert call.kwargs["coalesce"] is True
    assert call.kwargs["max_instances"] == 1
    assert call.kwargs["misfire_grace_time"] >= 600


def test_remediation_job_serializes_long_runs_and_coalesces_misfires():
    scheduler = MagicMock()
    workflow_factory = MagicMock()

    scheduler_module.sync_query_governance_remediation_job(scheduler, workflow_factory)

    call = next(
        call for call in scheduler.add_job.call_args_list
        if call.kwargs.get("id") == "query_governance_remediation_job"
    )
    assert call.kwargs["coalesce"] is True
    assert call.kwargs["max_instances"] == 1


def test_summary_reconciliation_job_serializes_long_runs_and_coalesces_misfires():
    scheduler = MagicMock()

    scheduler_module.sync_query_governance_summary_reconciliation_job(scheduler)

    call = next(
        call for call in scheduler.add_job.call_args_list
        if call.kwargs.get("id") == scheduler_module.QUERY_GOVERNANCE_SUMMARY_RECONCILIATION_JOB_ID
    )
    assert call.kwargs["minutes"] == 5
    assert call.kwargs["coalesce"] is True
    assert call.kwargs["max_instances"] == 1


def test_image_integrity_job_registers_daily_cron_and_coalesces_misfires():
    scheduler = MagicMock()

    scheduler_module.sync_query_governance_image_integrity_job(scheduler)

    call = next(
        call for call in scheduler.add_job.call_args_list
        if call.kwargs.get("id") == scheduler_module.QUERY_GOVERNANCE_IMAGE_INTEGRITY_JOB_ID
    )
    assert call.args[1] == "cron"
    assert call.kwargs["hour"] == 20
    assert call.kwargs["minute"] == 30
    assert call.kwargs["coalesce"] is True
    assert call.kwargs["max_instances"] == 1
    assert call.kwargs["misfire_grace_time"] == 3600


def test_recovery_job_serializes_long_runs_and_coalesces_misfires():
    scheduler = MagicMock()
    workflow_factory = MagicMock()

    scheduler_module.sync_query_governance_recovery_job(scheduler, workflow_factory)

    call = next(
        call for call in scheduler.add_job.call_args_list
        if call.kwargs.get("id") == scheduler_module.QUERY_GOVERNANCE_RECOVERY_JOB_ID
    )
    assert call.kwargs["minutes"] == 5
    assert call.kwargs["coalesce"] is True
    assert call.kwargs["max_instances"] == 1


def test_scheduler_runtime_logs_duration_and_terminal_status(monkeypatch, caplog):
    db = MagicMock()
    run = SimpleNamespace(id=41)
    monkeypatch.setattr(scheduler_module, "renew_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler_module, "create_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "claim_scheduler_job_run", MagicMock())
    monkeypatch.setattr(scheduler_module, "start_scheduler_job_run", MagicMock())
    monkeypatch.setattr(scheduler_module, "complete_scheduler_job_run", MagicMock())
    runtime = scheduler_module.SchedulerRunExecutor(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        release="a" * 40,
        config_fingerprint="b" * 64,
        lease_seconds=30,
        session_factory=MagicMock(return_value=db),
    )
    scheduled_for = datetime.datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
    runtime.record_scheduled_runs(SimpleNamespace(
        job_id="inventory_monitor_job:atlas",
        scheduled_run_times=[scheduled_for],
    ))

    with caplog.at_level(logging.INFO):
        assert runtime.execute("inventory_monitor_job:atlas", lambda: {"status": "ok"}) == {"status": "ok"}

    assert "scheduler job finished" in caplog.text
    assert "brand=atlas" in caplog.text
    assert "inventory_monitor_job:atlas" in caplog.text
    assert "status=succeeded" in caplog.text
    assert "duration_ms=" in caplog.text


def test_scheduler_runtime_marks_structured_error_result_as_failed(monkeypatch):
    db = MagicMock()
    run = SimpleNamespace(id=41)
    monkeypatch.setattr(scheduler_module, "renew_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler_module, "create_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "claim_scheduler_job_run", MagicMock())
    monkeypatch.setattr(scheduler_module, "start_scheduler_job_run", MagicMock())
    complete = MagicMock()
    monkeypatch.setattr(scheduler_module, "complete_scheduler_job_run", complete)
    runtime = scheduler_module.SchedulerRunExecutor(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        release="a" * 40,
        config_fingerprint="b" * 64,
        lease_seconds=30,
        session_factory=MagicMock(return_value=db),
    )
    runtime.record_scheduled_runs(SimpleNamespace(
        job_id="inventory_monitor_job:atlas",
        scheduled_run_times=[datetime.datetime(2026, 9, 22, 1, 0, tzinfo=UTC)],
    ))

    result = runtime.execute("inventory_monitor_job:atlas", lambda: {
        "status": "error",
        "error_code": "INVENTORY_ACTION_FAILED",
    })

    assert result["status"] == "error"
    assert complete.call_args.kwargs["status"] == "failed"


def test_scheduler_runtime_retries_terminal_write_after_mysql_connection_reset(monkeypatch):
    first_db = MagicMock()
    second_db = MagicMock()
    session_factory = MagicMock(side_effect=[first_db, second_db])
    run = SimpleNamespace(id=41)
    monkeypatch.setattr(scheduler_module, "renew_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler_module, "create_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "claim_scheduler_job_run", MagicMock())
    monkeypatch.setattr(scheduler_module, "start_scheduler_job_run", MagicMock())
    complete = MagicMock(side_effect=[OperationalError("UPDATE", {}, RuntimeError("gone away")), run])
    monkeypatch.setattr(scheduler_module, "complete_scheduler_job_run", complete)

    runtime = scheduler_module.SchedulerRunExecutor(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        release="a" * 40,
        config_fingerprint="b" * 64,
        lease_seconds=30,
        session_factory=session_factory,
    )
    runtime.record_scheduled_runs(SimpleNamespace(
        job_id="inventory_monitor_job:atlas",
        scheduled_run_times=[datetime.datetime(2026, 9, 22, 1, 0, tzinfo=UTC)],
    ))

    assert runtime.execute("inventory_monitor_job:atlas", lambda: "done") == "done"
    assert complete.call_count == 2
    assert complete.call_args_list[0].args[0] is first_db
    assert complete.call_args_list[1].args[0] is second_db
    first_db.close.assert_called_once_with()
    second_db.close.assert_called_once_with()


def test_scheduler_runtime_keeps_business_result_when_terminal_fact_retry_also_fails(monkeypatch):
    first_db = MagicMock()
    second_db = MagicMock()
    run = SimpleNamespace(id=41)
    monkeypatch.setattr(scheduler_module, "renew_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler_module, "create_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "claim_scheduler_job_run", MagicMock())
    monkeypatch.setattr(scheduler_module, "start_scheduler_job_run", MagicMock())
    monkeypatch.setattr(
        scheduler_module,
        "complete_scheduler_job_run",
        MagicMock(side_effect=OperationalError("UPDATE", {}, RuntimeError("gone away"))),
    )
    runtime = scheduler_module.SchedulerRunExecutor(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        release="a" * 40,
        config_fingerprint="b" * 64,
        lease_seconds=30,
        session_factory=MagicMock(side_effect=[first_db, second_db]),
    )
    runtime.record_scheduled_runs(SimpleNamespace(
        job_id="inventory_monitor_job:atlas",
        scheduled_run_times=[datetime.datetime(2026, 9, 22, 1, 0, tzinfo=UTC)],
    ))

    assert runtime.execute("inventory_monitor_job:atlas", lambda: "business-complete") == "business-complete"
