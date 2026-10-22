import datetime
import threading
import time
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main as scheduler_main
from importlib import import_module

scheduler_module = import_module("src.scheduler.runner")
from src.persistence import (
    Base,
    SchedulerJobRun,
    SchedulerLeaderFenceError,
    SchedulerLeaderLeaseUnavailable,
    acquire_scheduler_leader_lease,
)
from src.scheduler.reliability import scheduler_compensation_idempotency_key


UTC = datetime.timezone.utc
RELEASE = "a" * 40
CONFIG_FINGERPRINT = "b" * 64


def _runtime(session_factory):
    return scheduler_module.SchedulerRunExecutor(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        release=RELEASE,
        config_fingerprint=CONFIG_FINGERPRINT,
        lease_seconds=30,
        session_factory=session_factory,
    )


def test_fenced_scheduler_runtime_records_and_completes_a_submitted_slot(monkeypatch):
    db = MagicMock()
    session_factory = MagicMock(return_value=db)
    task = MagicMock(return_value={"status": "ok"})
    run = SimpleNamespace(id=41)
    scheduled_for = datetime.datetime(2026, 8, 7, 0, 30, tzinfo=UTC)
    renew = MagicMock(return_value=SimpleNamespace(fencing_token=7))
    create = MagicMock(return_value=run)
    claim = MagicMock(return_value=run)
    start = MagicMock(return_value=run)
    complete = MagicMock(return_value=run)
    monkeypatch.setattr(scheduler_module, "renew_scheduler_leader_lease", renew, raising=False)
    monkeypatch.setattr(scheduler_module, "create_scheduler_job_run", create, raising=False)
    monkeypatch.setattr(scheduler_module, "claim_scheduler_job_run", claim, raising=False)
    monkeypatch.setattr(scheduler_module, "start_scheduler_job_run", start, raising=False)
    monkeypatch.setattr(scheduler_module, "complete_scheduler_job_run", complete, raising=False)

    runtime = _runtime(session_factory)
    runtime.record_scheduled_runs(
        SimpleNamespace(job_id="query_governance_job", scheduled_run_times=[scheduled_for])
    )

    result = runtime.execute("query_governance_job", task, dry_run=True)

    assert result == {"status": "ok"}
    renew.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        lease_seconds=30,
    )
    create.assert_called_once_with(
        db,
        job_id="query_governance_job",
        scheduled_for=scheduled_for,
        idempotency_key="scheduler-run:v1:query_governance_job:2026-08-07T00:30:00Z",
        release=RELEASE,
        config_fingerprint=CONFIG_FINGERPRINT,
    )
    claim.assert_called_once_with(
        db,
        41,
        claim_owner="scheduler-host:123",
        claim_token=ANY,
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
    )
    start.assert_called_once()
    complete.assert_called_once()
    task.assert_called_once_with(dry_run=True)
    db.close.assert_called_once_with()


def test_fenced_scheduler_runtime_waits_for_submitted_event_after_worker_starts(monkeypatch):
    """APScheduler submits the worker before dispatching EVENT_JOB_SUBMITTED."""
    db = MagicMock()
    run = SimpleNamespace(id=41)
    monkeypatch.setattr(scheduler_module, "renew_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler_module, "create_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "claim_scheduler_job_run", MagicMock())
    monkeypatch.setattr(scheduler_module, "start_scheduler_job_run", MagicMock())
    monkeypatch.setattr(scheduler_module, "complete_scheduler_job_run", MagicMock())
    runtime = _runtime(MagicMock(return_value=db))
    scheduled_for = datetime.datetime(2026, 8, 7, 0, 30, tzinfo=UTC)
    started = threading.Event()
    result = {}

    def task():
        started.set()
        return {"status": "ok"}

    worker = threading.Thread(
        target=lambda: result.setdefault(
            "value", runtime.execute("inventory_monitor_job:atlas", task)
        )
    )
    worker.start()
    # The scheduler's worker can begin before its dispatcher publishes the event.
    time.sleep(0.01)
    assert not started.is_set()
    runtime.record_scheduled_runs(
        SimpleNamespace(job_id="inventory_monitor_job:atlas", scheduled_run_times=[scheduled_for])
    )
    worker.join(1)

    assert not worker.is_alive()
    assert result["value"] == {"status": "ok"}
    assert started.is_set()


