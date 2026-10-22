import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.persistence import (
    Base,
    SchedulerJobRun,
    SchedulerJobRunClaimError,
    SchedulerJobRunStateError,
    acquire_scheduler_leader_lease,
    claim_scheduler_job_run,
    complete_scheduler_job_run,
    create_scheduler_job_run,
    heartbeat_scheduler_job_run,
    start_scheduler_job_run,
)
from src.migrations.release_schema import ensure_release_schema, verify_release_schema


@pytest.fixture()
def engine():
    sqlite_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(sqlite_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=sqlite_engine)
    yield sqlite_engine
    sqlite_engine.dispose()


def _session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def _sqlite_file_engine(uri):
    sqlite_engine = create_engine(uri)

    @event.listens_for(sqlite_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return sqlite_engine


def _create_run(session, *, key="job-1:2026-08-06T10:00:00Z"):
    return create_scheduler_job_run(
        session,
        job_id="job-1",
        scheduled_for=datetime.datetime(2026, 8, 6, 10, 0, tzinfo=datetime.timezone.utc),
        idempotency_key=key,
        release="release-test",
        config_fingerprint="c" * 64,
    )


def _leader_fence(session):
    lease = acquire_scheduler_leader_lease(
        session,
        leader_name="scheduler-main",
        owner_id="leader-a",
        lease_seconds=30,
    )
    return {
        "leader_name": "scheduler-main",
        "owner_id": "leader-a",
        "fencing_token": lease.fencing_token,
    }


def test_scheduler_job_run_persists_full_lifecycle_and_rejects_illegal_transition(engine):
    Session = _session_factory(engine)
    with Session() as session:
        run = _create_run(session)
        fence = _leader_fence(session)
        assert run.status == "planned"
        assert run.attempt_no == 0
        assert run.created_at is not None

        claim_scheduler_job_run(session, run.id, claim_owner="worker-a", claim_token="token-a", **fence)
        start_scheduler_job_run(session, run.id, claim_owner="worker-a", claim_token="token-a", **fence)
        heartbeat_scheduler_job_run(session, run.id, claim_owner="worker-a", claim_token="token-a", **fence)
        complete_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            status="succeeded",
            terminal_summary="ok",
            **fence,
        )

        persisted = session.get(SchedulerJobRun, run.id)
        assert persisted.status == "succeeded"
        assert persisted.started_at is not None
        assert persisted.heartbeat_at is not None
        assert persisted.terminal_at is not None
        assert persisted.release == "release-test"
        assert persisted.config_fingerprint == "c" * 64

        with pytest.raises(SchedulerJobRunStateError):
            heartbeat_scheduler_job_run(
                session, run.id, claim_owner="worker-a", claim_token="token-a", **fence
            )


def test_scheduler_job_run_created_at_is_utc_naive_and_precedes_lifecycle(engine):
    Session = _session_factory(engine)
    with Session() as session:
        run = _create_run(session, key="utc-created-at")
        fence = _leader_fence(session)
        created_at = run.created_at
        expected_utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        assert created_at.tzinfo is None
        assert abs(created_at - expected_utc_now) < datetime.timedelta(seconds=1)

        claimed = claim_scheduler_job_run(
            session, run.id, claim_owner="worker-a", claim_token="token-a", **fence
        )
        started = start_scheduler_job_run(
            session, run.id, claim_owner="worker-a", claim_token="token-a", **fence
        )
        heartbeated = heartbeat_scheduler_job_run(
            session, run.id, claim_owner="worker-a", claim_token="token-a", **fence
        )
        completed = complete_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            status="succeeded",
            **fence,
        )

        assert created_at <= claimed.claimed_at <= started.started_at
        assert started.started_at <= heartbeated.heartbeat_at <= completed.terminal_at


def test_scheduler_job_run_duplicate_idempotency_and_slot_fail_closed(engine):
    Session = _session_factory(engine)
    with Session() as session:
        _create_run(session)
        with pytest.raises(SchedulerJobRunStateError):
            _create_run(session)
        with pytest.raises(SchedulerJobRunStateError):
            create_scheduler_job_run(
                session,
                job_id="job-1",
                scheduled_for=datetime.datetime(2026, 8, 6, 10, 0, tzinfo=datetime.timezone.utc),
                idempotency_key="different-key",
                release="release-test",
                config_fingerprint="c" * 64,
            )


