import datetime
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from importlib import import_module

scheduler_module = import_module("src.scheduler.runner")
import src.scheduler.recovery as recovery_module
from src.persistence import (
    Base,
    SchedulerJobRun,
    SchedulerLeaderFenceError,
    acquire_scheduler_leader_lease,
    claim_scheduler_job_run,
    create_scheduler_job_run,
    recover_stale_scheduler_job_runs,
    start_scheduler_job_run,
)


UTC = datetime.timezone.utc
RELEASE = "a" * 40
CONFIG_FINGERPRINT = "b" * 64


def _runtime(session_factory, heartbeat_factory):
    return scheduler_module.SchedulerRunExecutor(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        release=RELEASE,
        config_fingerprint=CONFIG_FINGERPRINT,
        lease_seconds=30,
        session_factory=session_factory,
        heartbeat_factory=heartbeat_factory,
    )


def test_run_heartbeat_renews_leader_and_running_fact(monkeypatch):
    db = MagicMock()
    renew = MagicMock()
    heartbeat_run = MagicMock()
    monkeypatch.setattr(recovery_module, "renew_scheduler_leader_lease", renew)
    monkeypatch.setattr(recovery_module, "heartbeat_scheduler_job_run", heartbeat_run)
    heartbeat = recovery_module.SchedulerLeaseHeartbeat(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        lease_seconds=30,
        session_factory=MagicMock(return_value=db),
        run_id=41,
        claim_owner="scheduler-host:123",
        claim_token="claim-token",
    )

    heartbeat.beat_once()

    renew.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        lease_seconds=30,
    )
    heartbeat_run.assert_called_once_with(
        db,
        41,
        claim_owner="scheduler-host:123",
        claim_token="claim-token",
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
    )
    heartbeat.assert_owned()
    db.close.assert_called_once_with()


def test_runtime_does_not_write_success_after_heartbeat_loses_fence(monkeypatch):
    db = MagicMock()
    session_factory = MagicMock(return_value=db)
    task = MagicMock(return_value="done")
    run = SimpleNamespace(id=41)
    complete = MagicMock()
    monkeypatch.setattr(scheduler_module, "renew_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler_module, "create_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "claim_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "start_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "complete_scheduler_job_run", complete)

    class LostHeartbeat:
        def start(self):
            return None

        def stop(self):
            return None

        def assert_owned(self):
            raise SchedulerLeaderFenceError("scheduler heartbeat lost")

    runtime = _runtime(session_factory, lambda **_kwargs: LostHeartbeat())
    runtime.record_scheduled_runs(
        SimpleNamespace(
            job_id="query_governance_job",
            scheduled_run_times=[datetime.datetime(2026, 8, 7, 0, 30, tzinfo=UTC)],
        )
    )

    with pytest.raises(SchedulerLeaderFenceError, match="heartbeat lost"):
        runtime.execute("query_governance_job", task)

    task.assert_called_once_with()
    complete.assert_not_called()
    db.close.assert_called_once_with()


