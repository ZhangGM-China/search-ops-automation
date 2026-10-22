import datetime

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.persistence import database
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
    release_scheduler_leader_lease,
    start_scheduler_job_run,
)


SCHEDULER_LEADER_DOMAIN = "scheduler-main"


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


def _run(session):
    return create_scheduler_job_run(
        session,
        job_id="job-fenced",
        scheduled_for=datetime.datetime(
            2026, 8, 6, 10, 0, tzinfo=datetime.timezone.utc
        ),
        idempotency_key="job-fenced:2026-08-06T10:00:00Z",
        release="release-test",
        config_fingerprint="c" * 64,
    )


def _fence(session, owner_id="leader-a", lease_seconds=30):
    lease = acquire_scheduler_leader_lease(
        session,
        leader_name=SCHEDULER_LEADER_DOMAIN,
        owner_id=owner_id,
        lease_seconds=lease_seconds,
    )
    return {
        "leader_name": SCHEDULER_LEADER_DOMAIN,
        "owner_id": owner_id,
        "fencing_token": lease.fencing_token,
    }


def test_scheduler_job_run_mutations_require_live_fence_and_complete_lifecycle(engine):
    Session = _session_factory(engine)
    with Session() as session:
        run = _run(session)
        fence = _fence(session)
        claimed = claim_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            **fence,
        )
        assert claimed.status == "claimed"
        started = start_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            **fence,
        )
        assert started.status == "running"
        heartbeated = heartbeat_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            **fence,
        )
        assert heartbeated.status == "running"
        completed = complete_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            status="succeeded",
            **fence,
        )
        assert completed.status == "succeeded"


def test_scheduler_job_run_stale_fence_claim_updates_zero_rows(engine):
    Session = _session_factory(engine)
    with Session() as session:
        run = _run(session)
        stale_fence = _fence(session, owner_id="leader-a")
        session.execute(
            text("UPDATE scheduler_leader_lease SET lease_expires_at = :expired "
                 "WHERE leader_name = :leader_name"),
            {"expired": datetime.datetime(2000, 1, 1), "leader_name": "scheduler-main"},
        )
        session.commit()
        successor = _fence(session, owner_id="leader-b")
        assert successor["fencing_token"] == stale_fence["fencing_token"] + 1

        with pytest.raises(SchedulerJobRunClaimError):
            claim_scheduler_job_run(
                session,
                run.id,
                claim_owner="worker-a",
                claim_token="token-a",
                **stale_fence,
            )
        session.expire_all()
        assert session.get(SchedulerJobRun, run.id).status == "planned"


def test_scheduler_job_run_stale_fence_heartbeat_and_terminal_fail_closed(engine):
    Session = _session_factory(engine)
    with Session() as session:
        run = _run(session)
        stale_fence = _fence(session, owner_id="leader-a")
        claim_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            **stale_fence,
        )
        start_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            **stale_fence,
        )
        session.execute(
            text("UPDATE scheduler_leader_lease SET lease_expires_at = :expired "
                 "WHERE leader_name = :leader_name"),
            {"expired": datetime.datetime(2000, 1, 1), "leader_name": "scheduler-main"},
        )
        session.commit()
        _fence(session, owner_id="leader-b")

        with pytest.raises(SchedulerJobRunStateError):
            heartbeat_scheduler_job_run(
                session,
                run.id,
                claim_owner="worker-a",
                claim_token="token-a",
                **stale_fence,
            )
        with pytest.raises(SchedulerJobRunStateError):
            complete_scheduler_job_run(
                session,
                run.id,
                claim_owner="worker-a",
                claim_token="token-a",
                status="succeeded",
                **stale_fence,
            )
        session.expire_all()
        assert session.get(SchedulerJobRun, run.id).status == "running"


def test_scheduler_job_run_fence_is_an_inline_update_condition(engine, monkeypatch):
    Session = _session_factory(engine)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    def _precheck_must_not_run(*_args, **_kwargs):
        raise AssertionError("run mutation must not precheck leader fence")

    monkeypatch.setattr(database, "require_scheduler_leader_fence", _precheck_must_not_run)
    with Session() as session:
        run = _run(session)
        fence = _fence(session)
        statements.clear()
        claim_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            **fence,
        )

    updates = [statement.lower() for statement in statements if "update scheduler_job_run" in statement.lower()]
    assert len(updates) == 1
    assert "exists" in updates[0]
    assert "scheduler_leader_lease" in updates[0]


def test_scheduler_job_run_mutation_signatures_have_required_fence_parameters():
    import inspect

    for mutation in (
        claim_scheduler_job_run,
        start_scheduler_job_run,
        heartbeat_scheduler_job_run,
        complete_scheduler_job_run,
    ):
        parameters = inspect.signature(mutation).parameters
        for name in ("leader_name", "owner_id", "fencing_token"):
            assert parameters[name].default is inspect.Parameter.empty


def _prepare_mutation(session, mutation):
    run = _run(session)
    fence = _fence(session)
    if mutation != "claim":
        claim_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            **fence,
        )
    if mutation in {"heartbeat", "complete"}:
        start_scheduler_job_run(
            session,
            run.id,
            claim_owner="worker-a",
            claim_token="token-a",
            **fence,
        )
    return run, fence