def test_fenced_scheduler_runtime_does_not_execute_when_renew_rejects_stale_fence(monkeypatch):
    db = MagicMock()
    session_factory = MagicMock(return_value=db)
    task = MagicMock()
    monkeypatch.setattr(
        scheduler_module,
        "renew_scheduler_leader_lease",
        MagicMock(side_effect=SchedulerLeaderFenceError("stale")),
        raising=False,
    )
    create = MagicMock()
    monkeypatch.setattr(scheduler_module, "create_scheduler_job_run", create, raising=False)
    runtime = _runtime(session_factory)
    runtime.record_scheduled_runs(
        SimpleNamespace(
            job_id="service_health_check_job",
            scheduled_run_times=[datetime.datetime(2026, 8, 7, 1, 0, tzinfo=UTC)],
        )
    )

    result = runtime.execute("service_health_check_job", task)

    assert result is None
    create.assert_not_called()
    task.assert_not_called()
    db.close.assert_called_once_with()


def test_fenced_scheduler_runtime_rejects_new_slot_after_termination_drain_starts():
    tracker = scheduler_module.ActiveRunTracker()
    tracker.stop_accepting()
    task = MagicMock()
    runtime = scheduler_module.SchedulerRunExecutor(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        release=RELEASE,
        config_fingerprint=CONFIG_FINGERPRINT,
        lease_seconds=30,
        active_run_tracker=tracker,
    )
    runtime.record_scheduled_runs(
        SimpleNamespace(
            job_id="service_health_check_job",
            scheduled_run_times=[datetime.datetime(2026, 8, 7, 1, 0, tzinfo=UTC)],
        )
    )

    assert runtime.execute("service_health_check_job", task) is None
    task.assert_not_called()


def test_fenced_scheduler_runtime_counts_active_work_until_callback_finishes(monkeypatch):
    db = MagicMock()
    session_factory = MagicMock(return_value=db)
    tracker = scheduler_module.ActiveRunTracker()
    started = threading.Event()
    release_callback = threading.Event()
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
        release=RELEASE,
        config_fingerprint=CONFIG_FINGERPRINT,
        lease_seconds=30,
        session_factory=session_factory,
        active_run_tracker=tracker,
    )
    runtime.record_scheduled_runs(
        SimpleNamespace(
            job_id="service_health_check_job",
            scheduled_run_times=[datetime.datetime(2026, 8, 7, 1, 0, tzinfo=UTC)],
        )
    )

    def task():
        started.set()
        assert release_callback.wait(1)
        return "done"

    worker = threading.Thread(target=lambda: runtime.execute("service_health_check_job", task))
    worker.start()
    assert started.wait(1)
    tracker.stop_accepting()
    assert tracker.wait_for_drain(0.001) is False
    release_callback.set()
    worker.join(1)
    assert not worker.is_alive()
    assert tracker.wait_for_drain(0.1) is True


def test_fenced_scheduler_runtime_persists_submitted_slot_with_live_leader_fence():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        lease = acquire_scheduler_leader_lease(
            session,
            leader_name="scheduler-main",
            owner_id="scheduler-host:123",
            lease_seconds=30,
        )

    runtime = scheduler_module.SchedulerRunExecutor(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=lease.fencing_token,
        release=RELEASE,
        config_fingerprint=CONFIG_FINGERPRINT,
        lease_seconds=30,
        session_factory=Session,
    )
    runtime.record_scheduled_runs(
        SimpleNamespace(
            job_id="query_governance_job",
            scheduled_run_times=[datetime.datetime(2026, 8, 7, 0, 30, tzinfo=UTC)],
        )
    )

    assert runtime.execute("query_governance_job", lambda: "done") == "done"

    with Session() as session:
        run = session.query(SchedulerJobRun).one()
        assert run.idempotency_key == "scheduler-run:v1:query_governance_job:2026-08-07T00:30:00Z"
        assert run.status == "succeeded"
        assert run.claim_owner == "scheduler-host:123"
        assert run.started_at is not None
        assert run.terminal_at is not None
    engine.dispose()


