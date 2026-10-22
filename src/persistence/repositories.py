import os
import json
import logging
import math
from pathlib import Path
from sqlalchemy import CheckConstraint, create_engine, Column, Integer, String, Float, Date, DateTime, Text, ForeignKey, ForeignKeyConstraint, Index, func, inspect, text, UniqueConstraint
from sqlalchemy import event
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import close_all_sessions, declarative_base, relationship, sessionmaker
import datetime
import hashlib
import re
from typing import List, Any, Dict, Optional
from dotenv import load_dotenv

from src.config import get_env
from .mysql_timeouts import mysql_timeout_connect_args

load_dotenv()

logger = logging.getLogger(__name__)

from .models import *  # noqa: F401,F403
from .models import _OPERATION_LOG_SENSITIVE_VALUE, _utc_naive_now
from .schema import _SCHEDULER_JOB_RUN_TERMINAL_STATUSES, _ensure_schema_upgrades
from .session import SessionLocal
from .inventory_recovery_dual_write import (
    count_inventory_recovery_tasks,
    read_inventory_recovery_tasks,
    sync_inventory_recovery_task,
)

logger = logging.getLogger(__name__)

def _sanitize_operation_log_payload(
    value: object,
    *,
    max_chars: int = OPERATION_LOG_PAYLOAD_MAX_CHARS,
) -> str | None:
    if value is None:
        return None
    payload = _OPERATION_LOG_SENSITIVE_VALUE.sub(r'\1[REDACTED]', str(value))
    if len(payload) <= max_chars:
        return payload
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    suffix = f"... [truncated sha256={digest}]"
    return payload[:max_chars - len(suffix)] + suffix


def _operation_log_payload_value(
    value: object,
    *,
    action: object,
    status: object,
    payload_policy: object = None,
    max_chars: int = OPERATION_LOG_PAYLOAD_MAX_CHARS,
) -> str | None:
    """Keep payloads only where readback or failure diagnosis needs them."""
    if value is None:
        return None
    normalized_action = str(action or "").strip()
    normalized_status = str(status or "").strip().lower()
    normalized_policy = str(payload_policy or "").strip().lower()
    if normalized_status == "success":
        if normalized_policy in {"none", "failure_only"}:
            return None
        if not normalized_policy and normalized_action not in OPERATION_LOG_PAYLOAD_REQUIRED_ACTIONS:
            return None
    return _sanitize_operation_log_payload(value, max_chars=max_chars)


def _operation_log_retention_cutoff(now: datetime.datetime | None = None) -> datetime.datetime:
    reference = now or datetime.datetime.now()
    if get_env() == "test":
        return reference - datetime.timedelta(days=OPERATION_LOG_TEST_RETENTION_DAYS)
    current_month_start = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return min(reference - datetime.timedelta(days=OPERATION_LOG_RETENTION_DAYS), current_month_start)


class OperationLogRetentionIndexMissing(RuntimeError):
    pass


def _has_operation_log_retention_index(db) -> bool:
    indexes = inspect(db.bind).get_indexes(OperationLog.__tablename__)
    return any(
        tuple(index.get("column_names") or ())[:2] == OPERATION_LOG_RETENTION_INDEX_COLUMNS
        for index in indexes
    )


