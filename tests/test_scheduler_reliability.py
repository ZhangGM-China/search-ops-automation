import datetime
from types import SimpleNamespace

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.persistence import Base, SchedulerJobRun
from src.scheduler.reliability import (
    SchedulerCompensationPlanError,
    persist_scheduler_compensation_plan,
    plan_and_persist_missed_scheduler_runs,
    plan_missed_scheduler_runs,
)


UTC = datetime.timezone.utc


def _dt(day: int, hour: int = 0, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def test_plans_cron_missed_slots_in_order_with_stable_audit_identity() -> None:
    trigger = CronTrigger(hour=0, minute=30, timezone=UTC)

    first = plan_missed_scheduler_runs(
        job_id="query_governance_job",
        trigger=trigger,
        last_scheduled_for=_dt(1, 0, 30),
        observed_at=_dt(4, 12),
        compensation_window=datetime.timedelta(days=7),
        max_compensations=10,
    )
    second = plan_missed_scheduler_runs(
        job_id="query_governance_job",
        trigger=trigger,
        last_scheduled_for=_dt(1, 0, 30),
        observed_at=_dt(4, 12),
        compensation_window=datetime.timedelta(days=7),
        max_compensations=10,
    )

    assert first == second
    assert first["plannedCount"] == 3
    assert first["windowTruncated"] is False
    assert first["limitTruncated"] is False
    assert [intent["scheduledFor"] for intent in first["intents"]] == [
        "2026-08-02T00:30:00Z",
        "2026-08-03T00:30:00Z",
        "2026-08-04T00:30:00Z",
    ]
    assert all(intent["source"] == "compensation" for intent in first["intents"])
    assert all(intent["reason"] == "missed_schedule" for intent in first["intents"])
    assert len({intent["idempotencyKey"] for intent in first["intents"]}) == 3
    assert all(
        intent["idempotencyKey"].startswith("scheduler-run:v1:")
        for intent in first["intents"]
    )


def test_compensation_window_excludes_old_slots_and_reports_truncation() -> None:
    trigger = CronTrigger(hour=0, minute=30, timezone=UTC)

    plan = plan_missed_scheduler_runs(
        job_id="daily_job",
        trigger=trigger,
        last_scheduled_for=_dt(1, 0, 30),
        observed_at=_dt(10, 12),
        compensation_window=datetime.timedelta(days=2),
        max_compensations=10,
    )

    assert plan["windowStart"] == "2026-08-08T12:00:00Z"
    assert plan["windowTruncated"] is True
    assert [intent["scheduledFor"] for intent in plan["intents"]] == [
        "2026-08-09T00:30:00Z",
        "2026-08-10T00:30:00Z",
    ]


def test_max_compensations_bounds_each_plan_without_coalescing_slots() -> None:
    trigger = IntervalTrigger(
        hours=1,
        start_date=_dt(1),
        timezone=UTC,
    )

    plan = plan_missed_scheduler_runs(
        job_id="hourly_job",
        trigger=trigger,
        last_scheduled_for=_dt(1, 0),
        observed_at=_dt(1, 6, 30),
        compensation_window=datetime.timedelta(days=1),
        max_compensations=2,
    )

    assert plan["plannedCount"] == 2
    assert plan["limitTruncated"] is True
    assert [intent["scheduledFor"] for intent in plan["intents"]] == [
        "2026-08-01T01:00:00Z",
        "2026-08-01T02:00:00Z",
    ]


def test_slot_at_observation_time_is_not_yet_missed() -> None:
    trigger = IntervalTrigger(
        hours=1,
        start_date=_dt(1),
        timezone=UTC,
    )

    plan = plan_missed_scheduler_runs(
        job_id="hourly_job",
        trigger=trigger,
        last_scheduled_for=_dt(1, 0),
        observed_at=_dt(1, 2),
        compensation_window=datetime.timedelta(days=1),
        max_compensations=10,
    )

    assert [intent["scheduledFor"] for intent in plan["intents"]] == [
        "2026-08-01T01:00:00Z",
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"job_id": "  "}, "job_id"),
        ({"last_scheduled_for": datetime.datetime(2026, 8, 1)}, "timezone-aware"),
        ({"observed_at": datetime.datetime(2026, 8, 2)}, "timezone-aware"),
        ({"compensation_window": datetime.timedelta(0)}, "compensation_window"),
        ({"max_compensations": 0}, "max_compensations"),
        ({"max_compensations": 101}, "max_compensations"),
    ],
)
def test_invalid_planning_contract_fails_closed(overrides, message: str) -> None:
    arguments = {
        "job_id": "daily_job",
        "trigger": CronTrigger(hour=0, timezone=UTC),
        "last_scheduled_for": _dt(1),
        "observed_at": _dt(2, 12),
        "compensation_window": datetime.timedelta(days=1),
        "max_compensations": 10,
    }
    arguments.update(overrides)

    with pytest.raises(ValueError, match=message):
        plan_missed_scheduler_runs(**arguments)