def test_compensation_execution_claims_a_persisted_allowlisted_run_before_callback():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    scheduled_for = datetime.datetime(2026, 8, 6, 0, 30, tzinfo=UTC)
    with Session() as session:
        lease = acquire_scheduler_leader_lease(
            session,
            leader_name="scheduler-main",
            owner_id="scheduler-host:123",
            lease_seconds=30,
        )
        run = SchedulerJobRun(
            job_id="query_governance_job",
            scheduled_for=scheduled_for.replace(tzinfo=None),
            idempotency_key=scheduler_compensation_idempotency_key(
                "query_governance_job", scheduled_for
            ),
            status="planned",
            attempt_no=0,
            release=RELEASE,
            config_fingerprint=CONFIG_FINGERPRINT,
        )
        session.add(run)
        session.commit()

    callback = MagicMock(return_value="compensated")
    runtime = scheduler_module.SchedulerRunExecutor(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=lease.fencing_token,
        release=RELEASE,
        config_fingerprint=CONFIG_FINGERPRINT,
        lease_seconds=30,
        compensation_handlers={"query_governance_job": callback},
        session_factory=Session,
    )

    assert runtime.execute_compensation(run.id, "query_governance_job") == "compensated"
    assert runtime.execute_compensation(run.id, "query_governance_job") is None
    callback.assert_called_once_with()
    with Session() as session:
        persisted = session.get(SchedulerJobRun, run.id)
        assert persisted.status == "succeeded"
        assert persisted.attempt_no == 1
        assert persisted.claim_owner == "scheduler-host:123"
        assert persisted.started_at is not None
        assert persisted.terminal_at is not None
    engine.dispose()


@pytest.mark.parametrize(
    ("run_release", "run_config_fingerprint"),
    [("stale-release", CONFIG_FINGERPRINT), (RELEASE, "c" * 64)],
)
def test_compensation_execution_rejects_stale_release_or_config_before_claim(
    monkeypatch, run_release, run_config_fingerprint
):
    scheduled_for = datetime.datetime(2026, 8, 6, 0, 30)
    run = SimpleNamespace(
        id=41,
        job_id="query_governance_job",
        scheduled_for=scheduled_for,
        idempotency_key=scheduler_compensation_idempotency_key(
            "query_governance_job", scheduled_for.replace(tzinfo=UTC)
        ),
        status="planned",
        attempt_no=0,
        release=run_release,
        config_fingerprint=run_config_fingerprint,
    )
    db = MagicMock()
    db.get.return_value = run
    callback = MagicMock()
    renew = MagicMock()
    monkeypatch.setattr(scheduler_module, "renew_scheduler_leader_lease", renew, raising=False)
    runtime = _runtime(MagicMock(return_value=db))
    runtime._compensation_handlers["query_governance_job"] = callback

    assert runtime.execute_compensation(41, "query_governance_job") is None
    callback.assert_not_called()
    renew.assert_not_called()
    db.close.assert_called_once_with()


def test_compensation_execution_rejects_unallowlisted_job_without_claiming_or_callback():
    db = MagicMock()
    callback = MagicMock()
    runtime = _runtime(MagicMock(return_value=db))
    runtime._compensation_handlers["query_governance_job"] = callback

    assert runtime.execute_compensation(41, "unknown_job") is None
    callback.assert_not_called()
    db.get.assert_not_called()
    db.close.assert_not_called()


def test_compensation_execution_rejects_missing_release_identity_before_renew(monkeypatch):
    scheduled_for = datetime.datetime(2026, 8, 6, 0, 30)
    run = SimpleNamespace(
        id=41,
        job_id="query_governance_job",
        status="planned",
        attempt_no=0,
        scheduled_for=scheduled_for,
        idempotency_key=scheduler_compensation_idempotency_key(
            "query_governance_job", scheduled_for.replace(tzinfo=UTC)
        ),
    )
    db = MagicMock()
    db.get.return_value = run
    callback = MagicMock()
    renew = MagicMock()
    monkeypatch.setattr(scheduler_module, "renew_scheduler_leader_lease", renew, raising=False)
    runtime = _runtime(MagicMock(return_value=db))
    runtime._compensation_handlers["query_governance_job"] = callback

    assert runtime.execute_compensation(41, "query_governance_job") is None
    callback.assert_not_called()
    renew.assert_not_called()
    db.close.assert_called_once_with()