def purge_expired_operation_logs(
    now: datetime.datetime | None = None,
    batch_size: int = OPERATION_LOG_RETENTION_BATCH_SIZE,
) -> int:
    """Delete one bounded batch without crossing the current calendar month."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    cutoff = _operation_log_retention_cutoff(now)
    db = SessionLocal()
    try:
        if not _has_operation_log_retention_index(db):
            raise OperationLogRetentionIndexMissing(
                "operation_log retention requires an index on (created_at, id)"
            )
        ids = [
            row_id
            for (row_id,) in db.query(OperationLog.id).filter(
                OperationLog.created_at < cutoff
            ).order_by(
                OperationLog.created_at.asc(),
                OperationLog.id.asc(),
            ).limit(batch_size).all()
        ]
        if not ids:
            return 0
        db.query(OperationLog).filter(OperationLog.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        return len(ids)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# 5. 任务运行会话表 (Req 1: 监控每一轮任务的整体进度)
def _scheduler_job_run_now() -> datetime.datetime:
    return _utc_naive_now()


def _normalize_scheduler_job_run_timestamp(
    value: datetime.datetime,
    field: str,
) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        raise SchedulerJobRunStateError(f"scheduler job run {field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SchedulerJobRunStateError(
            f"scheduler job run {field} must be timezone-aware"
        )
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _raise_if_blank_scheduler_job_run_value(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SchedulerJobRunStateError(f"scheduler job run {field} must be non-empty")


def create_scheduler_job_run(
    db,
    *,
    job_id: str,
    scheduled_for: datetime.datetime,
    idempotency_key: str,
    release: str,
    config_fingerprint: str,
) -> SchedulerJobRun:
    """Persist one scheduled slot before any scheduler side effect begins."""
    for field, value in (
        ("job_id", job_id),
        ("idempotency_key", idempotency_key),
        ("release", release),
        ("config_fingerprint", config_fingerprint),
    ):
        _raise_if_blank_scheduler_job_run_value(value, field)
    run = SchedulerJobRun(
        job_id=job_id,
        scheduled_for=_normalize_scheduler_job_run_timestamp(
            scheduled_for,
            "scheduled_for",
        ),
        idempotency_key=idempotency_key,
        release=release,
        config_fingerprint=config_fingerprint,
    )
    try:
        db.add(run)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise SchedulerJobRunStateError(
            "scheduler job run idempotency key or scheduled slot already exists"
        ) from exc
    db.refresh(run)
    return run


def _update_scheduler_job_run_or_raise(
    db,
    run_id: int,
    *,
    current_status: str,
    claim_owner: str | None = None,
    claim_token: str | None = None,
    leader_name: str,
    owner_id: str,
    fencing_token: int,
    values: Dict[str, Any],
    error_type=SchedulerJobRunStateError,
) -> SchedulerJobRun:
    _raise_if_blank_scheduler_job_run_value(leader_name, "leader_name")
    _raise_if_blank_scheduler_job_run_value(owner_id, "leader_owner_id")
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token <= 0:
        raise SchedulerJobRunStateError("scheduler job run fencing token is invalid")
    live_fence = db.query(SchedulerLeaderLease.id).filter(
        SchedulerLeaderLease.leader_name == SCHEDULER_GLOBAL_LEADER_NAME,
        SchedulerLeaderLease.leader_name == leader_name,
        SchedulerLeaderLease.owner_id == owner_id,
        SchedulerLeaderLease.fencing_token == fencing_token,
        SchedulerLeaderLease.status == "active",
        SchedulerLeaderLease.lease_expires_at > _scheduler_leader_db_now_expression(db),
    ).exists()
    query = db.query(SchedulerJobRun).filter(
        SchedulerJobRun.id == run_id,
        SchedulerJobRun.status == current_status,
        live_fence,
    )
    if claim_owner is not None:
        query = query.filter(SchedulerJobRun.claim_owner == claim_owner)
    if claim_token is not None:
        query = query.filter(SchedulerJobRun.claim_token == claim_token)
    if query.update(values, synchronize_session=False) != 1:
        db.rollback()
        raise error_type("scheduler job run is not claimable in its current state")
    db.commit()
    db.expire_all()
    return db.get(SchedulerJobRun, run_id)


def claim_scheduler_job_run(
    db,
    run_id: int,
    *,
    claim_owner: str,
    claim_token: str,
    leader_name: str,
    owner_id: str,
    fencing_token: int,
) -> SchedulerJobRun:
    """Atomically claim an unclaimed slot only while its leader fence is live."""
    _raise_if_blank_scheduler_job_run_value(claim_owner, "claim_owner")
    _raise_if_blank_scheduler_job_run_value(claim_token, "claim_token")
    now = _scheduler_job_run_now()
    return _update_scheduler_job_run_or_raise(
        db,
        run_id,
        current_status="planned",
        leader_name=leader_name,
        owner_id=owner_id,
        fencing_token=fencing_token,
        values={
            "status": "claimed",
            "attempt_no": SchedulerJobRun.attempt_no + 1,
            "claim_owner": claim_owner,
            "claim_token": claim_token,
            "claimed_at": now,
        },
        error_type=SchedulerJobRunClaimError,
    )


def start_scheduler_job_run(
    db,
    run_id: int,
    *,
    claim_owner: str,
    claim_token: str,
    leader_name: str,
    owner_id: str,
    fencing_token: int,
) -> SchedulerJobRun:
    now = _scheduler_job_run_now()
    return _update_scheduler_job_run_or_raise(
        db,
        run_id,
        current_status="claimed",
        claim_owner=claim_owner,
        claim_token=claim_token,
        leader_name=leader_name,
        owner_id=owner_id,
        fencing_token=fencing_token,
        values={"status": "running", "started_at": now, "heartbeat_at": now},
        error_type=SchedulerJobRunClaimError,
    )


def heartbeat_scheduler_job_run(
    db,
    run_id: int,
    *,
    claim_owner: str,
    claim_token: str,
    leader_name: str,
    owner_id: str,
    fencing_token: int,
) -> SchedulerJobRun:
    return _update_scheduler_job_run_or_raise(
        db,
        run_id,
        current_status="running",
        claim_owner=claim_owner,
        claim_token=claim_token,
        leader_name=leader_name,
        owner_id=owner_id,
        fencing_token=fencing_token,
        values={"heartbeat_at": _scheduler_job_run_now()},
        error_type=SchedulerJobRunStateError,
    )


def complete_scheduler_job_run(
    db,
    run_id: int,
    *,
    claim_owner: str,
    claim_token: str,
    leader_name: str,
    owner_id: str,
    fencing_token: int,
    status: str,
    terminal_summary: str | None = None,
) -> SchedulerJobRun:
    if status not in _SCHEDULER_JOB_RUN_TERMINAL_STATUSES:
        raise SchedulerJobRunStateError("scheduler job run status is not terminal")
    return _update_scheduler_job_run_or_raise(
        db,
        run_id,
        current_status="running",
        claim_owner=claim_owner,
        claim_token=claim_token,
        leader_name=leader_name,
        owner_id=owner_id,
        fencing_token=fencing_token,
        values={
            "status": status,
            "terminal_at": _scheduler_job_run_now(),
            "terminal_summary": terminal_summary,
        },
        error_type=SchedulerJobRunStateError,
    )


def recover_stale_scheduler_job_runs(
    db,
    *,
    leader_name: str,
    owner_id: str,
    fencing_token: int,
    limit: int = 100,
) -> int:
    """Terminalize prior-generation runs before the new leader starts any work."""
    _raise_if_blank_scheduler_job_run_value(leader_name, "leader_name")
    _raise_if_blank_scheduler_job_run_value(owner_id, "leader_owner_id")
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token <= 0:
        raise SchedulerJobRunStateError("scheduler job run fencing token is invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 1000:
        raise SchedulerJobRunStateError("scheduler job run recovery limit must be between 1 and 1000")
    candidates = (
        db.query(SchedulerJobRun.id)
        .filter(SchedulerJobRun.status == "running")
        .order_by(SchedulerJobRun.heartbeat_at.asc(), SchedulerJobRun.id.asc())
        .limit(limit)
        .all()
    )
    run_ids = [int(row[0]) for row in candidates]
    if not run_ids:
        return 0
    live_fence = db.query(SchedulerLeaderLease.id).filter(
        SchedulerLeaderLease.leader_name == SCHEDULER_GLOBAL_LEADER_NAME,
        SchedulerLeaderLease.leader_name == leader_name,
        SchedulerLeaderLease.owner_id == owner_id,
        SchedulerLeaderLease.fencing_token == fencing_token,
        SchedulerLeaderLease.status == "active",
        SchedulerLeaderLease.lease_expires_at > _scheduler_leader_db_now_expression(db),
    ).exists()
    updated = (
        db.query(SchedulerJobRun)
        .filter(
            SchedulerJobRun.id.in_(run_ids),
            SchedulerJobRun.status == "running",
            live_fence,
        )
        .update(
            {
                "status": "execution_unknown",
                "terminal_at": _scheduler_leader_db_now_expression(db),
                "terminal_summary": "scheduler run heartbeat was lost before successor recovery",
            },
            synchronize_session=False,
        )
    )
    if updated != len(run_ids):
        db.rollback()
        raise SchedulerJobRunStateError(
            "scheduler job run recovery lost its fence or candidate state changed"
        )
    db.commit()
    db.expire_all()
    return updated


_SCHEDULER_LEADER_MAX_LEASE_SECONDS = 600


def _raise_if_invalid_scheduler_leader_value(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SchedulerLeaderLeaseError(f"scheduler leader {field} must be non-empty")


def _validate_scheduler_leader_lease_seconds(lease_seconds: int) -> None:
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or not 1 <= lease_seconds <= _SCHEDULER_LEADER_MAX_LEASE_SECONDS
    ):
        raise SchedulerLeaderLeaseError(
            "scheduler leader lease_seconds must be between 1 and 600"
        )


def _scheduler_leader_db_now_expression(db):
    if db.get_bind().dialect.name == "sqlite":
        return func.current_timestamp()
    if db.get_bind().dialect.name in {"mysql", "mariadb"}:
        return func.utc_timestamp()
    raise SchedulerLeaderLeaseError(
        f"scheduler leader lease is unsupported for {db.get_bind().dialect.name}"
    )


def _scheduler_leader_db_expiry_expression(db, lease_seconds: int):
    now = _scheduler_leader_db_now_expression(db)
    if db.get_bind().dialect.name == "sqlite":
        return func.datetime(now, f"+{lease_seconds} seconds")
    return func.date_add(now, text(f"INTERVAL {lease_seconds} SECOND"))


def _scheduler_leader_lease_or_raise(db, leader_name: str) -> SchedulerLeaderLease:
    lease = db.query(SchedulerLeaderLease).filter(
        SchedulerLeaderLease.leader_name == leader_name
    ).one_or_none()
    if lease is None:
        raise SchedulerLeaderFenceError("scheduler leader lease is not held")
    return lease


def acquire_scheduler_leader_lease(
    db,
    *,
    leader_name: str,
    owner_id: str,
    lease_seconds: int,
) -> SchedulerLeaderLease:
    """Atomically acquire a released or expired lease and advance its fence."""
    _raise_if_invalid_scheduler_leader_value(leader_name, "leader_name")
    _raise_if_invalid_scheduler_leader_value(owner_id, "owner_id")
    _validate_scheduler_leader_lease_seconds(lease_seconds)
    now = _scheduler_leader_db_now_expression(db)
    expires_at = _scheduler_leader_db_expiry_expression(db, lease_seconds)
    lease = SchedulerLeaderLease(
        leader_name=leader_name,
        owner_id=owner_id,
        fencing_token=1,
        status="active",
        acquired_at=now,
        heartbeat_at=now,
        lease_expires_at=expires_at,
    )
    try:
        db.add(lease)
        db.commit()
        db.refresh(lease)
        return lease
    except IntegrityError:
        db.rollback()

    now_expression = _scheduler_leader_db_now_expression(db)
    updated = db.query(SchedulerLeaderLease).filter(
        SchedulerLeaderLease.leader_name == leader_name,
        (
            (SchedulerLeaderLease.status == "released")
            | (
                (SchedulerLeaderLease.status == "active")
                & (SchedulerLeaderLease.lease_expires_at <= now_expression)
            )
        ),
    ).update(
        {
            "owner_id": owner_id,
            "fencing_token": SchedulerLeaderLease.fencing_token + 1,
            "status": "active",
            "acquired_at": now,
            "heartbeat_at": now,
            "lease_expires_at": _scheduler_leader_db_expiry_expression(db, lease_seconds),
            "released_at": None,
        },
        synchronize_session=False,
    )
    if updated != 1:
        db.rollback()
        raise SchedulerLeaderLeaseUnavailable("scheduler leader lease is held")
    db.commit()
    db.expire_all()
    return _scheduler_leader_lease_or_raise(db, leader_name)


def renew_scheduler_leader_lease(
    db,
    *,
    leader_name: str,
    owner_id: str,
    fencing_token: int,
    lease_seconds: int,
) -> SchedulerLeaderLease:
    """Renew only the current unexpired owner/token pair."""
    _raise_if_invalid_scheduler_leader_value(leader_name, "leader_name")
    _raise_if_invalid_scheduler_leader_value(owner_id, "owner_id")
    _validate_scheduler_leader_lease_seconds(lease_seconds)
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token <= 0:
        raise SchedulerLeaderFenceError("scheduler leader fencing token is invalid")
    now = _scheduler_leader_db_now_expression(db)
    updated = db.query(SchedulerLeaderLease).filter(
        SchedulerLeaderLease.leader_name == leader_name,
        SchedulerLeaderLease.owner_id == owner_id,
        SchedulerLeaderLease.fencing_token == fencing_token,
        SchedulerLeaderLease.status == "active",
        SchedulerLeaderLease.lease_expires_at > _scheduler_leader_db_now_expression(db),
    ).update(
        {
            "heartbeat_at": now,
            "lease_expires_at": _scheduler_leader_db_expiry_expression(db, lease_seconds),
        },
        synchronize_session=False,
    )
    if updated != 1:
        db.rollback()
        raise SchedulerLeaderFenceError("scheduler leader fence is no longer valid")
    db.commit()
    db.expire_all()
    return _scheduler_leader_lease_or_raise(db, leader_name)


def release_scheduler_leader_lease(
    db,
    *,
    leader_name: str,
    owner_id: str,
    fencing_token: int,
) -> SchedulerLeaderLease:
    """Release only the current unexpired owner/token pair."""
    _raise_if_invalid_scheduler_leader_value(leader_name, "leader_name")
    _raise_if_invalid_scheduler_leader_value(owner_id, "owner_id")
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token <= 0:
        raise SchedulerLeaderFenceError("scheduler leader fencing token is invalid")
    now = _scheduler_leader_db_now_expression(db)
    updated = db.query(SchedulerLeaderLease).filter(
        SchedulerLeaderLease.leader_name == leader_name,
        SchedulerLeaderLease.owner_id == owner_id,
        SchedulerLeaderLease.fencing_token == fencing_token,
        SchedulerLeaderLease.status == "active",
        SchedulerLeaderLease.lease_expires_at > _scheduler_leader_db_now_expression(db),
    ).update(
        {"status": "released", "released_at": now},
        synchronize_session=False,
    )
    if updated != 1:
        db.rollback()
        raise SchedulerLeaderFenceError("scheduler leader fence is no longer valid")
    db.commit()
    db.expire_all()
    return _scheduler_leader_lease_or_raise(db, leader_name)


def require_scheduler_leader_fence(
    db,
    *,
    leader_name: str,
    owner_id: str,
    fencing_token: int,
) -> SchedulerLeaderLease:
    """Fail closed unless a future run mutation is guarded by the live fence."""
    _raise_if_invalid_scheduler_leader_value(leader_name, "leader_name")
    _raise_if_invalid_scheduler_leader_value(owner_id, "owner_id")
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token <= 0:
        raise SchedulerLeaderFenceError("scheduler leader fencing token is invalid")
    lease = db.query(SchedulerLeaderLease).filter(
        SchedulerLeaderLease.leader_name == leader_name,
        SchedulerLeaderLease.owner_id == owner_id,
        SchedulerLeaderLease.fencing_token == fencing_token,
        SchedulerLeaderLease.status == "active",
        SchedulerLeaderLease.lease_expires_at > _scheduler_leader_db_now_expression(db),
    ).one_or_none()
    if lease is None:
        raise SchedulerLeaderFenceError("scheduler leader fence is no longer valid")
    return lease

def save_operation_log(log_data: dict):
    """保存可审计的 API 结果，并避免成功写操作复制大 payload。"""
    db = SessionLocal()
    try:
        action = log_data.get('action')
        status = log_data.get('status')
        payload_policy = log_data.get('payload_policy')
        request_payload_policy = log_data.get('request_payload_policy', payload_policy)
        response_payload_policy = log_data.get('response_payload_policy', payload_policy)
        new_log = OperationLog(
            brand=log_data.get('brand'),
            query_text=log_data.get('query'),
            module=log_data.get('module'),
            action=action,
            request_payload=_operation_log_payload_value(
                log_data.get('request'), action=action, status=status,
                payload_policy=request_payload_policy,
            ),
            response_payload=_operation_log_payload_value(
                log_data.get('response'), action=action, status=status,
                payload_policy=response_payload_policy,
            ),
            status=status,
            duration=log_data.get('duration'),
            error_message=_sanitize_operation_log_payload(
                log_data.get('error'), max_chars=OPERATION_LOG_ERROR_MAX_CHARS,
            ),
        )
        db.add(new_log)
        db.commit()
    finally:
        db.close()

def create_task_session(task_name: str) -> int:
    """开启一个新的任务运行会话"""
    db = SessionLocal()
    try:
        session = TaskRunSession(task_name=task_name, status='running', current_phase='Started')
        db.add(session)
        db.commit()
        db.refresh(session)
        return session.id # type: ignore
    finally:
        db.close()

def update_task_session(session_id: int, phase: str = None, status: str = None, summary: dict = None, error: str = None):
    """更新任务运行会话状态"""
    db = SessionLocal()
    try:
        session = db.get(TaskRunSession, session_id)
        if session:
            if phase: session.current_phase = phase # type: ignore
            if status: session.status = status # type: ignore
            if summary: session.summary_data = json.dumps(summary) # type: ignore
            if error: session.error_log = error # type: ignore
            if status in ['success', 'failed', 'partial_failed']:
                session.end_time = datetime.datetime.now() # type: ignore
            db.commit()
    finally:
        db.close()

def check_database_connection() -> bool:
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        driver_error = getattr(exc, "orig", None)
        driver_args = getattr(driver_error, "args", ())
        driver_error_code = (
            driver_args[0]
            if driver_args and isinstance(driver_args[0], int)
            else None
        )
        logger.warning(
            "database health check failed error_type=%s driver_error_code=%s",
            type(exc).__name__,
            driver_error_code,
        )
        return False
    finally:
        db.close()

def get_latest_task_session(task_name: str) -> TaskRunSession | None:
    db = SessionLocal()
    try:
        return db.query(TaskRunSession).filter(
            TaskRunSession.task_name == task_name
        ).order_by(TaskRunSession.start_time.desc()).first()
    finally:
        db.close()

def has_recent_task_activity(task_name: str, start_time: datetime.datetime) -> bool:
    db = SessionLocal()
    try:
        return _has_recent_task_activity(db, task_name, start_time)
    finally:
        db.close()

def _has_recent_task_activity(db, task_name: str, start_time: datetime.datetime) -> bool:
    if task_name != "agent_loop_governance_job":
        return False
    activity_checks = (
        db.query(TopQueryTask.id).filter(TopQueryTask.updated_at >= start_time).first(),
        db.query(TopQueryStepStatus.id).filter(TopQueryStepStatus.updated_at >= start_time).first(),
        db.query(TopQueryAgentEvent.id).filter(TopQueryAgentEvent.created_at >= start_time).first(),
        db.query(TopQueryActionPlan.id).filter(TopQueryActionPlan.updated_at >= start_time).first(),
    )
    return any(row is not None for row in activity_checks)


def _get_current_agent_loop_workflow(
    db,
    stale_before: datetime.datetime,
    now: Optional[datetime.datetime] = None,
):
    return db.query(TopQueryWorkflowRun).filter(
        TopQueryWorkflowRun.active_slot == "active",
        TopQueryWorkflowRun.status == "running",
        TopQueryWorkflowRun.run_key.like("task:%"),
        TopQueryWorkflowRun.heartbeat_at >= stale_before,
        TopQueryWorkflowRun.lease_expires_at > (now or datetime.datetime.now()),
    ).order_by(TopQueryWorkflowRun.heartbeat_at.desc()).first()


def get_agent_loop_run_freshness(stale_minutes: int) -> Dict[str, Any]:
    """Return only current, leased Agent Loop evidence for health decisions."""
    now = datetime.datetime.now()
    stale_before = now - datetime.timedelta(minutes=stale_minutes)
    db = SessionLocal()
    try:
        running_sessions = db.query(TaskRunSession).filter(
            TaskRunSession.task_name == "agent_loop_governance_job",
            TaskRunSession.status == "running",
        ).order_by(TaskRunSession.start_time.desc()).all()
        has_current_session = any(
            session.start_time is not None and session.start_time >= stale_before
            for session in running_sessions
        )
        current_workflow = _get_current_agent_loop_workflow(db, stale_before, now)
        stale_sessions = [
            session for session in running_sessions
            if session.start_time is None or session.start_time < stale_before
        ]
        workflow_backed_stale_sessions = 1 if current_workflow is not None and not has_current_session else 0
        stale_session_count = max(0, len(stale_sessions) - workflow_backed_stale_sessions)
        allowed_running_session_count = int(has_current_session or current_workflow is not None)
        isolated_session_count = max(0, len(running_sessions) - allowed_running_session_count)
        stale_workflow_query = db.query(TopQueryWorkflowRun).filter(
            TopQueryWorkflowRun.active_slot == "active",
            TopQueryWorkflowRun.status == "running",
            TopQueryWorkflowRun.run_key.like("task:%"),
            (
                TopQueryWorkflowRun.heartbeat_at.is_(None)
                | (TopQueryWorkflowRun.heartbeat_at < stale_before)
                | TopQueryWorkflowRun.lease_expires_at.is_(None)
                | (TopQueryWorkflowRun.lease_expires_at <= now)
            ),
        )
        total_stale_workflow_count = stale_workflow_query.count()
        latest_session = db.query(TaskRunSession.start_time).filter(
            TaskRunSession.task_name == "agent_loop_governance_job",
        ).order_by(TaskRunSession.start_time.desc()).first()
        latest_session_start = latest_session[0] if latest_session else None
        if latest_session_start is not None:
            stale_workflow_count = stale_workflow_query.filter(
                (TopQueryWorkflowRun.heartbeat_at >= latest_session_start)
                | (TopQueryWorkflowRun.started_at >= latest_session_start)
                | (TopQueryWorkflowRun.created_at >= latest_session_start)
            ).count()
        else:
            stale_workflow_count = total_stale_workflow_count
        historical_stale_workflow_count = max(
            0,
            total_stale_workflow_count - stale_workflow_count,
        )
        return {
            "has_current_session": has_current_session,
            "has_current_workflow": current_workflow is not None,
            "stale_session_count": stale_session_count,
            "isolated_session_count": isolated_session_count,
            "stale_workflow_count": stale_workflow_count,
            "historical_stale_workflow_count": historical_stale_workflow_count,
        }
    finally:
        db.close()

def mark_stale_running_sessions_failed(task_name: str, stale_minutes: int) -> int:
    db = SessionLocal()
    try:
        stale_before = datetime.datetime.now() - datetime.timedelta(minutes=stale_minutes)
        sessions = db.query(TaskRunSession).filter(
            TaskRunSession.task_name == task_name,
            TaskRunSession.status == "running",
            TaskRunSession.start_time < stale_before
        ).all()
        if _has_recent_task_activity(db, task_name, stale_before) or (
            task_name == "agent_loop_governance_job"
            and _get_current_agent_loop_workflow(db, stale_before) is not None
        ):
            return 0
        for session in sessions:
            session.status = "failed" # type: ignore
            session.current_phase = "Finished" # type: ignore
            session.end_time = datetime.datetime.now() # type: ignore
            existing_error = session.error_log or ""
            suffix = "health check auto-marked stale running session as failed"
            session.error_log = f"{existing_error}\n{suffix}".strip() # type: ignore
        if sessions:
            db.commit()
        return len(sessions)
    finally:
        db.close()

def get_latest_abnormal_query_for_brand(brand: str) -> DailyAbnormalQuery | None:
    db = SessionLocal()
    try:
        return db.query(DailyAbnormalQuery).filter(
            DailyAbnormalQuery.brand == brand
        ).order_by(DailyAbnormalQuery.created_at.desc()).first()
    finally:
        db.close()

def get_recent_table_activity(window_minutes: int = 60) -> dict:
    db = SessionLocal()
    try:
        start_time = datetime.datetime.now() - datetime.timedelta(minutes=window_minutes)
        return {
            "task_run_session": db.query(TaskRunSession).filter(TaskRunSession.start_time >= start_time).count(),
            "operation_log": db.query(OperationLog).filter(OperationLog.created_at >= start_time).count(),
            "daily_abnormal_query": db.query(DailyAbnormalQuery).filter(DailyAbnormalQuery.updated_at >= start_time).count(),
            "automation_action_log": db.query(ActionLog).filter(ActionLog.updated_at >= start_time).count(),
            "inventory_recovery_task": count_inventory_recovery_tasks(
                db,
                updated_after=start_time,
            ),
            "no_result_query": db.query(NoResultQuery).filter(NoResultQuery.updated_at >= start_time).count(),
        }
    finally:
        db.close()

def _json_dumps_for_db(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


_TOP_QUERY_CONTEXT_SNAPSHOT_MAX_CHARS = 60000
_TOP_QUERY_CONTEXT_PRODUCT_LIST_LIMIT = 16
_TOP_QUERY_CONTEXT_SIZE_LIST_LIMIT = 6
_TOP_QUERY_CONTEXT_TEXT_LIMIT = 160


def _truncate_snapshot_text(value: Any, limit: int = _TOP_QUERY_CONTEXT_TEXT_LIMIT) -> Any:
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else f"{value[:limit]}..."


def _compact_size_inventory_for_snapshot(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compacted = {}
    for key in [
        "total_size_count",
        "available_size_count",
        "broken_size_count",
        "broken_rate",
        "source",
    ]:
        if key in value:
            compacted[key] = value.get(key)
    for key in ["available_sizes", "unavailable_sizes", "low_stock_sizes"]:
        items = value.get(key)
        if isinstance(items, list):
            compacted[key] = list(items)[:_TOP_QUERY_CONTEXT_SIZE_LIST_LIMIT]
    return compacted


def _compact_product_for_snapshot(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compacted = {}
    for key in [
        "rank",
        "product_id",
        "productId",
        "sku_id",
        "skuId",
        "spuId",
        "name",
        "product_name",
        "productName",
        "mainImage",
        "imageUrl",
        "price",
        "original_price",
        "originalPrice",
        "discount_price",
        "discountPrice",
        "discount",
        "broken_size_rate",
        "broken_rate",
        "is_relevant",
        "relevance_reason",
        "relevance_source",
        "relevance_check_status",
    ]:
        if key in value:
            compacted[key] = _truncate_snapshot_text(value.get(key))
    if "size_inventory" in value:
        compacted["size_inventory"] = _compact_size_inventory_for_snapshot(value.get("size_inventory"))
    return compacted


def _compact_relevance_diagnostics_for_snapshot(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compacted = {}
    for key in ["checked_count", "mismatch_count", "check_failed_count", "mismatch_ratio"]:
        if key in value:
            compacted[key] = value.get(key)
    items = value.get("items")
    if isinstance(items, list):
        compacted["items"] = [
            _compact_product_for_snapshot(item)
            for item in items[:_TOP_QUERY_CONTEXT_PRODUCT_LIST_LIMIT]
            if isinstance(item, dict)
        ]
    return compacted


def _compact_context_snapshot_branch_for_storage(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compacted = dict(value)
    products = compacted.get("products")
    if isinstance(products, list):
        compacted["products"] = [
            _compact_product_for_snapshot(item)
            for item in products[:_TOP_QUERY_CONTEXT_PRODUCT_LIST_LIMIT]
            if isinstance(item, dict)
        ]
    if isinstance(compacted.get("relevance_diagnostics"), dict):
        compacted["relevance_diagnostics"] = _compact_relevance_diagnostics_for_snapshot(compacted.get("relevance_diagnostics"))
    for key in ["query_text", "query", "reason", "summary", "diagnosisConclusion"]:
        if key in compacted:
            compacted[key] = _truncate_snapshot_text(compacted.get(key))
    return compacted


def _compact_top_query_context_snapshot_payload(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if any(isinstance(value.get(branch), dict) for branch in ["before", "after"]):
        compacted = dict(value)
        for branch in ["before", "after"]:
            if isinstance(compacted.get(branch), dict):
                compacted[branch] = _compact_context_snapshot_branch_for_storage(compacted.get(branch))
        return compacted
    return _compact_context_snapshot_branch_for_storage(value)


def _json_dumps_top_query_context_snapshot_payload(value: Any) -> str:
    payload = _json_dumps_for_db(value)
    if len(payload) <= _TOP_QUERY_CONTEXT_SNAPSHOT_MAX_CHARS:
        return payload

    compacted_value = _compact_top_query_context_snapshot_payload(value)
    compacted_payload = _json_dumps_for_db(compacted_value)
    if len(compacted_payload) <= _TOP_QUERY_CONTEXT_SNAPSHOT_MAX_CHARS:
        return compacted_payload

    aggressively_compacted = _compact_top_query_context_snapshot_payload(compacted_value)
    if isinstance(aggressively_compacted, dict):
        if isinstance(aggressively_compacted.get("relevance_diagnostics"), dict):
            diagnostics = dict(aggressively_compacted["relevance_diagnostics"])
            diagnostics.pop("items", None)
            aggressively_compacted["relevance_diagnostics"] = diagnostics
        products = aggressively_compacted.get("products")
        if isinstance(products, list):
            trimmed_products = []
            for item in products[:8]:
                if not isinstance(item, dict):
                    continue
                trimmed_item = dict(item)
                trimmed_item.pop("size_inventory", None)
                for key in ["name", "product_name", "productName", "relevance_reason"]:
                    if key in trimmed_item:
                        trimmed_item[key] = _truncate_snapshot_text(trimmed_item.get(key), limit=80)
                trimmed_products.append(trimmed_item)
            aggressively_compacted["products"] = trimmed_products
        for branch in ["before", "after"]:
            branch_value = aggressively_compacted.get(branch)
            if isinstance(branch_value, dict):
                branch_copy = dict(branch_value)
                if isinstance(branch_copy.get("relevance_diagnostics"), dict):
                    diagnostics = dict(branch_copy["relevance_diagnostics"])
                    diagnostics.pop("items", None)
                    branch_copy["relevance_diagnostics"] = diagnostics
                products = branch_copy.get("products")
                if isinstance(products, list):
                    branch_copy["products"] = products[:8]
                aggressively_compacted[branch] = branch_copy
    return _json_dumps_for_db(aggressively_compacted)

def _coerce_date(value) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        return datetime.date.fromisoformat(value)
    return datetime.date.today()

def _get_top_query_task(db, task_id: int) -> TopQueryTask:
    task = db.get(TopQueryTask, task_id)
    if not task:
        raise ValueError(f"top query task not found: {task_id}")
    return task

def create_brand_daily_metric(metric_data: dict) -> int:
    db = SessionLocal()
    try:
        brand = metric_data.get("brand")
        data_date = _coerce_date(metric_data.get("data_date"))
        existing_rows = db.query(BrandDailyMetric).filter(
            BrandDailyMetric.brand == brand,
            BrandDailyMetric.data_date == data_date,
        ).order_by(BrandDailyMetric.id.desc()).all()
        metric = existing_rows[0] if existing_rows else BrandDailyMetric(
            brand=brand,
            data_date=data_date,
        )
        metric.brand = brand
        metric.data_date = data_date
        metric.brand_uv = metric_data.get("brand_uv") or 0
        metric.brand_pv = metric_data.get("brand_pv") or 0
        metric.ctr = metric_data.get("ctr") or 0
        metric.cvr = metric_data.get("cvr") or 0
        metric.top_query_count = metric_data.get("top_query_count") or 0
        metric.top_query_pv = metric_data.get("top_query_pv") or 0
        metric.top_query_pv_share = metric_data.get("top_query_pv_share") or 0
        metric.raw_payload = _json_dumps_for_db(metric_data.get("raw_payload") or metric_data)
        if not existing_rows:
            db.add(metric)
        for duplicate in existing_rows[1:]:
            db.delete(duplicate)
        db.commit()
        db.refresh(metric)
        return metric.id # type: ignore
    finally:
        db.close()

def upsert_top_query_task(task_data: dict, task_id: Optional[int] = None) -> int:
    db = SessionLocal()
    try:
        brand = task_data.get("brand")
        query_text = task_data.get("query_text") or task_data.get("query")
        data_date = _coerce_date(task_data.get("data_date"))
        query_source = str(task_data.get("query_source") or "top_query")
        task = db.get(TopQueryTask, task_id) if task_id is not None else None
        if task_id is not None:
            if task is None:
                raise ValueError(f"top query task not found: {task_id}")
            if (
                task.brand != brand
                or task.query_text != query_text
                or task.data_date != data_date
                or str(task.query_source or "top_query") != query_source
            ):
                raise ValueError("explicit top query task identity mismatch")
        else:
            task = db.query(TopQueryTask).filter(
                TopQueryTask.brand == brand,
                TopQueryTask.query_text == query_text,
                TopQueryTask.data_date == data_date,
                func.coalesce(TopQueryTask.query_source, "top_query") == query_source,
            ).first()
        if not task:
            task = TopQueryTask(
                brand=brand,
                query_text=query_text,
                data_date=data_date,
                started_at=task_data.get("started_at"),
                query_source=query_source,
            )
            db.add(task)

        for field in [
            "query_rank", "search_uv", "search_pv", "ctr", "cvr", "avg_ctr",
            "avg_cvr", "query_pv_share", "status", "display_status",
            "result_count", "started_at", "completed_at", "error_message",
            "query_source", "hot_word_type", "hot_status",
        ]:
            if field in task_data:
                setattr(task, field, task_data.get(field))
        task.raw_payload = _json_dumps_for_db(task_data.get("raw_payload") or task_data) # type: ignore
        db.commit()
        db.refresh(task)
        return task.id # type: ignore
    finally:
        db.close()

def append_top_query_step_status(task_id: int, status_data: dict) -> int:
    db = SessionLocal()
    try:
        task = _get_top_query_task(db, task_id)
        step = TopQueryStepStatus(
            task_id=task_id,
            brand=task.brand,
            query_text=task.query_text,
            check_key=status_data.get("check_key"),
            status=status_data.get("status"),
            display_status=status_data.get("display_status"),
            summary=status_data.get("summary"),
            evidence=_json_dumps_for_db(status_data.get("evidence")),
            before_snapshot=_json_dumps_for_db(status_data.get("before_snapshot")),
            after_snapshot=_json_dumps_for_db(status_data.get("after_snapshot")),
        )
        db.add(step)
        db.commit()
        db.refresh(step)
        return step.id # type: ignore
    finally:
        db.close()

def append_top_query_context_snapshot(task_id: int, context_type: str, payload: dict) -> int:
    db = SessionLocal()
    try:
        task = _get_top_query_task(db, task_id)
        snapshot = TopQueryContextSnapshot(
            task_id=task_id,
            brand=task.brand,
            query_text=task.query_text,
            context_type=context_type,
            payload=_json_dumps_top_query_context_snapshot_payload(payload),
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        return snapshot.id # type: ignore
    finally:
        db.close()

def append_top_query_agent_event(task_id: int, event_data: dict) -> int:
    db = SessionLocal()
    try:
        task = _get_top_query_task(db, task_id)
        event = TopQueryAgentEvent(
            task_id=task_id,
            brand=task.brand,
            query_text=task.query_text,
            agent_name=event_data.get("agent_name"),
            stage=event_data.get("stage"),
            event_type=event_data.get("event_type"),
            summary=event_data.get("summary"),
            input_payload=_json_dumps_for_db(event_data.get("input_payload")),
            output_payload=_json_dumps_for_db(event_data.get("output_payload")),
            duration=event_data.get("duration"),
            token_usage=event_data.get("token_usage") or 0,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event.id # type: ignore
    finally:
        db.close()

def append_top_query_action_plan(task_id: int, action_data: dict) -> int:
    db = SessionLocal()
    try:
        task = _get_top_query_task(db, task_id)
        idempotency_key = action_data.get("idempotency_key")
        if idempotency_key:
            existing = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.idempotency_key == idempotency_key,
            ).first()
            if existing is not None:
                _assert_matching_top_query_action(existing, task_id, action_data)
                return int(existing.id)
        action = TopQueryActionPlan(
            task_id=task_id,
            workflow_run_id=action_data.get("workflow_run_id"),
            round_no=action_data.get("round_no") or 0,
            idempotency_key=idempotency_key,
            external_request_id=action_data.get("external_request_id"),
            brand=task.brand,
            query_text=task.query_text,
            check_key=action_data.get("check_key"),
            tool_key=action_data.get("tool_key"),
            action_type=action_data.get("action_type"),
            execution_mode=action_data.get("execution_mode"),
            status=action_data.get("status") or "pending",
            request_payload=_json_dumps_for_db(action_data.get("request_payload")),
            response_payload=_json_dumps_for_db(action_data.get("response_payload")),
            before_snapshot=_json_dumps_for_db(action_data.get("before_snapshot")),
            after_snapshot=_json_dumps_for_db(action_data.get("after_snapshot")),
            reason=action_data.get("reason"),
            error_message=action_data.get("error_message"),
            expert_key=action_data.get("expert_key"),
            risk_level=action_data.get("risk_level") or "low",
            superseded_by_action_id=action_data.get("superseded_by_action_id"),
            proposal_kind=action_data.get("proposal_kind"),
            priority_mode=action_data.get("priority_mode"),
            baseline_hash=action_data.get("baseline_hash"),
            alternative_of_action_id=action_data.get("alternative_of_action_id"),
        )
        db.add(action)
        db.commit()
        db.refresh(action)
        return action.id # type: ignore
    except IntegrityError:
        db.rollback()
        idempotency_key = action_data.get("idempotency_key")
        existing = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.idempotency_key == idempotency_key,
        ).first() if idempotency_key else None
        if existing is None:
            raise
        _assert_matching_top_query_action(existing, task_id, action_data)
        return int(existing.id)
    finally:
        db.close()


def _assert_matching_top_query_action(existing: Any, task_id: int, action_data: dict) -> None:
    try:
        existing_payload = json.loads(existing.request_payload or "{}")
    except (TypeError, json.JSONDecodeError):
        existing_payload = existing.request_payload
    requested_payload = action_data.get("request_payload") or {}
    identity = (
        int(existing.task_id or 0),
        int(existing.workflow_run_id or 0),
        int(existing.round_no or 0),
        str(existing.check_key or ""),
        str(existing.tool_key or ""),
        str(existing.action_type or ""),
        str(existing.execution_mode or ""),
        existing_payload,
    )
    requested_identity = (
        int(task_id),
        int(action_data.get("workflow_run_id") or 0),
        int(action_data.get("round_no") or 0),
        str(action_data.get("check_key") or ""),
        str(action_data.get("tool_key") or ""),
        str(action_data.get("action_type") or ""),
        str(action_data.get("execution_mode") or ""),
        requested_payload,
    )
    if identity != requested_identity:
        raise ValueError("top query action idempotency key collision")

def save_no_result_batch(brand: str, batch_date: datetime.datetime, queries: List[dict]) -> int:
    """保存无结果词治理批次和归一化 Query。"""
    db = SessionLocal()
    try:
        batch = NoResultGovernanceBatch(
            brand=brand,
            batch_date=batch_date,
            total_queries=len(queries),
            status='pending',
            started_at=datetime.datetime.now(),
        )
        db.add(batch)
        db.flush()
        for item in queries:
            db.add(NoResultQuery(
                batch_id=batch.id,
                brand=brand,
                query_text=item.get("query"),
                data_date=item.get("data_date") or batch_date,
                no_result_count=item.get("no_result_count") or 0,
                search_pv=item.get("search_pv") or 0,
                search_uv=item.get("search_uv"),
                before_result_count=item.get("before_result_count") or 0,
                after_result_count=item.get("after_result_count"),
                source=item.get("source"),
                raw_payload=json.dumps(item.get("raw_payload") or {}, ensure_ascii=False),
                status=item.get("status") or "pending",
                ai_decision=item.get("ai_decision"),
                ai_reason=item.get("ai_reason"),
                final_strategy=item.get("final_strategy"),
                candidate_tags=json.dumps(item.get("candidate_tags") or [], ensure_ascii=False),
            ))
        db.commit()
        return batch.id # type: ignore
    finally:
        db.close()

def record_no_result_action(action_data: dict) -> int:
    """记录无结果词图谱动作，每条动作都包含快照和回滚请求。"""
    db = SessionLocal()
    try:
        action = NoResultGovernanceAction(
            query_id=action_data.get("query_id"),
            batch_id=action_data.get("batch_id"),
            brand=action_data.get("brand"),
            query_text=action_data.get("query"),
            action_type=action_data.get("action_type"),
            target_interface=action_data.get("target_interface"),
            request_payload=json.dumps(action_data.get("request_payload") or {}, ensure_ascii=False),
            response_payload=json.dumps(action_data.get("response_payload") or {}, ensure_ascii=False),
            before_snapshot=json.dumps(action_data.get("before_snapshot") or {}, ensure_ascii=False),
            rollback_payload=json.dumps(action_data.get("rollback_payload") or {}, ensure_ascii=False),
            status=action_data.get("status") or "pending",
            rollback_status=action_data.get("rollback_status") or "pending",
            risk_level=action_data.get("risk_level") or "low",
            agent_reason=action_data.get("agent_reason"),
            error_message=action_data.get("error_message"),
        )
        db.add(action)
        db.commit()
        db.refresh(action)
        return action.id # type: ignore
    finally:
        db.close()

def get_recent_operation_metrics(window_minutes: int = 60, slow_threshold: float = 5.0, modules: List[str] | None = None) -> dict:
    db = SessionLocal()
    try:
        start_time = datetime.datetime.now() - datetime.timedelta(minutes=window_minutes)
        query = db.query(OperationLog.status, OperationLog.duration).filter(OperationLog.created_at >= start_time)
        if modules:
            query = query.filter(OperationLog.module.in_(modules))
        logs = query.all()
        durations = sorted([
            float(duration) for _status, duration in logs
            if duration is not None
        ])
        total = len(logs)
        failed = sum(1 for status, _duration in logs if status == "failed")
        slow = sum(1 for duration in durations if duration > slow_threshold)
        if durations:
            index = max(0, math.ceil(len(durations) * 0.95) - 1)
            p95 = round(durations[index], 4)
        else:
            p95 = 0.0

        return {
            "window_minutes": window_minutes,
            "modules": modules or [],
            "total_count": total,
            "failed_count": failed,
            "failure_rate_percent": round((failed / total) * 100, 2) if total else 0.0,
            "slow_count": slow,
            "p95_duration_seconds": p95,
        }
    finally:
        db.close()

def save_abnormal_queries(queries: List[dict]) -> List[int]:
    """
    全量同步从生产数据源数据库拉取的异常 Query。
    具备代码级去重功能：同一日期、品牌、Query不重复插入。
    """
    db = SessionLocal()
    ids = []
    processed_in_batch = set() # 批次内去重，防止传入列表自带重复
    try:
        sync_count = 0
        data_date = datetime.date.today()

        for q in queries:
            query_text = q.get('query')
            brand = q.get('brand')

            if not query_text or not brand: continue

            # 批次内去重检查
            batch_key = (brand, query_text)
            if batch_key in processed_in_batch:
                continue
            processed_in_batch.add(batch_key)

            # 1. 检查本地是否已同步过该记录 (基于 data_date)
            existing = db.query(DailyAbnormalQuery).filter(
                DailyAbnormalQuery.brand == brand,
                DailyAbnormalQuery.query_text == query_text,
                func.date(DailyAbnormalQuery.data_date) == data_date
            ).first()

            if existing:
                existing.ctr = q.get('ctr') # type: ignore
                existing.cvr = q.get('cvr', 0) # type: ignore
                existing.anomaly_type = q.get('type') # type: ignore
                existing.ctr_gap = q.get('ctr_gap') # type: ignore
                existing.cvr_gap = q.get('cvr_gap') # type: ignore
                db.flush()
                ids.append(existing.id) # type: ignore
            else:
                new_q = DailyAbnormalQuery(
                    brand=brand,
                    query_text=query_text,
                    anomaly_type=q.get('type'),
                    ctr=q.get('ctr'),
                    cvr=q.get('cvr', 0),
                    avg_ctr_3d=q.get('ctr_avg'),
                    avg_cvr_3d=q.get('cvr_avg', 0),
                    ctr_gap=q.get('ctr_gap'),
                    cvr_gap=q.get('cvr_gap'),
                    is_processed=0,
                    ops_effect='pending'
                )
                db.add(new_q)
                db.flush()
                ids.append(new_q.id) # type: ignore
                sync_count += 1

        db.commit()
        print(f"[DB] 同步完成：今日新增 {sync_count} 条，总计处理 {len(queries)} 条异常 Query。")
        return ids # type: ignore
    finally:
        db.close()

def get_queries_by_status(status: int) -> List[DailyAbnormalQuery]:
    """根据处理状态获取 Query 列表 (0:待处理, 1:已处理, 2:待批量提交)"""
    db = SessionLocal()
    try:
        return db.query(DailyAbnormalQuery).filter(DailyAbnormalQuery.is_processed == status).all()
    finally:
        db.close()

def save_action_log(action_data: dict):
    db = SessionLocal()
    try:
        # 增加防御性去重逻辑：同一天、同品牌、同Query、同商品、同操作不重复记录
        today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        existing = db.query(ActionLog).filter(
            ActionLog.brand == action_data.get('brand'),
            ActionLog.query_text == action_data.get('query'),
            ActionLog.product_id == action_data.get('spu'),
            ActionLog.action_type == action_data.get('type'),
            ActionLog.created_at >= today_start
        ).first()

        if existing:
            # 如果已存在，更新其理由（可能诊断结论有细微差别）并更新时间
            existing.reason = action_data.get('reason') # type: ignore
            # 早期记录可能没有保存商品名称；幂等重试时用本次动作携带的
            # 权威名称补齐，避免前端长期显示空名称。
            product_name = action_data.get('name')
            if product_name and not existing.product_name:
                existing.product_name = product_name # type: ignore
            image_url = action_data.get('image_url') or action_data.get('imageUrl')
            if image_url and not existing.image_url:
                existing.image_url = image_url # type: ignore
            existing.created_at = datetime.datetime.now() # type: ignore
            db.commit()
            return

        log = ActionLog(
            brand=action_data.get('brand'),
            abnormal_query_id=action_data.get('abnormal_query_id'),
            action_type=action_data.get('type'),
            product_id=action_data.get('spu'),
            product_name=action_data.get('name'),
            image_url=action_data.get('image_url') or action_data.get('imageUrl'),
            query_text=action_data.get('query'),
            reason=action_data.get('reason')
        )
        db.add(log)
        db.commit()
    finally:
        db.close()

def mark_query_processed(query_id: int, action: str, remark: str = "", result: str = "success", status: int = 1, reason_analysis: str = None, action_suggestions: str = None):
    """
    更新 Query 处理状态。
    status: 1 (已完成), 2 (已诊断待提交)
    """
    db = SessionLocal()
    try:
        q = db.get(DailyAbnormalQuery, query_id)
        if q:
            q.is_processed = status # type: ignore
            q.action_taken = action # type: ignore
            q.remark = remark # type: ignore
            q.ops_effect = result # type: ignore
            if reason_analysis:
                q.reason_analysis = reason_analysis # type: ignore
            if action_suggestions:
                q.action_suggestions = action_suggestions # type: ignore
            db.commit()
    finally:
        db.close()

def get_active_demoted_products() -> List[ActionLog]:
    db = SessionLocal()
    try:
        demoted = db.query(ActionLog).filter(ActionLog.action_type == 2).all()
        active_demoted = []
        for d in demoted:
            recovered = db.query(ActionLog).filter(
                ActionLog.action_type == 3,
                ActionLog.product_id == d.product_id,
                ActionLog.query_text == d.query_text
            ).first()
            if not recovered: active_demoted.append(d)
        return active_demoted
    finally:
        db.close()

def get_active_demoted_items(brand: str) -> Dict[str, List[str]]:
    """获取指定品牌当前处于降权状态的 SKU 字典 (按 Query 分组)"""
    db = SessionLocal()
    try:
        # 1. 找出该品牌所有执行过降权的记录
        demoted_logs = db.query(ActionLog).filter(
            ActionLog.brand == brand,
            ActionLog.action_type == 2
        ).all()

        active_items = {} # query_text -> set(skus)
        for log in demoted_logs:
            sku = log.product_id
            query = log.query_text
            if not sku or not query: continue

            # 2. 检查该 SKU+Query 是否有对应的最新恢复记录
            latest_recover = db.query(ActionLog).filter(
                ActionLog.brand == brand,
                ActionLog.action_type == 3,
                ActionLog.product_id == sku,
                ActionLog.query_text == query,
                ActionLog.created_at > log.created_at
            ).first()

            if not latest_recover:
                if query not in active_items:
                    active_items[query] = set()
                active_items[query].add(sku)

        return {q: list(skus) for q, skus in active_items.items()}
    finally:
        db.close()

def get_queries_to_track(days: int = 5) -> List[DailyAbnormalQuery]:
    """
    读取 daily_abnormal_query 表，筛选出需要追踪的词 (PRD 要求)
    筛选条件: is_processed=1 且 action_taken 为 blacklisted, demoted, blacklisted_and_demoted
    """
    db = SessionLocal()
    try:
        start_date = datetime.datetime.now() - datetime.timedelta(days=days)
        return db.query(DailyAbnormalQuery).filter(
            DailyAbnormalQuery.is_processed == 1,
            DailyAbnormalQuery.action_taken.in_(['blacklisted', 'demoted', 'blacklisted_and_demoted']),
            DailyAbnormalQuery.created_at >= start_date
        ).all()
    finally:
        db.close()

def update_tracking_stats(query_id: int, tracking_json: str):
    db = SessionLocal()
    try:
        q = db.get(DailyAbnormalQuery, query_id)
        if q:
            q.tracking_data = tracking_json # type: ignore
            db.commit()
    finally:
        db.close()

def save_inventory_task(task_data: dict):
    """保存断码率监测任务"""
    db = SessionLocal()
    try:
        # 分层降权按 Query 生效，同一商品在不同 Query 下需要分别追踪恢复。
        existing = db.query(InventoryRecoveryTask).filter(
            InventoryRecoveryTask.brand == task_data.get('brand'),
            InventoryRecoveryTask.product_id == task_data.get('spu'),
            InventoryRecoveryTask.original_query == task_data.get('query'),
            InventoryRecoveryTask.status == 'monitoring'
        ).first()

        if not existing:
            task = InventoryRecoveryTask(
                brand=task_data.get('brand'),
                product_id=task_data.get('spu'),
                product_name=task_data.get('name'),
                original_query=task_data.get('query'),
                last_check_stock=task_data.get('stock'),
                status='monitoring'
            )
            db.add(task)
            db.flush()
            sync_inventory_recovery_task(db, task)
            db.commit()
    finally:
        db.close()

def get_active_inventory_tasks() -> List[InventoryRecoveryTask]:
    """获取所有正在监控中（待恢复）的断码率任务"""
    db = SessionLocal()
    try:
        return read_inventory_recovery_tasks(db, active_only=True)
    finally:
        db.close()

def mark_inventory_task_recovered(task_id: int, current_stock: int):
    """标记断码率任务为已恢复"""
    db = SessionLocal()
    try:
        task = db.get(InventoryRecoveryTask, task_id)
        if task:
            task.status = 'recovered' # type: ignore
            task.last_check_stock = current_stock # type: ignore
            task.updated_at = datetime.datetime.now() # type: ignore
            db.flush()
            sync_inventory_recovery_task(db, task)
            db.commit()
    finally:
        db.close()

def update_inventory_task_stock(task_id: int, current_stock: int):
    """更新断码率检查数值并强制刷新更新时间"""
    db = SessionLocal()
    try:
        task = db.get(InventoryRecoveryTask, task_id)
        if task:
            task.last_check_stock = current_stock # type: ignore
            task.updated_at = datetime.datetime.now() # type: ignore
            db.flush()
            sync_inventory_recovery_task(db, task)
            db.commit()
    finally:
        db.close()

def update_inventory_task_status(task_id: int, status: str):
    """更新任务追踪状态 (monitoring, auto_stopped, manually_stopped, recovered)"""
    db = SessionLocal()
    try:
        task = db.get(InventoryRecoveryTask, task_id)
        if task:
            task.status = status # type: ignore
            task.updated_at = datetime.datetime.now() # type: ignore
            db.flush()
            sync_inventory_recovery_task(db, task)
            db.commit()
    finally:
        db.close()

def mark_inventory_task_recovered_by_details(brand: str, query: str, sku: str, current_stock: int = -1):
    """根据详情标记正在监控中的库存任务为已恢复"""
    db = SessionLocal()
    try:
        tasks = db.query(InventoryRecoveryTask).filter(
            InventoryRecoveryTask.brand == brand,
            InventoryRecoveryTask.original_query == query,
            InventoryRecoveryTask.product_id == sku,
            InventoryRecoveryTask.status == 'monitoring'
        ).all()
        for task in tasks:
            task.status = 'recovered' # type: ignore
            task.last_check_stock = current_stock # type: ignore
            db.flush()
            sync_inventory_recovery_task(db, task)
        db.commit()
    finally:
        db.close()

def get_daily_action_count() -> int:
    """获取当日已执行的操作总数 (用于 PRD 5.2 调控上限)"""
    db = SessionLocal()
    try:
        today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return db.query(ActionLog).filter(ActionLog.created_at >= today_start).count()
    finally:
        db.close()

def get_daily_stats() -> dict:
    """
    分品牌统计昨日/当日运维实绩 (PRD 4.1 要求)
    返回格式: { "brands": { "ATLAS": { ... }, ... }, "summary": { ... } }
    """
    db = SessionLocal()
    # 品牌映射 (数据库存储名 -> 报表展示友好名)
    brand_map = {
        "atlas": "ATLAS",
        "meadow": "Meadow",
        "harbor": "Harbor",
        "summit": "Summit",
        "riviera": "Riviera",
        "lumen": "Lumen",
    }

    report_data = {}
    summary = {
        "total_sum": 0,
        "processed_sum": 0,
        "black_sum": 0,
        "demote_sum": 0,
        "recover_sum": 0,
        "unhealthy_sum": 0
    }

    try:
        today = datetime.datetime.now().date()

        for brand_key, display_name in brand_map.items():
            # 1. 异常 Query 总数与已处理数
            total_err = db.query(DailyAbnormalQuery).filter(
                DailyAbnormalQuery.brand == brand_key,
                func.date(DailyAbnormalQuery.created_at) == today
            ).count()

            processed = db.query(DailyAbnormalQuery).filter(
                DailyAbnormalQuery.brand == brand_key,
                func.date(DailyAbnormalQuery.created_at) == today,
                DailyAbnormalQuery.is_processed == 1
            ).count()

            # 2. 治理动作统计 (拉黑/降权)
            blacklisted = db.query(ActionLog).filter(
                ActionLog.brand == brand_key,
                func.date(ActionLog.created_at) == today,
                ActionLog.action_type == 1
            ).count()

            demoted = db.query(ActionLog).filter(
                ActionLog.brand == brand_key,
                func.date(ActionLog.created_at) == today,
                ActionLog.action_type == 2
            ).count()

            # 3. 恢复分层统计
            recovered = sum(
                1
                for row in read_inventory_recovery_tasks(
                    db,
                    brand=brand_key,
                    status="recovered",
                )
                if row.updated_at and row.updated_at.date() == today
            )

            # 4. 二次复查后依然不健康的 Query 列表 (ops_effect 为 failed)
            unhealthy_list = db.query(DailyAbnormalQuery.query_text).filter(
                DailyAbnormalQuery.brand == brand_key,
                func.date(DailyAbnormalQuery.created_at) == today,
                DailyAbnormalQuery.ops_effect == 'failed'
            ).all()
            unhealthy_queries = [q[0] for q in unhealthy_list]

            brand_stats = {
                "total_err_queries": total_err,
                "auto_processed_queries": processed,
                "blacklisted_products": blacklisted,
                "demoted_products": demoted,
                "recovered_products": recovered,
                "unhealthy_queries_after_retry": unhealthy_queries,
                "unhealthy_queries_count": len(unhealthy_queries)
            }
            report_data[display_name] = brand_stats

            # 累加汇总
            summary["total_sum"] += total_err
            summary["processed_sum"] += processed
            summary["black_sum"] += blacklisted
            summary["demote_sum"] += demoted
            summary["recover_sum"] += recovered
            summary["unhealthy_sum"] += len(unhealthy_queries)

        # 计算处理率
        summary["rate"] = round((summary["processed_sum"] / summary["total_sum"] * 100), 1) if summary["total_sum"] > 0 else 0.0

        return {
            "brands": report_data,
            "summary": summary
        }
    finally:
        db.close()

# --- Dashboard API Helpers ---

def get_dashboard_action_logs(action_type: int, filters: dict, page: int = 1, limit: int = 20) -> dict:
    db = SessionLocal()
    try:
        query = db.query(ActionLog).filter(ActionLog.action_type == action_type)

        if filters.get('date'):
            target_date = datetime.datetime.strptime(filters['date'], '%Y-%m-%d').date()
            query = query.filter(func.date(ActionLog.created_at) == target_date)

        if filters.get('brand'):
            query = query.filter(ActionLog.brand == filters['brand'])
        elif filters.get('brands'):
            query = query.filter(ActionLog.brand.in_(filters['brands']))

        if filters.get('search'):
            s = f"%{filters['search']}%"
            query = query.filter((ActionLog.product_id.like(s)) |
                                 (ActionLog.product_name.like(s)) |
                                 (ActionLog.query_text.like(s)))

        total = query.count()
        items = query.order_by(ActionLog.created_at.desc()).offset((page - 1) * limit).limit(limit).all()
        return {"total": total, "items": items}
    finally:
        db.close()

def get_dashboard_inventory_tasks(filters: dict, page: int = 1, limit: int = 20) -> dict:
    db = SessionLocal()
    try:
        status = filters.get('status')
        if status == 'stopped':
            status = ('auto_stopped', 'manually_stopped')
        common = {
            "brand": filters.get("brand"),
            "status": status,
            "search": filters.get("search"),
        }
        if filters.get("brands"):
            common["status"] = status
            # The read adapter accepts one brand at a time; combine the scoped
            # results to preserve the existing permission-filtered contract.
            items_all = []
            total = 0
            for brand in filters["brands"]:
                total += count_inventory_recovery_tasks(db, brand=brand, status=status, search=common["search"])
                items_all.extend(read_inventory_recovery_tasks(db, brand=brand, status=status, search=common["search"]))
            items_all.sort(key=lambda item: (item.updated_at or datetime.datetime.min, item.id), reverse=True)
            items = items_all[(page - 1) * limit: page * limit]
        else:
            total = count_inventory_recovery_tasks(db, **common)
            items = read_inventory_recovery_tasks(db, **common, limit=limit, offset=(page - 1) * limit)
        return {"total": total, "items": items}
    finally:
        db.close()

def get_dashboard_manual_attention(filters: dict, page: int = 1, limit: int = 20) -> dict:
    db = SessionLocal()
    try:
        # 1. 获取失败的 Query
        failed_query = db.query(DailyAbnormalQuery).filter(DailyAbnormalQuery.ops_effect == 'failed')

        if filters.get('date'):
            target_date = datetime.datetime.strptime(filters['date'], '%Y-%m-%d').date()
            failed_query = failed_query.filter(func.date(DailyAbnormalQuery.created_at) == target_date)

        if filters.get('brand'):
            failed_query = failed_query.filter(DailyAbnormalQuery.brand == filters['brand'])
        elif filters.get('brands'):
            failed_query = failed_query.filter(DailyAbnormalQuery.brand.in_(filters['brands']))

        if filters.get('query_text'):
            failed_query = failed_query.filter(DailyAbnormalQuery.query_text.like(f"%{filters['query_text']}%"))

        failed_results = failed_query.all()

        output = []
        for q in failed_results:
            output.append({
                "id": q.id,
                "query": q.query_text,
                "brand": q.brand,
                "problem": "二次治理后依然存在问题"
            })

        # 2. 获取拉黑过多的 Query
        # 使用分组聚合找出拉黑超过 6 个的 abnormal_query_id
        excessive_blacklist = db.query(
            ActionLog.abnormal_query_id,
            func.count(ActionLog.id).label('total')
        ).filter(ActionLog.action_type == 1).group_by(ActionLog.abnormal_query_id).having(func.count(ActionLog.id) > 6).all()

        for item in excessive_blacklist:
            q_id = item[0]
            if not q_id: continue

            # 检查这个 ID 是否已经在 output 中 (避免重复)
            if any(o['id'] == q_id for o in output):
                # 如果已经在 output 中，可能需要合并问题描述？
                # PRD 没说要合并，但逻辑上可能有多个问题
                for o in output:
                    if o['id'] == q_id:
                        o['problem'] += " & 该Query下自动化拉黑商品数量过多"
                continue

            q_info = db.get(DailyAbnormalQuery, q_id)
            if q_info:
                # 同样应用过滤条件
                if filters.get('date'):
                    target_date = datetime.datetime.strptime(filters['date'], '%Y-%m-%d').date()
                    if q_info.created_at.date() != target_date: continue
                if filters.get('brand'):
                    if q_info.brand != filters['brand']: continue
                elif filters.get('brands'):
                    if q_info.brand not in filters['brands']: continue
                if filters.get('query_text') and filters['query_text'] not in q_info.query_text: continue

                output.append({
                    "id": q_info.id,
                    "query": q_info.query_text,
                    "brand": q_info.brand,
                    "problem": "该Query下自动化拉黑商品数量过多"
                })

        # 内存分页
        total = len(output)
        items = output[(page - 1) * limit : page * limit]
        return {"total": total, "items": items}
    finally:
        db.close()