def test_runtime_terminalizes_unknown_when_run_heartbeat_cannot_start(monkeypatch):
    db = MagicMock()
    session_factory = MagicMock(return_value=db)
    task = MagicMock(return_value="done")
    run = SimpleNamespace(id=41)
    complete = MagicMock()
    monkeypatch.setattr(scheduler_module, "renew_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler_module, "create_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "claim_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "start_scheduler_job_run", MagicMock(return_value=run))
    monkeypatch.setattr(scheduler_module, "complete_scheduler_job_run", complete)

    class StartFailureHeartbeat:
        def start(self):
            raise RuntimeError("heartbeat thread failed to start")

        def stop(self):
            return None

    runtime = _runtime(session_factory, lambda **_kwargs: StartFailureHeartbeat())
    runtime.record_scheduled_runs(
        SimpleNamespace(
            job_id="query_governance_job",
            scheduled_run_times=[datetime.datetime(2026, 8, 7, 0, 30, tzinfo=UTC)],
        )
    )

    with pytest.raises(RuntimeError, match="heartbeat thread failed to start"):
        runtime.execute("query_governance_job", task)

    task.assert_not_called()
    complete.assert_called_once_with(
        db,
        41,
        claim_owner="scheduler-host:123",
        claim_token=ANY,
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        status="execution_unknown",
        terminal_summary="scheduler run heartbeat failed before job execution",
    )
    db.close.assert_called_once_with()


def test_successor_recovers_old_running_facts_in_bounded_heartbeat_order():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        old_lease = acquire_scheduler_leader_lease(
            db,
            leader_name="scheduler-main",
            owner_id="leader-old",
            lease_seconds=30,
        )
        run_ids = []
        for minute in (0, 1):
            run = create_scheduler_job_run(
                db,
                job_id="query_governance_job",
                scheduled_for=datetime.datetime(2026, 8, 7, 0, minute, tzinfo=UTC),
                idempotency_key=f"scheduler-run:test:{minute}",
                release=RELEASE,
                config_fingerprint=CONFIG_FINGERPRINT,
            )
            claim_scheduler_job_run(
                db,
                run.id,
                claim_owner="leader-old",
                claim_token=f"claim-{minute}",
                leader_name="scheduler-main",
                owner_id="leader-old",
                fencing_token=old_lease.fencing_token,
            )
            start_scheduler_job_run(
                db,
                run.id,
                claim_owner="leader-old",
                claim_token=f"claim-{minute}",
                leader_name="scheduler-main",
                owner_id="leader-old",
                fencing_token=old_lease.fencing_token,
            )
            run_ids.append(run.id)
        db.execute(
            text(
                "UPDATE scheduler_leader_lease SET lease_expires_at = :expired "
                "WHERE leader_name = :leader_name"
            ),
            {"expired": datetime.datetime(2000, 1, 1), "leader_name": "scheduler-main"},
        )
        db.commit()
        new_lease = acquire_scheduler_leader_lease(
            db,
            leader_name="scheduler-main",
            owner_id="leader-new",
            lease_seconds=30,
        )

        recovered = recover_stale_scheduler_job_runs(
            db,
            leader_name="scheduler-main",
            owner_id="leader-new",
            fencing_token=new_lease.fencing_token,
            limit=1,
        )

        assert recovered == 1
        assert db.get(SchedulerJobRun, run_ids[0]).status == "execution_unknown"
        assert db.get(SchedulerJobRun, run_ids[1]).status == "running"
        assert db.get(SchedulerJobRun, run_ids[0]).terminal_at is not None
    engine.dispose()


def test_successor_recovers_same_owner_future_heartbeat_from_previous_fence():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        old_lease = acquire_scheduler_leader_lease(
            db,
            leader_name="scheduler-main",
            owner_id="stable-owner",
            lease_seconds=30,
        )
        old_fencing_token = old_lease.fencing_token
        run = create_scheduler_job_run(
            db,
            job_id="query_governance_job",
            scheduled_for=datetime.datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
            idempotency_key="scheduler-run:same-owner",
            release=RELEASE,
            config_fingerprint=CONFIG_FINGERPRINT,
        )
        claim_scheduler_job_run(
            db,
            run.id,
            claim_owner="stable-owner",
            claim_token="old-claim",
            leader_name="scheduler-main",
            owner_id="stable-owner",
            fencing_token=old_fencing_token,
        )
        start_scheduler_job_run(
            db,
            run.id,
            claim_owner="stable-owner",
            claim_token="old-claim",
            leader_name="scheduler-main",
            owner_id="stable-owner",
            fencing_token=old_fencing_token,
        )
        db.execute(
            text(
                "UPDATE scheduler_job_run SET heartbeat_at = :future WHERE id = :run_id"
            ),
            {"future": datetime.datetime(2099, 1, 1), "run_id": run.id},
        )
        db.execute(
            text(
                "UPDATE scheduler_leader_lease SET lease_expires_at = :expired "
                "WHERE leader_name = :leader_name"
            ),
            {"expired": datetime.datetime(2000, 1, 1), "leader_name": "scheduler-main"},
        )
        db.commit()
        new_lease = acquire_scheduler_leader_lease(
            db,
            leader_name="scheduler-main",
            owner_id="stable-owner",
            lease_seconds=30,
        )

        recovered = recover_stale_scheduler_job_runs(
            db,
            leader_name="scheduler-main",
            owner_id="stable-owner",
            fencing_token=new_lease.fencing_token,
        )

        assert new_lease.fencing_token > old_fencing_token
        assert recovered == 1
        assert db.get(SchedulerJobRun, run.id).status == "execution_unknown"
    engine.dispose()


def test_scheduler_releases_new_leader_when_stale_recovery_fails(monkeypatch):
    db = MagicMock()
    lease = SimpleNamespace(fencing_token=9)
    release = MagicMock()
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(
        scheduler_module,
        "acquire_scheduler_leader_lease",
        MagicMock(return_value=lease),
    )
    monkeypatch.setattr(
        scheduler_module,
        "recover_stale_scheduler_job_runs",
        MagicMock(side_effect=RuntimeError("recovery failed")),
    )
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", release)

    with pytest.raises(RuntimeError, match="recovery failed"):
        scheduler_module.start_scheduler(owner_id="leader-new")

    release.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="leader-new",
        fencing_token=9,
    )
    db.close.assert_called_once_with()