def test_scheduler_job_run_rejects_naive_scheduled_for(engine):
    Session = _session_factory(engine)
    with Session() as session:
        with pytest.raises(SchedulerJobRunStateError, match="timezone-aware"):
            create_scheduler_job_run(
                session,
                job_id="job-1",
                scheduled_for=datetime.datetime(2026, 8, 6, 10, 0),
                idempotency_key="naive-slot",
                release="release-test",
                config_fingerprint="c" * 64,
            )


def test_scheduler_job_run_normalizes_equivalent_aware_slots_to_utc_naive(engine):
    Session = _session_factory(engine)
    with Session() as session:
        run = _create_run(session)
        assert run.scheduled_for == datetime.datetime(2026, 8, 6, 10, 0)
        assert run.scheduled_for.tzinfo is None

        with pytest.raises(SchedulerJobRunStateError):
            create_scheduler_job_run(
                session,
                job_id="job-1",
                scheduled_for=datetime.datetime(
                    2026,
                    8,
                    6,
                    18,
                    0,
                    tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
                ),
                idempotency_key="equivalent-offset-slot",
                release="release-test",
                config_fingerprint="c" * 64,
            )


def test_scheduler_job_run_atomic_claim_allows_one_winner(tmp_path):
    engine = _sqlite_file_engine(f"sqlite:///{tmp_path / 'atomic-claim.db'}")
    Base.metadata.create_all(bind=engine)
    Session = _session_factory(engine)
    with Session() as session:
        run = _create_run(session)
        run_id = run.id
        fence = _leader_fence(session)

    def attempt(owner):
        with Session() as session:
            try:
                claim_scheduler_job_run(
                    session, run_id, claim_owner=owner, claim_token=f"token-{owner}", **fence
                )
                return "claimed"
            except SchedulerJobRunClaimError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(attempt, ("a", "b")))
    assert results == ["claimed", "rejected"]
    engine.dispose()


def test_scheduler_job_run_wrong_owner_and_duplicate_terminal_fail_closed(engine):
    Session = _session_factory(engine)
    with Session() as session:
        run = _create_run(session)
        fence = _leader_fence(session)
        with pytest.raises(SchedulerJobRunStateError):
            claim_scheduler_job_run(session, run.id, claim_owner=" ", claim_token="token-a", **fence)
        claim_scheduler_job_run(session, run.id, claim_owner="worker-a", claim_token="token-a", **fence)
        with pytest.raises(SchedulerJobRunClaimError):
            start_scheduler_job_run(session, run.id, claim_owner="worker-b", claim_token="token-b", **fence)
        start_scheduler_job_run(session, run.id, claim_owner="worker-a", claim_token="token-a", **fence)
        with pytest.raises(SchedulerJobRunStateError):
            complete_scheduler_job_run(
                session,
                run.id,
                claim_owner="worker-a",
                claim_token="token-a",
                status="claimed",
                **fence,
            )
        complete_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            status="failed",
            terminal_summary="error",
            **fence,
        )
        with pytest.raises(SchedulerJobRunStateError):
            complete_scheduler_job_run(
                session,
                run.id,
                claim_owner="worker-a",
                claim_token="token-a",
                status="succeeded",
                terminal_summary="late",
                **fence,
            )


def test_scheduler_job_run_survives_engine_restart_and_migration_is_verified(tmp_path):
    path = tmp_path / "scheduler-job-run.db"
    uri = f"sqlite:///{path}"
    first_engine = _sqlite_file_engine(uri)
    ensure_release_schema(first_engine)
    Session = _session_factory(first_engine)
    with Session() as session:
        run = _create_run(session)
        fence = _leader_fence(session)
        claim_scheduler_job_run(session, run.id, claim_owner="worker-a", claim_token="token-a", **fence)
        start_scheduler_job_run(session, run.id, claim_owner="worker-a", claim_token="token-a", **fence)
        complete_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            status="succeeded",
            **fence,
        )
        run_id = run.id
    first_engine.dispose()

    second_engine = _sqlite_file_engine(uri)
    ensure_release_schema(second_engine)
    with _session_factory(second_engine)() as session:
        persisted = session.get(SchedulerJobRun, run_id)
        assert persisted is not None
        assert persisted.idempotency_key == "job-1:2026-08-06T10:00:00Z"
        assert persisted.status == "succeeded"
        assert persisted.terminal_at is not None
    verification = verify_release_schema(second_engine)
    assert verification["ok"] is True
    assert SchedulerJobRun.__tablename__ in verification["tables"]
    assert inspect(second_engine).get_table_names().count("scheduler_job_run") == 1
    second_engine.dispose()