def _mutate_run(session, mutation, run_id, fence):
    kwargs = {
        "claim_owner": "worker-a",
        "claim_token": "token-a",
        **fence,
    }
    if mutation == "claim":
        return claim_scheduler_job_run(session, run_id, **kwargs)
    if mutation == "start":
        return start_scheduler_job_run(session, run_id, **kwargs)
    if mutation == "heartbeat":
        return heartbeat_scheduler_job_run(session, run_id, **kwargs)
    return complete_scheduler_job_run(session, run_id, status="succeeded", **kwargs)


def _invalidate_fence(session, condition, fence):
    if condition == "wrong_owner":
        return {**fence, "owner_id": "leader-b"}
    if condition == "wrong_token":
        return {**fence, "fencing_token": fence["fencing_token"] + 1}
    if condition == "released":
        release_scheduler_leader_lease(
            session,
            leader_name=fence["leader_name"],
            owner_id=fence["owner_id"],
            fencing_token=fence["fencing_token"],
        )
    else:
        session.execute(
            text(
                "UPDATE scheduler_leader_lease SET lease_expires_at = :expired "
                "WHERE leader_name = :leader_name"
            ),
            {
                "expired": datetime.datetime(2000, 1, 1),
                "leader_name": SCHEDULER_LEADER_DOMAIN,
            },
        )
    session.commit()
    return fence


@pytest.mark.parametrize("mutation", ("claim", "start", "heartbeat", "complete"))
@pytest.mark.parametrize("condition", ("wrong_owner", "wrong_token", "released", "expired"))
def test_scheduler_job_run_each_mutation_rejects_invalid_live_fence(
    engine,
    mutation,
    condition,
):
    Session = _session_factory(engine)
    with Session() as session:
        run, fence = _prepare_mutation(session, mutation)
        before_status = run.status
        invalid_fence = _invalidate_fence(session, condition, fence)
        update_rowcounts = []

        @event.listens_for(engine, "after_cursor_execute")
        def _capture_rowcount(_connection, cursor, statement, _parameters, _context, _executemany):
            if "update scheduler_job_run" in statement.lower():
                update_rowcounts.append(cursor.rowcount)

        with pytest.raises((SchedulerJobRunClaimError, SchedulerJobRunStateError)):
            _mutate_run(session, mutation, run.id, invalid_fence)

        session.expire_all()
        assert update_rowcounts == [0]
        assert session.get(SchedulerJobRun, run.id).status == before_status


@pytest.mark.parametrize("mutation", ("claim", "start", "heartbeat", "complete"))
def test_scheduler_job_run_each_mutation_uses_all_live_fence_conditions_in_update(
    engine,
    mutation,
):
    Session = _session_factory(engine)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture_statement(_connection, _cursor, statement, parameters, _context, _executemany):
        statements.append((statement.lower(), parameters))

    with Session() as session:
        run, fence = _prepare_mutation(session, mutation)
        statements.clear()
        _mutate_run(session, mutation, run.id, fence)

    updates = [
        (statement, parameters)
        for statement, parameters in statements
        if "update scheduler_job_run" in statement
    ]
    assert len(updates) == 1
    statement, parameters = updates[0]
    for column in (
        "scheduler_leader_lease.leader_name",
        "scheduler_leader_lease.owner_id",
        "scheduler_leader_lease.fencing_token",
        "scheduler_leader_lease.status",
        "scheduler_leader_lease.lease_expires_at",
    ):
        assert column in statement
    assert "exists" in statement
    assert "current_timestamp" in statement
    assert SCHEDULER_LEADER_DOMAIN in tuple(parameters)
    assert "leader-a" in tuple(parameters)
    assert fence["fencing_token"] in tuple(parameters)
    assert "active" in tuple(parameters)


def test_scheduler_job_run_rejects_live_fence_from_another_leader_domain(engine):
    Session = _session_factory(engine)
    with Session() as session:
        run = _run(session)
        other_lease = acquire_scheduler_leader_lease(
            session,
            leader_name="scheduler-secondary",
            owner_id="leader-b",
            lease_seconds=30,
        )

        with pytest.raises(SchedulerJobRunClaimError):
            claim_scheduler_job_run(
                session,
                run.id,
                claim_owner="worker-a",
                claim_token="token-a",
                leader_name="scheduler-secondary",
                owner_id="leader-b",
                fencing_token=other_lease.fencing_token,
            )

        session.expire_all()
        assert session.get(SchedulerJobRun, run.id).status == "planned"


@pytest.mark.parametrize("invalid_token", (0, True))
def test_scheduler_job_run_rejects_invalid_fencing_token_before_claim(engine, invalid_token):
    Session = _session_factory(engine)
    with Session() as session:
        run = _run(session)
        fence = _fence(session)

        with pytest.raises(SchedulerJobRunStateError, match="fencing token is invalid"):
            claim_scheduler_job_run(
                session,
                run.id,
                claim_owner="worker-a",
                claim_token="token-a",
                fencing_token=invalid_token,
                leader_name=fence["leader_name"],
                owner_id=fence["owner_id"],
            )

        session.expire_all()
        assert session.get(SchedulerJobRun, run.id).status == "planned"