def test_compensation_execution_rejects_stale_fence_without_callback(monkeypatch):
    db = MagicMock()
    callback = MagicMock()
    run = SimpleNamespace(
        id=41,
        job_id="query_governance_job",
        status="planned",
        attempt_no=0,
        scheduled_for=datetime.datetime(2026, 8, 6, 0, 30),
        idempotency_key=scheduler_compensation_idempotency_key(
            "query_governance_job", datetime.datetime(2026, 8, 6, 0, 30, tzinfo=UTC)
        ),
        release=RELEASE,
        config_fingerprint=CONFIG_FINGERPRINT,
    )
    db.get.return_value = run
    monkeypatch.setattr(
        scheduler_module,
        "renew_scheduler_leader_lease",
        MagicMock(side_effect=SchedulerLeaderFenceError("stale")),
        raising=False,
    )
    runtime = _runtime(MagicMock(return_value=db))
    runtime._compensation_handlers["query_governance_job"] = callback

    assert runtime.execute_compensation(41, "query_governance_job") is None
    callback.assert_not_called()
    db.close.assert_called_once_with()


def test_start_scheduler_requires_a_durable_global_leader_lease(monkeypatch):
    scheduler = MagicMock()
    db = MagicMock()
    acquire = MagicMock(return_value=SimpleNamespace(fencing_token=9))
    release = MagicMock()
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", MagicMock(return_value=scheduler))
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease", acquire, raising=False)
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", release, raising=False)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_LEASE_SECONDS", 30, raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)
    monkeypatch.setattr(scheduler_module, "_scheduler_config_fingerprint", lambda: CONFIG_FINGERPRINT, raising=False)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    assert scheduler_module.start_scheduler(release=RELEASE) is True

    acquire.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        lease_seconds=30,
    )
    release.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=9,
    )


def test_start_scheduler_persists_missed_run_plans_before_start_without_executing_jobs(monkeypatch):
    scheduler = MagicMock()
    scheduler.get_jobs.return_value = [
        SimpleNamespace(
            id="query_governance_job",
            trigger=SimpleNamespace(get_next_fire_time=MagicMock()),
        )
    ]
    leader_db = MagicMock()
    planning_db = MagicMock()
    release_db = MagicMock()
    acquire = MagicMock(return_value=SimpleNamespace(fencing_token=9))
    persist_plans = MagicMock(return_value={"jobCount": 1, "plannedCount": 0})
    query_governance_callback = MagicMock()
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", MagicMock(return_value=scheduler))
    monkeypatch.setattr(
        scheduler_module,
        "SessionLocal",
        MagicMock(side_effect=[leader_db, planning_db, release_db]),
    )
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease", acquire, raising=False)
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", MagicMock(), raising=False)
    monkeypatch.setattr(
        scheduler_module,
        "plan_and_persist_missed_scheduler_runs",
        persist_plans,
        raising=False,
    )
    monkeypatch.setattr(
        scheduler_module,
        "query_governance_job",
        query_governance_callback,
        raising=False,
    )
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_LEASE_SECONDS", 30, raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    assert scheduler_module.start_scheduler(
        release=RELEASE,
        config_fingerprint=CONFIG_FINGERPRINT,
    ) is True

    persist_plans.assert_called_once()
    call = persist_plans.call_args
    assert call.args == (planning_db,)
    assert call.kwargs["jobs"] == scheduler.get_jobs.return_value
    assert call.kwargs["release"] == RELEASE
    assert call.kwargs["config_fingerprint"] == CONFIG_FINGERPRINT
    assert call.kwargs["observed_at"].tzinfo is not None
    planning_db.close.assert_called_once_with()
    query_governance_callback.assert_not_called()
    scheduler.start.assert_called_once_with()


def test_scheduler_shutdown_stops_accepting_runs_before_waiting_and_releasing_leader(monkeypatch):
    scheduler = MagicMock()
    db = MagicMock()
    lease = SimpleNamespace(fencing_token=9)
    release = MagicMock()
    tracker = MagicMock()
    tracker.wait_for_drain.return_value = True
    controller = MagicMock()
    controller.shutdown_requested = False
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", MagicMock(return_value=scheduler))
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease", MagicMock(return_value=lease))
    monkeypatch.setattr(scheduler_module, "recover_stale_scheduler_job_runs", MagicMock(return_value=0))
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", release)
    monkeypatch.setattr(scheduler_module, "ActiveRunTracker", MagicMock(return_value=tracker))
    monkeypatch.setattr(scheduler_module, "SchedulerTerminationController", MagicMock(return_value=controller))
    monkeypatch.setattr(scheduler_module, "install_termination_signal_handlers", lambda **kwargs: lambda: None)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    assert scheduler_module.start_scheduler(release=RELEASE) is True

    controller.request_shutdown.assert_called_once_with("scheduler_stopped")
    controller.drain_and_release.assert_called_once_with()


