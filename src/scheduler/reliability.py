import datetime
import hashlib
from typing import Any

from sqlalchemy.exc import IntegrityError

from src.persistence import SchedulerJobRun


MAX_COMPENSATIONS_PER_PLAN = 100


class SchedulerCompensationPlanError(RuntimeError):
    pass


def _require_aware(value: datetime.datetime, field_name: str) -> datetime.datetime:
    if not isinstance(value, datetime.datetime) or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(datetime.timezone.utc)


def _utc_iso(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _next_fire_time(trigger: Any, previous: datetime.datetime | None, now: datetime.datetime):
    get_next_fire_time = getattr(trigger, "get_next_fire_time", None)
    if not callable(get_next_fire_time):
        raise ValueError("trigger must provide get_next_fire_time")
    candidate = get_next_fire_time(previous, now)
    if candidate is None:
        return None
    candidate_utc = _require_aware(candidate, "trigger fire time")
    if previous is not None and candidate_utc <= previous.astimezone(datetime.timezone.utc):
        raise ValueError("trigger must advance fire times")
    return candidate_utc


def scheduler_compensation_idempotency_key(
    job_id: str,
    scheduled_for: datetime.datetime,
) -> str:
    identity = f"scheduler-run:v1\0{job_id}\0{_utc_iso(scheduled_for)}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"scheduler-run:v1:{digest}"


_scheduler_run_idempotency_key = scheduler_compensation_idempotency_key


def plan_missed_scheduler_runs(
    *,
    job_id: str,
    trigger: Any,
    last_scheduled_for: datetime.datetime,
    observed_at: datetime.datetime,
    compensation_window: datetime.timedelta,
    max_compensations: int,
) -> dict[str, Any]:
    """Build bounded, deterministic compensation intents without executing jobs."""
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id must be non-empty")
    if job_id != job_id.strip() or any(ord(character) < 32 for character in job_id):
        raise ValueError("job_id must be normalized")
    if (
        not isinstance(compensation_window, datetime.timedelta)
        or compensation_window <= datetime.timedelta(0)
    ):
        raise ValueError("compensation_window must be positive")
    if (
        isinstance(max_compensations, bool)
        or not isinstance(max_compensations, int)
        or not 1 <= max_compensations <= MAX_COMPENSATIONS_PER_PLAN
    ):
        raise ValueError(
            f"max_compensations must be between 1 and {MAX_COMPENSATIONS_PER_PLAN}"
        )

    baseline = _require_aware(last_scheduled_for, "last_scheduled_for")
    observed = _require_aware(observed_at, "observed_at")
    if baseline >= observed:
        raise ValueError("last_scheduled_for must be before observed_at")

    window_start = observed - compensation_window
    search_start = max(baseline, window_start)
    candidate = _next_fire_time(trigger, None, search_start)
    eligible_slots: list[datetime.datetime] = []

    while candidate is not None:
        if candidate <= baseline:
            candidate = _next_fire_time(trigger, candidate, candidate)
            continue
        if candidate >= observed:
            break
        if candidate >= window_start:
            eligible_slots.append(candidate)
            if len(eligible_slots) > max_compensations:
                break
        candidate = _next_fire_time(trigger, candidate, candidate)

    planned_slots = eligible_slots[:max_compensations]
    observed_iso = _utc_iso(observed)
    intents = [
        {
            "jobId": job_id,
            "scheduledFor": _utc_iso(scheduled_for),
            "observedAt": observed_iso,
            "source": "compensation",
            "reason": "missed_schedule",
            "idempotencyKey": scheduler_compensation_idempotency_key(job_id, scheduled_for),
        }
        for scheduled_for in planned_slots
    ]
    return {
        "jobId": job_id,
        "baselineScheduledFor": _utc_iso(baseline),
        "observedAt": observed_iso,
        "windowStart": _utc_iso(window_start),
        "windowTruncated": baseline < window_start,
        "limitTruncated": len(eligible_slots) > max_compensations,
        "plannedCount": len(intents),
        "intents": intents,
    }


def _parse_utc_iso(value: Any, field_name: str) -> datetime.datetime:
    if not isinstance(value, str) or not value.strip():
        raise SchedulerCompensationPlanError(f"{field_name} must be a UTC timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchedulerCompensationPlanError(
            f"{field_name} must be a UTC timestamp"
        ) from exc
    if parsed.utcoffset() is None:
        raise SchedulerCompensationPlanError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(datetime.timezone.utc)


def _validate_persistence_identity(release: str, config_fingerprint: str) -> None:
    if not isinstance(release, str) or not release.strip():
        raise SchedulerCompensationPlanError("release must be non-empty")
    if (
        not isinstance(config_fingerprint, str)
        or len(config_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in config_fingerprint)
    ):
        raise SchedulerCompensationPlanError("config_fingerprint must be lowercase sha256")


def _validated_compensation_intents(plan: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(plan, dict):
        raise SchedulerCompensationPlanError("plan must be an object")
    job_id = plan.get("jobId")
    if not isinstance(job_id, str) or not job_id.strip() or job_id != job_id.strip():
        raise SchedulerCompensationPlanError("plan jobId must be normalized")
    raw_intents = plan.get("intents")
    if not isinstance(raw_intents, list):
        raise SchedulerCompensationPlanError("plan intents must be a list")
    if plan.get("plannedCount") != len(raw_intents):
        raise SchedulerCompensationPlanError("plan plannedCount does not match intents")
    observed_at = _parse_utc_iso(plan.get("observedAt"), "plan observedAt")
    validated: list[dict[str, Any]] = []
    seen_slots: set[datetime.datetime] = set()
    for intent in raw_intents:
        if not isinstance(intent, dict):
            raise SchedulerCompensationPlanError("plan intent must be an object")
        if intent.get("jobId") != job_id:
            raise SchedulerCompensationPlanError("plan intent jobId does not match plan")
        if intent.get("source") != "compensation" or intent.get("reason") != "missed_schedule":
            raise SchedulerCompensationPlanError("plan intent is not a missed-run compensation")
        if intent.get("observedAt") != plan.get("observedAt"):
            raise SchedulerCompensationPlanError("plan intent observedAt does not match plan")
        scheduled_for = _parse_utc_iso(intent.get("scheduledFor"), "plan intent scheduledFor")
        if scheduled_for >= observed_at:
            raise SchedulerCompensationPlanError("plan intent scheduledFor must precede observedAt")
        if scheduled_for in seen_slots:
            raise SchedulerCompensationPlanError("plan contains duplicate scheduledFor slots")
        seen_slots.add(scheduled_for)
        expected_key = scheduler_compensation_idempotency_key(job_id, scheduled_for)
        if intent.get("idempotencyKey") != expected_key:
            raise SchedulerCompensationPlanError("plan intent idempotencyKey is invalid")
        validated.append({
            "job_id": job_id,
            "scheduled_for": scheduled_for.replace(tzinfo=None),
            "idempotency_key": expected_key,
        })
    return job_id, validated


def persist_scheduler_compensation_plan(
    db,
    *,
    plan: dict[str, Any],
    release: str,
    config_fingerprint: str,
) -> dict[str, int]:
    """Persist missed slots as unclaimed facts without running any business job."""
    _validate_persistence_identity(release, config_fingerprint)
    job_id, intents = _validated_compensation_intents(plan)
    slots = [intent["scheduled_for"] for intent in intents]
    existing_slots = {
        row.scheduled_for
        for row in (
            db.query(SchedulerJobRun).filter(
                SchedulerJobRun.job_id == job_id,
                SchedulerJobRun.scheduled_for.in_(slots),
            ).all()
            if slots
            else []
        )
    }
    new_intents = [intent for intent in intents if intent["scheduled_for"] not in existing_slots]
    try:
        db.add_all([
            SchedulerJobRun(
                job_id=intent["job_id"],
                scheduled_for=intent["scheduled_for"],
                idempotency_key=intent["idempotency_key"],
                status="planned",
                attempt_no=0,
                release=release,
                config_fingerprint=config_fingerprint,
            )
            for intent in new_intents
        ])
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        persisted_slots = {
            row.scheduled_for
            for row in db.query(SchedulerJobRun).filter(
                SchedulerJobRun.job_id == job_id,
                SchedulerJobRun.scheduled_for.in_(slots),
            ).all()
        }
        if persisted_slots != set(slots):
            raise SchedulerCompensationPlanError(
                "compensation plan persistence conflicted without complete readback"
            ) from exc
        return {
            "plannedCount": len(intents),
            "createdCount": 0,
            "existingCount": len(intents),
        }
    return {
        "plannedCount": len(intents),
        "createdCount": len(new_intents),
        "existingCount": len(existing_slots),
    }


def plan_and_persist_missed_scheduler_runs(
    db,
    *,
    jobs: Any,
    observed_at: datetime.datetime,
    compensation_window: datetime.timedelta,
    max_compensations: int,
    release: str,
    config_fingerprint: str,
) -> dict[str, int]:
    """Plan from attempted durable facts and persist only; never claim or execute."""
    observed = _require_aware(observed_at, "observed_at")
    totals = {"jobCount": 0, "plannedCount": 0, "createdCount": 0, "existingCount": 0}
    normalized_jobs = sorted(list(jobs), key=lambda item: str(getattr(item, "id", "")))
    for job in normalized_jobs:
        job_id = getattr(job, "id", None)
        trigger = getattr(job, "trigger", None)
        if not isinstance(job_id, str) or not job_id or not callable(
            getattr(trigger, "get_next_fire_time", None)
        ):
            continue
        baseline = db.query(SchedulerJobRun).filter(
            SchedulerJobRun.job_id == job_id,
            SchedulerJobRun.attempt_no > 0,
        ).order_by(
            SchedulerJobRun.scheduled_for.desc(),
            SchedulerJobRun.id.desc(),
        ).first()
        if baseline is None:
            continue
        plan = plan_missed_scheduler_runs(
            job_id=job_id,
            trigger=trigger,
            last_scheduled_for=baseline.scheduled_for.replace(tzinfo=datetime.timezone.utc),
            observed_at=observed,
            compensation_window=compensation_window,
            max_compensations=max_compensations,
        )
        persisted = persist_scheduler_compensation_plan(
            db,
            plan=plan,
            release=release,
            config_fingerprint=config_fingerprint,
        )
        totals["jobCount"] += 1
        for key in ("plannedCount", "createdCount", "existingCount"):
            totals[key] += persisted[key]
    return totals