def test_scheduler_releases_new_leader_when_heartbeat_start_fails(monkeypatch):
    db = MagicMock()
    lease = SimpleNamespace(fencing_token=9)
    release = MagicMock()
    heartbeat = MagicMock()
    heartbeat.start.side_effect = RuntimeError("heartbeat thread failed to start")
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(
        scheduler_module,
        "acquire_scheduler_leader_lease",
        MagicMock(return_value=lease),
    )
    monkeypatch.setattr(
        scheduler_module,
        "recover_stale_scheduler_job_runs",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", release)
    monkeypatch.setattr(
        scheduler_module,
        "SchedulerLeaseHeartbeat",
        MagicMock(return_value=heartbeat),
    )

    with pytest.raises(RuntimeError, match="heartbeat thread failed to start"):
        scheduler_module.start_scheduler(owner_id="leader-new")

    release.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="leader-new",
        fencing_token=9,
    )


def test_scheduler_releases_database_lease_when_heartbeat_stop_times_out(monkeypatch):
    scheduler = MagicMock()
    db = MagicMock()
    lease = SimpleNamespace(fencing_token=9)
    release = MagicMock()
    heartbeat = MagicMock()
    heartbeat.stop.side_effect = SchedulerLeaderFenceError(
        "scheduler heartbeat thread did not stop before timeout"
    )
    heartbeat.assert_owned = MagicMock()
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", MagicMock(return_value=scheduler))
    monkeypatch.setattr(scheduler_module, "SessionLocal", MagicMock(return_value=db))
    monkeypatch.setattr(
        scheduler_module,
        "acquire_scheduler_leader_lease",
        MagicMock(return_value=lease),
    )
    monkeypatch.setattr(
        scheduler_module,
        "recover_stale_scheduler_job_runs",
        MagicMock(return_value=0),
    )
    monkeypatch.setattr(scheduler_module, "release_scheduler_leader_lease", release)
    monkeypatch.setattr(
        scheduler_module,
        "SchedulerLeaseHeartbeat",
        MagicMock(return_value=heartbeat),
    )
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})

    with pytest.raises(SchedulerLeaderFenceError, match="did not stop"):
        scheduler_module.start_scheduler(
            owner_id="leader-new",
            release=RELEASE,
            config_fingerprint=CONFIG_FINGERPRINT,
        )

    release.assert_called_once_with(
        db,
        leader_name="scheduler-main",
        owner_id="leader-new",
        fencing_token=9,
    )


@pytest.mark.parametrize("interval_seconds", [0, -1, 30])
def test_scheduler_heartbeat_rejects_non_positive_or_non_renewing_interval(interval_seconds):
    with pytest.raises(ValueError, match="heartbeat interval"):
        recovery_module.SchedulerLeaseHeartbeat(
            leader_name="scheduler-main",
            owner_id="scheduler-host:123",
            fencing_token=7,
            lease_seconds=30,
            interval_seconds=interval_seconds,
        )


def test_scheduler_heartbeat_thread_records_renewal_failure_and_stops(monkeypatch):
    heartbeat = recovery_module.SchedulerLeaseHeartbeat(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        lease_seconds=30,
        interval_seconds=0.001,
    )
    monkeypatch.setattr(
        heartbeat,
        "beat_once",
        MagicMock(side_effect=RuntimeError("renewal failed")),
    )

    heartbeat.start()
    heartbeat._thread.join(timeout=1)

    with pytest.raises(SchedulerLeaderFenceError, match="lost its live fence"):
        heartbeat.assert_owned()
    heartbeat.stop()
    assert not heartbeat._thread.is_alive()


def test_scheduler_heartbeat_stop_fails_closed_when_thread_does_not_exit():
    heartbeat = recovery_module.SchedulerLeaseHeartbeat(
        leader_name="scheduler-main",
        owner_id="scheduler-host:123",
        fencing_token=7,
        lease_seconds=30,
    )
    stuck_thread = MagicMock()
    stuck_thread.is_alive.return_value = True
    heartbeat._thread = stuck_thread

    with pytest.raises(SchedulerLeaderFenceError, match="did not stop"):
        heartbeat.stop()

    stuck_thread.join.assert_called_once_with(timeout=heartbeat.interval_seconds + 1)
    with pytest.raises(SchedulerLeaderFenceError, match="lost its live fence"):
        heartbeat.assert_owned()