def test_start_scheduler_stops_leader_heartbeat_when_termination_controller_does_not_release(
    monkeypatch,
):
    scheduler = MagicMock()
    db = MagicMock()
    lease = SimpleNamespace(fencing_token=9)
    heartbeat = MagicMock()
    controller = MagicMock()
    heartbeat.assert_owned = MagicMock()
    controller.shutdown_requested = False

    monkeypatch.setattr(scheduler_module, "BlockingScheduler", MagicMock(return_value=scheduler))
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease", MagicMock(return_value=lease))
    monkeypatch.setattr(scheduler_module, "recover_stale_scheduler_job_runs", MagicMock(return_value=0))
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler_module, "SchedulerLeaseHeartbeat", MagicMock(return_value=heartbeat))
    monkeypatch.setattr(scheduler_module, "SchedulerTerminationController", MagicMock(return_value=controller))
    monkeypatch.setattr(scheduler_module, "install_termination_signal_handlers", lambda **kwargs: lambda: None)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    assert scheduler_module.start_scheduler(release=RELEASE) is True

    heartbeat.start.assert_called_once_with()
    heartbeat.stop.assert_called_once_with()


def test_scheduler_sigterm_uses_termination_controller_drain_before_process_returns(monkeypatch):
    scheduler = MagicMock()
    db = MagicMock()
    lease = SimpleNamespace(fencing_token=9)
    controller = MagicMock()
    controller.shutdown_requested = True
    registered = {}
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", MagicMock(return_value=scheduler))
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease", MagicMock(return_value=lease))
    monkeypatch.setattr(scheduler_module, "recover_stale_scheduler_job_runs", MagicMock(return_value=0))
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler_module, "SchedulerTerminationController", MagicMock(return_value=controller))
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    def install(**kwargs):
        handler = kwargs["controller"]
        handler.request_shutdown("SIGTERM")
        handler.drain_and_release()
        return lambda: registered.setdefault("restored", True)

    monkeypatch.setattr(scheduler_module, "install_termination_signal_handlers", install)

    assert scheduler_module.start_scheduler(release=RELEASE) is True
    controller.request_shutdown.assert_called_once_with("SIGTERM")
    assert controller.drain_and_release.call_count == 2
    assert registered["restored"] is True


def test_start_scheduler_refuses_to_register_jobs_when_durable_leader_is_held(monkeypatch):
    scheduler_factory = MagicMock()
    db = MagicMock()
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", scheduler_factory)
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(
        scheduler_module,
        "acquire_scheduler_leader_lease",
        MagicMock(side_effect=SchedulerLeaderLeaseUnavailable("held")),
        raising=False,
    )
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_LEASE_SECONDS", 30, raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    assert scheduler_module.start_scheduler(release=RELEASE) is False

    scheduler_factory.assert_not_called()


def test_scheduler_waits_for_requested_background_shutdown_before_return(monkeypatch):
    scheduler = MagicMock()
    db = MagicMock()
    controller = MagicMock()
    controller.shutdown_requested = True
    controller.drain_and_release.return_value = True
    heartbeat = MagicMock()
    heartbeat.assert_owned = MagicMock()
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", lambda: scheduler)
    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease",
                        lambda *_args, **_kwargs: SimpleNamespace(fencing_token=9))
    monkeypatch.setattr(scheduler_module, "recover_stale_scheduler_job_runs", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler_module, "SchedulerLeaseHeartbeat", lambda **_kwargs: heartbeat)
    monkeypatch.setattr(scheduler_module, "SchedulerTerminationController", lambda **_kwargs: controller)
    monkeypatch.setattr(scheduler_module, "install_termination_signal_handlers", lambda **_kwargs: lambda: None)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    assert scheduler_module.start_scheduler(release=RELEASE) is True
    controller.request_shutdown.assert_not_called()
    controller.drain_and_release.assert_called_once_with()