def test_non_advancing_trigger_fails_closed() -> None:
    class NonAdvancingTrigger:
        def get_next_fire_time(self, previous_fire_time, now):
            return previous_fire_time or now

    with pytest.raises(ValueError, match="advance"):
        plan_missed_scheduler_runs(
            job_id="broken_trigger_job",
            trigger=NonAdvancingTrigger(),
            last_scheduled_for=_dt(1),
            observed_at=_dt(2),
            compensation_window=datetime.timedelta(days=2),
            max_compensations=10,
        )


@pytest.fixture()
def scheduler_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        yield session
    engine.dispose()


def _compensation_plan() -> dict:
    return plan_missed_scheduler_runs(
        job_id="daily_job",
        trigger=CronTrigger(hour=0, minute=30, timezone=UTC),
        last_scheduled_for=_dt(1, 0, 30),
        observed_at=_dt(4, 12),
        compensation_window=datetime.timedelta(days=7),
        max_compensations=10,
    )


def test_persists_compensation_slots_as_unclaimed_durable_facts(scheduler_session) -> None:
    result = persist_scheduler_compensation_plan(
        scheduler_session,
        plan=_compensation_plan(),
        release="release-test",
        config_fingerprint="c" * 64,
    )

    rows = scheduler_session.query(SchedulerJobRun).order_by(SchedulerJobRun.scheduled_for).all()
    assert result == {"plannedCount": 3, "createdCount": 3, "existingCount": 0}
    assert [row.status for row in rows] == ["planned", "planned", "planned"]
    assert [row.attempt_no for row in rows] == [0, 0, 0]
    assert all(row.claim_owner is None and row.claim_token is None for row in rows)
    assert all(row.release == "release-test" for row in rows)
    assert all(row.config_fingerprint == "c" * 64 for row in rows)


def test_persisting_same_compensation_plan_is_idempotent(scheduler_session) -> None:
    first = persist_scheduler_compensation_plan(
        scheduler_session,
        plan=_compensation_plan(),
        release="release-test",
        config_fingerprint="c" * 64,
    )
    second = persist_scheduler_compensation_plan(
        scheduler_session,
        plan=_compensation_plan(),
        release="release-test",
        config_fingerprint="c" * 64,
    )

    assert first["createdCount"] == 3
    assert second == {"plannedCount": 3, "createdCount": 0, "existingCount": 3}
    assert scheduler_session.query(SchedulerJobRun).count() == 3


def test_invalid_compensation_plan_rolls_back_without_partial_facts(scheduler_session) -> None:
    plan = _compensation_plan()
    plan["intents"][1]["jobId"] = "other_job"

    with pytest.raises(SchedulerCompensationPlanError, match="jobId"):
        persist_scheduler_compensation_plan(
            scheduler_session,
            plan=plan,
            release="release-test",
            config_fingerprint="c" * 64,
        )

    assert scheduler_session.query(SchedulerJobRun).count() == 0


def test_plans_from_latest_attempted_fact_and_does_not_execute_compensations(scheduler_session) -> None:
    scheduler_session.add_all([
        SchedulerJobRun(
            job_id="daily_job",
            scheduled_for=_dt(1, 0, 30).replace(tzinfo=None),
            idempotency_key="completed-baseline",
            status="succeeded",
            attempt_no=1,
            claim_owner="worker-a",
            claim_token="token-a",
            release="release-old",
            config_fingerprint="a" * 64,
            claimed_at=_dt(1, 0, 31).replace(tzinfo=None),
            started_at=_dt(1, 0, 31).replace(tzinfo=None),
            heartbeat_at=_dt(1, 0, 31).replace(tzinfo=None),
            terminal_at=_dt(1, 0, 32).replace(tzinfo=None),
        ),
        SchedulerJobRun(
            job_id="daily_job",
            scheduled_for=_dt(3, 0, 30).replace(tzinfo=None),
            idempotency_key="already-planned",
            status="planned",
            attempt_no=0,
            release="release-test",
            config_fingerprint="c" * 64,
        ),
    ])
    scheduler_session.commit()
    business_job = SimpleNamespace(
        id="daily_job",
        trigger=CronTrigger(hour=0, minute=30, timezone=UTC),
    )

    result = plan_and_persist_missed_scheduler_runs(
        scheduler_session,
        jobs=[business_job],
        observed_at=_dt(4, 12),
        compensation_window=datetime.timedelta(days=7),
        max_compensations=10,
        release="release-test",
        config_fingerprint="c" * 64,
    )

    rows = scheduler_session.query(SchedulerJobRun).filter(
        SchedulerJobRun.job_id == "daily_job"
    ).order_by(SchedulerJobRun.scheduled_for).all()
    assert result == {"jobCount": 1, "plannedCount": 3, "createdCount": 2, "existingCount": 1}
    assert [row.scheduled_for for row in rows] == [
        _dt(1, 0, 30).replace(tzinfo=None),
        _dt(2, 0, 30).replace(tzinfo=None),
        _dt(3, 0, 30).replace(tzinfo=None),
        _dt(4, 0, 30).replace(tzinfo=None),
    ]
    assert [row.status for row in rows[1:]] == ["planned", "planned", "planned"]