def test_start_scheduler_releases_durable_leader_when_job_registration_fails(monkeypatch):
    scheduler = MagicMock()
    scheduler.add_job.side_effect = RuntimeError("registration failed")
    db = MagicMock()
    acquire = MagicMock(return_value=SimpleNamespace(fencing_token=9))
    release = MagicMock()
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", MagicMock(return_value=scheduler))
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease", acquire, raising=False)
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", release, raising=False)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    with pytest.raises(RuntimeError, match="registration failed"):
        scheduler_module.start_scheduler(release=RELEASE)

    release.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=9,
    )


def test_start_scheduler_releases_durable_leader_when_start_listener_registration_fails(monkeypatch):
    scheduler = MagicMock()
    scheduler.add_listener.side_effect = (None, RuntimeError("listener registration failed"))
    db = MagicMock()
    acquire = MagicMock(return_value=SimpleNamespace(fencing_token=9))
    release = MagicMock()
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", MagicMock(return_value=scheduler))
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease", acquire, raising=False)
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", release, raising=False)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    with pytest.raises(RuntimeError, match="listener registration failed"):
        scheduler_module.start_scheduler(release=RELEASE, on_started=lambda: None)

    release.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=9,
    )


def test_start_scheduler_releases_durable_leader_when_scheduler_construction_fails(monkeypatch):
    db = MagicMock()
    acquire = MagicMock(return_value=SimpleNamespace(fencing_token=9))
    release = MagicMock()
    monkeypatch.setattr(
        scheduler_module,
        "BlockingScheduler",
        MagicMock(side_effect=RuntimeError("scheduler construction failed")),
    )
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease", acquire, raising=False)
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", release, raising=False)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)

    with pytest.raises(RuntimeError, match="scheduler construction failed"):
        scheduler_module.start_scheduler(release=RELEASE)

    release.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=9,
    )


def test_start_scheduler_releases_durable_leader_when_start_fails(monkeypatch):
    scheduler = MagicMock()
    scheduler.start.side_effect = RuntimeError("scheduler start failed")
    db = MagicMock()
    acquire = MagicMock(return_value=SimpleNamespace(fencing_token=9))
    release = MagicMock()
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", MagicMock(return_value=scheduler))
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease", acquire, raising=False)
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", release, raising=False)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    with pytest.raises(RuntimeError, match="scheduler start failed"):
        scheduler_module.start_scheduler(release=RELEASE)

    scheduler.start.assert_called_once_with()
    release.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=9,
    )


def test_start_scheduler_preserves_start_failure_when_lease_release_also_fails(
    monkeypatch,
    caplog,
):
    scheduler = MagicMock()
    scheduler.start.side_effect = ValueError("original scheduler failure")
    db = MagicMock()
    acquire = MagicMock(return_value=SimpleNamespace(fencing_token=9))
    release = MagicMock(side_effect=RuntimeError("lease release failed"))
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", MagicMock(return_value=scheduler))
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(scheduler_module, "acquire_scheduler_leader_lease", acquire, raising=False)
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", release, raising=False)
    monkeypatch.setattr(scheduler_module, "SCHEDULER_GLOBAL_LEADER_NAME", "scheduler-main", raising=False)
    monkeypatch.setattr(scheduler_module, "_new_scheduler_owner_id", lambda: "scheduler-host:123", raising=False)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    with pytest.raises(RuntimeError, match="scheduler start failed"):
        scheduler_module.start_scheduler(release=RELEASE)

    release.assert_called_once()
    assert "scheduler leader lease release failed during scheduler failure" in caplog.text


def test_main_passes_release_identity_to_scheduler(monkeypatch):
    received = {}

    monkeypatch.setattr(scheduler_main, "get_env", lambda: "test")
    monkeypatch.setattr(
        scheduler_main,
        "get_release_info",
        lambda: {
            "releaseSha": RELEASE,
            "schemaRelease": "2026-08-07",
            "artifactManifestSha256": CONFIG_FINGERPRINT,
        },
    )

    def start_scheduler(**kwargs):
        received.update(kwargs)
        kwargs["on_started"]()
        return True

    monkeypatch.setattr(scheduler_main, "start_scheduler", start_scheduler)

    scheduler_main.main()

    assert received["release"] == RELEASE
    assert received["config_fingerprint"] == CONFIG_FINGERPRINT
