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

Base: Any = declarative_base() # type: ignore

PLATFORM_ADMIN_ROLE = "platform_admin"
BRAND_ADMIN_ROLE = "brand_admin"
BRAND_MEMBER_ROLE = "brand_member"

ACTION_MANAGE_PLATFORM_USERS = "manage_platform_users"
ACTION_MANAGE_BRAND_USERS = "manage_brand_users"
ACTION_MANAGE_PLATFORM_CONFIG = "manage_platform_config"
ACTION_MANAGE_BRAND_CONFIG = "manage_brand_config"
ACTION_MANAGE_ASSISTANT_ACCESS = "manage_assistant_access"
ACTION_EXECUTE_GOVERNANCE_ACTIONS = "execute_governance_actions"
ACTION_MANAGE_NO_RESULT_EXECUTION = "manage_no_result_execution"
ACTION_CANCEL_GOVERNANCE_ACTIONS = "cancel_governance_actions"

SCHEDULER_GLOBAL_LEADER_NAME = "scheduler-main"

DEFAULT_PLATFORM_ADMIN_ACTION_PERMISSIONS = [
    ACTION_MANAGE_PLATFORM_USERS,
    ACTION_MANAGE_BRAND_USERS,
    ACTION_MANAGE_PLATFORM_CONFIG,
    ACTION_MANAGE_BRAND_CONFIG,
    ACTION_MANAGE_ASSISTANT_ACCESS,
    ACTION_EXECUTE_GOVERNANCE_ACTIONS,
    ACTION_MANAGE_NO_RESULT_EXECUTION,
    ACTION_CANCEL_GOVERNANCE_ACTIONS,
]

DEFAULT_BRAND_ADMIN_ACTION_PERMISSIONS = [
    ACTION_MANAGE_BRAND_USERS,
    ACTION_MANAGE_BRAND_CONFIG,
    ACTION_MANAGE_ASSISTANT_ACCESS,
    ACTION_EXECUTE_GOVERNANCE_ACTIONS,
    ACTION_MANAGE_NO_RESULT_EXECUTION,
    ACTION_CANCEL_GOVERNANCE_ACTIONS,
]

DEFAULT_BRAND_MEMBER_ACTION_PERMISSIONS: List[str] = []


def _sha256_hex_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column_name}) = 64 AND length({remainder}) = 0"


def _non_whitespace_check(column_name: str) -> str:
    checked_identifier = "`release`" if column_name == "release" else column_name
    remainder = f"trim({checked_identifier})"
    for character_code in (9, 10, 13):
        remainder = f"replace({remainder}, char({character_code}), '')"
    return f"length({remainder}) > 0"


def _utc_naive_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

# 1. 自动化操作日志表模型 (记录商品维度的治理动作)
class ActionLog(Base): # type: ignore
    __tablename__ = 'automation_action_log'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    brand = Column(String(50))
    abnormal_query_id = Column(Integer, ForeignKey('daily_abnormal_query.id'))
    action_type = Column(Integer)  # 1: 拉黑商品, 2: 降低分层/降权, 3: 恢复分层
    product_id = Column(Text)
    product_name = Column(String(255))
    image_url = Column(Text)
    query_text = Column(String(255))
    reason = Column(Text)

    parent_query = relationship("DailyAbnormalQuery", back_populates="actions")

# 2. 每日数据异常Query表模型
class DailyAbnormalQuery(Base): # type: ignore
    __tablename__ = 'daily_abnormal_query'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    brand = Column(String(50))
    data_date = Column(DateTime, default=datetime.datetime.now) # 对应 PRD 的 first_detected_date
    query_text = Column(String(255))
    anomaly_type = Column(String(20)) # low_ctr, low_cvr, both
    is_processed = Column(Integer, default=0) # 0: 待处理, 1: 已处理
    action_taken = Column(String(255)) # no_action_needed, blacklisted, demoted, blacklisted_and_demoted, limit_exceeded, execution_failed
    ctr = Column(Float)
    cvr = Column(Float)
    avg_ctr_3d = Column(Float)
    avg_cvr_3d = Column(Float)
    tracking_data = Column(Text)
    remark = Column(Text)
    ops_effect = Column(String(20), default='pending') # 对应 PRD 的 automation_result (success, failed, pending)
    ctr_gap = Column(Float)
    cvr_gap = Column(Float)
    reason_analysis = Column(Text)
    action_suggestions = Column(Text)

    actions = relationship("ActionLog", back_populates="parent_query") # type: ignore

# 3. 恢复任务表 (沿用旧表名，当前用于监测因断码率过高降权的商品)
class InventoryRecoveryTask(Base): # type: ignore
    __tablename__ = 'inventory_recovery_task'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    brand = Column(String(50))
    product_id = Column(Text)
    product_name = Column(String(255))
    original_query = Column(String(255))
    last_check_stock = Column(Integer)  # 当前存储最近一次断码率百分比整数
    status = Column(String(20), default='monitoring') # monitoring / recovered
    last_action_request_id = Column(String(255))
    last_action_generation = Column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

# 4. 详细操作流水表 (Req 1: 记录 API 请求/返回等底层细节)
class OperationLog(Base): # type: ignore
    __tablename__ = 'operation_log'
    __table_args__ = (
        Index(
            "ix_operation_log_search_lookup",
            "brand",
            "query_text",
            "module",
            "action",
            "status",
            "created_at",
        ),
        Index(
            "ix_operation_log_retention_created_id",
            "created_at",
            "id",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    brand = Column(String(50))
    query_text = Column(String(255))
    module = Column(String(50)) # Extractor, Workflow, Service, etc.
    action = Column(String(100)) # e.g., 'API_Request'
    request_payload = Column(Text)
    response_payload = Column(Text)
    status = Column(String(20)) # success / failed
    duration = Column(Float) # 耗时(秒)
    error_message = Column(Text)


# Unified action facts. Business tables remain projections; these records are the
# durable authorization and execution boundary for new write integrations.
class ActionIntent(Base):  # type: ignore
    __tablename__ = "action_intent"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_action_intent_idempotency_key"),
        CheckConstraint(
            _non_whitespace_check("action_type"),
            name="ck_action_intent_action_type_nonempty",
        ),
        CheckConstraint(
            _non_whitespace_check("idempotency_key"),
            name="ck_action_intent_idempotency_key_nonempty",
        ),
        CheckConstraint(
            _sha256_hex_check("payload_hash"),
            name="ck_action_intent_payload_hash_sha256",
        ),
        CheckConstraint(
            _non_whitespace_check("payload_json"),
            name="ck_action_intent_payload_json_nonempty",
        ),
        CheckConstraint(
            "json_valid(payload_json) = 1",
            name="ck_action_intent_payload_json_valid",
        ),
        CheckConstraint(
            _non_whitespace_check("operator"),
            name="ck_action_intent_operator_nonempty",
        ),
        CheckConstraint(
            _non_whitespace_check("brand"),
            name="ck_action_intent_brand_nonempty",
        ),
        CheckConstraint(
            _non_whitespace_check("release"),
            name="ck_action_intent_release_nonempty",
        ),
        CheckConstraint(
            _non_whitespace_check("permission"),
            name="ck_action_intent_permission_nonempty",
        ),
        CheckConstraint(
            "status IN ('pending', 'success', 'failed', 'execution_unknown', 'inconclusive')",
            name="ck_action_intent_status",
        ),
        CheckConstraint("version >= 0", name="ck_action_intent_version_nonnegative"),
        Index("ix_action_intent_status_created", "status", "created_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now, nullable=False)
    action_type = Column(String(100), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    operator = Column(String(120), nullable=False)
    brand = Column(String(50), nullable=False)
    release = Column(String(120), nullable=False)
    permission = Column(String(120), nullable=False)
    query_text = Column(String(255))
    target = Column(String(255))
    status = Column(String(40), nullable=False, default="pending")
    version = Column(Integer, nullable=False, default=0)
    parent_intent_id = Column(Integer, ForeignKey("action_intent.id"))

    attempts = relationship("ActionAttempt", back_populates="intent", cascade="all, delete-orphan")
    fence_evidence = relationship(
        "ActionFenceEvidence",
        back_populates="intent",
        cascade="all, delete-orphan",
    )
    readbacks = relationship("ActionReadback", back_populates="intent", cascade="all, delete-orphan")
    terminal_fact = relationship(
        "ActionTerminalFact",
        back_populates="intent",
        uselist=False,
        cascade="all, delete-orphan",
    )
    parent_intent = relationship("ActionIntent", remote_side=[id], uselist=False)


class ActionAttempt(Base):  # type: ignore
    __tablename__ = "action_attempt"
    __table_args__ = (
        UniqueConstraint("intent_id", "attempt_no", name="uq_action_attempt_intent_no"),
        UniqueConstraint("id", "intent_id", name="uq_action_attempt_id_intent"),
        UniqueConstraint(
            "id",
            "intent_id",
            "external_request_id",
            "execution_generation",
            name="uq_action_attempt_identity_facts",
        ),
        CheckConstraint("attempt_no > 0", name="ck_action_attempt_attempt_no_positive"),
        CheckConstraint(
            "status IN ('persisted', 'executing', 'fencing', 'execution_unknown', "
            "'reconciling', 'success', 'failed', 'inconclusive')",
            name="ck_action_attempt_status",
        ),
        CheckConstraint("version >= 0", name="ck_action_attempt_version_nonnegative"),
        CheckConstraint(
            "execution_generation >= 0",
            name="ck_action_attempt_execution_generation_nonnegative",
        ),
        CheckConstraint(
            "external_request_id IS NOT NULL AND "
            + _non_whitespace_check("external_request_id"),
            name="ck_action_attempt_external_request_id_nonempty",
        ),
        CheckConstraint(
            "(status = 'persisted' AND execution_generation = 0 "
            "AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND finished_at IS NULL) OR "
            "(status IN ('executing', 'fencing', 'reconciling') "
            "AND execution_generation > 0 "
            "AND claim_token IS NOT NULL AND "
            + _non_whitespace_check("claim_token")
            + " "
            "AND lease_expires_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status = 'execution_unknown' AND execution_generation > 0 "
            "AND claim_token IS NULL AND lease_expires_at IS NULL "
            "AND finished_at IS NULL) OR "
            "(status IN ('success', 'failed', 'inconclusive') "
            "AND execution_generation > 0 AND claim_token IS NULL "
            "AND lease_expires_at IS NULL AND finished_at IS NOT NULL)",
            name="ck_action_attempt_state_facts",
        ),
        Index("ix_action_attempt_intent_created", "intent_id", "created_at", "id"),
        Index("ix_action_attempt_status_lease", "status", "lease_expires_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    intent_id = Column(Integer, ForeignKey("action_intent.id"), nullable=False)
    attempt_no = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    started_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    finished_at = Column(DateTime)
    status = Column(String(40), nullable=False, default="persisted")
    version = Column(Integer, nullable=False, default=0)
    execution_generation = Column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    claim_token = Column(String(64))
    lease_expires_at = Column(DateTime)
    external_request_id = Column(String(255), nullable=False)
    response_summary = Column(Text)

    intent = relationship("ActionIntent", back_populates="attempts")
    fence_evidence = relationship(
        "ActionFenceEvidence",
        back_populates="attempt",
        cascade="all, delete-orphan",
        overlaps="fence_evidence,intent",
    )
    readbacks = relationship(
        "ActionReadback",
        back_populates="attempt",
        cascade="all, delete-orphan",
        overlaps="intent,readbacks",
    )


class ActionFenceEvidence(Base):  # type: ignore
    __tablename__ = "action_fence_evidence"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "execution_generation",
            name="uq_action_fence_attempt_generation",
        ),
        ForeignKeyConstraint(
            (
                "attempt_id",
                "intent_id",
                "external_request_id",
                "execution_generation",
            ),
            (
                "action_attempt.id",
                "action_attempt.intent_id",
                "action_attempt.external_request_id",
                "action_attempt.execution_generation",
            ),
            name="fk_action_fence_attempt_identity",
        ),
        CheckConstraint(
            _non_whitespace_check("external_request_id"),
            name="ck_action_fence_external_request_id_nonempty",
        ),
        CheckConstraint(
            "execution_generation > 0",
            name="ck_action_fence_execution_generation_positive",
        ),
        CheckConstraint(
            _non_whitespace_check("fence_claim_token"),
            name="ck_action_fence_claim_token_nonempty",
        ),
        CheckConstraint("status = 'fenced'", name="ck_action_fence_status"),
        CheckConstraint(
            _non_whitespace_check("evidence_payload"),
            name="ck_action_fence_evidence_payload_nonempty",
        ),
        CheckConstraint(
            "json_valid(evidence_payload) = 1",
            name="ck_action_fence_evidence_payload_valid",
        ),
        CheckConstraint(
            _sha256_hex_check("fact_hash"),
            name="ck_action_fence_fact_hash_sha256",
        ),
        Index("ix_action_fence_intent_created", "intent_id", "created_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    intent_id = Column(Integer, ForeignKey("action_intent.id"), nullable=False)
    attempt_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    external_request_id = Column(String(255), nullable=False)
    execution_generation = Column(Integer, nullable=False)
    fence_claim_token = Column(String(64), nullable=False)
    status = Column(String(40), nullable=False)
    evidence_payload = Column(Text, nullable=False)
    fact_hash = Column(String(64), nullable=False)

    intent = relationship(
        "ActionIntent",
        back_populates="fence_evidence",
        overlaps="attempt,fence_evidence",
    )
    attempt = relationship(
        "ActionAttempt",
        back_populates="fence_evidence",
        overlaps="fence_evidence,intent",
    )


class ActionReadback(Base):  # type: ignore
    __tablename__ = "action_readback"
    __table_args__ = (
        ForeignKeyConstraint(
            ("attempt_id", "intent_id"),
            ("action_attempt.id", "action_attempt.intent_id"),
            name="fk_action_readback_attempt_intent",
        ),
        CheckConstraint(
            "status IN ('success', 'failed', 'unavailable')",
            name="ck_action_readback_status",
        ),
        CheckConstraint(
            "authoritative IN (0, 1)",
            name="ck_action_readback_authoritative_boolean",
        ),
        CheckConstraint(
            "(authoritative = 1 AND status IN ('success', 'failed')) OR "
            "(authoritative = 0 AND status IN ('failed', 'unavailable'))",
            name="ck_action_readback_authoritative_status",
        ),
        CheckConstraint(
            _non_whitespace_check("observed_payload"),
            name="ck_action_readback_observed_payload_nonempty",
        ),
        CheckConstraint(
            "json_valid(observed_payload) = 1",
            name="ck_action_readback_observed_payload_valid",
        ),
        CheckConstraint(
            _sha256_hex_check("fact_hash"),
            name="ck_action_readback_fact_hash_sha256",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    intent_id = Column(Integer, ForeignKey("action_intent.id"), nullable=False)
    attempt_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    status = Column(String(40), nullable=False)
    authoritative = Column(Integer, nullable=False, default=1)
    observed_payload = Column(Text, nullable=False)
    fact_hash = Column(String(64), nullable=False)

    intent = relationship(
        "ActionIntent",
        back_populates="readbacks",
        overlaps="attempt,readbacks",
    )
    attempt = relationship(
        "ActionAttempt",
        back_populates="readbacks",
        overlaps="intent,readbacks",
    )


class ActionTerminalFact(Base):  # type: ignore
    __tablename__ = "action_terminal_fact"
    __table_args__ = (
        UniqueConstraint("intent_id", name="uq_action_terminal_fact_intent"),
        CheckConstraint(
            "status IN ('success', 'failed', 'inconclusive')",
            name="ck_action_terminal_fact_status",
        ),
        CheckConstraint(
            _non_whitespace_check("terminal_code"),
            name="ck_action_terminal_fact_terminal_code_nonempty",
        ),
        CheckConstraint(
            "(status = 'success' AND terminal_code IN ("
            "'executor_succeeded', 'authoritative_readback_success')) OR "
            "(status = 'failed' AND terminal_code IN ("
            "'executor_failed', 'authoritative_readback_failed')) OR "
            "(status = 'inconclusive' AND terminal_code IN ("
            "'readback_unavailable', 'readback_failed'))",
            name="ck_action_terminal_fact_status_code",
        ),
        CheckConstraint(
            _non_whitespace_check("result_json"),
            name="ck_action_terminal_fact_result_json_nonempty",
        ),
        CheckConstraint(
            "json_valid(result_json) = 1",
            name="ck_action_terminal_fact_result_json_valid",
        ),
        CheckConstraint(
            _non_whitespace_check("replay_result_json"),
            name="ck_action_terminal_fact_replay_result_json_nonempty",
        ),
        CheckConstraint(
            "json_valid(replay_result_json) = 1",
            name="ck_action_terminal_fact_replay_result_json_valid",
        ),
        CheckConstraint(
            _sha256_hex_check("fact_hash"),
            name="ck_action_terminal_fact_fact_hash_sha256",
        ),
        Index("ix_action_terminal_fact_status_created", "status", "created_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    intent_id = Column(Integer, ForeignKey("action_intent.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=False)
    status = Column(String(40), nullable=False)
    terminal_code = Column(String(80), nullable=False)
    result_json = Column(Text, nullable=False)
    replay_result_json = Column(
        Text().with_variant(MEDIUMTEXT(), "mysql", "mariadb"),
        nullable=False,
    )
    fact_hash = Column(String(64), nullable=False)

    intent = relationship("ActionIntent", back_populates="terminal_fact")


OPERATION_LOG_PAYLOAD_MAX_CHARS = 4096
OPERATION_LOG_ERROR_MAX_CHARS = 2048
OPERATION_LOG_RETENTION_DAYS = 30
OPERATION_LOG_TEST_RETENTION_DAYS = 7
OPERATION_LOG_RETENTION_BATCH_SIZE = 1000
OPERATION_LOG_RETENTION_INDEX_NAME = "ix_operation_log_retention_created_id"
OPERATION_LOG_RETENTION_INDEX_COLUMNS = ("created_at", "id")
OPERATION_LOG_PAYLOAD_REQUIRED_ACTIONS = frozenset({"POST mixed", "POST list"})
_OPERATION_LOG_SENSITIVE_VALUE = re.compile(
    r'(?i)(["\']?(?:access[_-]?token|authorization|password|secret|api[_-]?key)["\']?\s*[:=]\s*["\']?)([^"\'\s,}]+)'
)


class TaskRunSession(Base): # type: ignore
    __tablename__ = 'task_run_session'

    id = Column(Integer, primary_key=True, autoincrement=True)
    start_time = Column(DateTime, default=datetime.datetime.now)
    end_time = Column(DateTime)
    task_name = Column(String(100)) # agent_loop_governance_job / service_health_check_job
    current_phase = Column(String(50)) # Started, Extract, AI_Diagnosis, Apply_Actions, Verify, Finished
    status = Column(String(20), default='running') # running, success, partial_failed, failed
    summary_data = Column(Text) # JSON summary (counts, etc.)
    error_log = Column(Text)


class SchedulerJobRun(Base):  # type: ignore
    """Durable scheduler execution fact; it does not elect a scheduler leader."""

    __tablename__ = "scheduler_job_run"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_scheduler_job_run_idempotency_key"),
        UniqueConstraint("job_id", "scheduled_for", name="uq_scheduler_job_run_slot"),
        CheckConstraint(_non_whitespace_check("job_id"), name="ck_scheduler_job_run_job_id_nonempty"),
        CheckConstraint(
            _non_whitespace_check("idempotency_key"),
            name="ck_scheduler_job_run_idempotency_key_nonempty",
        ),
        CheckConstraint(_non_whitespace_check("release"), name="ck_scheduler_job_run_release_nonempty"),
        CheckConstraint(
            _sha256_hex_check("config_fingerprint"),
            name="ck_scheduler_job_run_config_fingerprint_sha256",
        ),
        CheckConstraint("attempt_no >= 0", name="ck_scheduler_job_run_attempt_nonnegative"),
        CheckConstraint(
            "status IN ('planned', 'claimed', 'running', 'succeeded', 'failed', "
            "'execution_unknown')",
            name="ck_scheduler_job_run_status",
        ),
        CheckConstraint(
            "(status = 'planned' AND attempt_no = 0 AND claim_owner IS NULL "
            "AND claim_token IS NULL AND claimed_at IS NULL AND started_at IS NULL "
            "AND heartbeat_at IS NULL AND terminal_at IS NULL) OR "
            "(status = 'claimed' AND attempt_no > 0 AND claim_owner IS NOT NULL "
            "AND " + _non_whitespace_check("claim_owner") + " AND claim_token IS NOT NULL "
            "AND " + _non_whitespace_check("claim_token") + " AND claimed_at IS NOT NULL "
            "AND started_at IS NULL AND heartbeat_at IS NULL AND terminal_at IS NULL) OR "
            "(status = 'running' AND attempt_no > 0 AND claim_owner IS NOT NULL "
            "AND " + _non_whitespace_check("claim_owner") + " AND claim_token IS NOT NULL "
            "AND " + _non_whitespace_check("claim_token") + " AND claimed_at IS NOT NULL "
            "AND started_at IS NOT NULL AND heartbeat_at IS NOT NULL AND terminal_at IS NULL) OR "
            "(status IN ('succeeded', 'failed', 'execution_unknown') AND attempt_no > 0 "
            "AND claim_owner IS NOT NULL AND " + _non_whitespace_check("claim_owner") + " "
            "AND claim_token IS NOT NULL AND " + _non_whitespace_check("claim_token") + " "
            "AND claimed_at IS NOT NULL AND started_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND terminal_at IS NOT NULL)",
            name="ck_scheduler_job_run_state_facts",
        ),
        Index("ix_scheduler_job_run_job_status_heartbeat", "job_id", "status", "heartbeat_at", "id"),
        Index("ix_scheduler_job_run_status_created", "status", "created_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(120), nullable=False)
    scheduled_for = Column(DateTime, nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    status = Column(String(40), nullable=False, default="planned")
    attempt_no = Column(Integer, nullable=False, default=0, server_default=text("0"))
    claim_owner = Column(String(120))
    claim_token = Column(String(64))
    release = Column(String(120), nullable=False)
    config_fingerprint = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utc_naive_now)
    claimed_at = Column(DateTime)
    started_at = Column(DateTime)
    heartbeat_at = Column(DateTime)
    terminal_at = Column(DateTime)
    terminal_summary = Column(Text)


class SchedulerJobRunStateError(RuntimeError):
    pass


class SchedulerJobRunClaimError(SchedulerJobRunStateError):
    pass


class SchedulerLeaderLease(Base):  # type: ignore
    """Durable leader lease with a monotonically increasing fencing token."""

    __tablename__ = "scheduler_leader_lease"
    __table_args__ = (
        UniqueConstraint("leader_name", name="uq_scheduler_leader_lease_name"),
        CheckConstraint(_non_whitespace_check("leader_name"), name="ck_scheduler_leader_lease_name_nonempty"),
        CheckConstraint(_non_whitespace_check("owner_id"), name="ck_scheduler_leader_lease_owner_nonempty"),
        CheckConstraint("fencing_token > 0", name="ck_scheduler_leader_lease_token_positive"),
        CheckConstraint("status IN ('active', 'released')", name="ck_scheduler_leader_lease_status"),
        CheckConstraint(
            "(status = 'active' AND owner_id IS NOT NULL AND acquired_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND lease_expires_at IS NOT NULL AND released_at IS NULL) OR "
            "(status = 'released' AND owner_id IS NOT NULL AND acquired_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND lease_expires_at IS NOT NULL AND released_at IS NOT NULL)",
            name="ck_scheduler_leader_lease_state_facts",
        ),
        Index("ix_scheduler_leader_lease_status_expiry", "status", "lease_expires_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    leader_name = Column(String(120), nullable=False)
    owner_id = Column(String(120), nullable=False)
    fencing_token = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="active")
    created_at = Column(DateTime, nullable=False, default=_utc_naive_now)
    acquired_at = Column(DateTime, nullable=False)
    heartbeat_at = Column(DateTime, nullable=False)
    lease_expires_at = Column(DateTime, nullable=False)
    released_at = Column(DateTime)


class SchedulerLeaderLeaseError(RuntimeError):
    pass


class SchedulerLeaderLeaseUnavailable(SchedulerLeaderLeaseError):
    pass


class SchedulerLeaderFenceError(SchedulerLeaderLeaseError):
    pass


class NodeModelConfigVersion(Base):  # type: ignore
    """Immutable node-model configuration bundle with one current slot."""

    __tablename__ = "node_model_config_version"
    __table_args__ = (
        UniqueConstraint("version", name="uq_node_model_config_version_version"),
        UniqueConstraint("active_slot", name="uq_node_model_config_version_active_slot"),
        CheckConstraint("version > 0", name="ck_node_model_config_version_positive"),
        CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_node_model_config_version_status",
        ),
        CheckConstraint(
            "active_slot IS NULL OR active_slot = 'active'",
            name="ck_node_model_config_version_active_slot",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="draft")
    active_slot = Column(String(20), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utc_naive_now)
    published_at = Column(DateTime)
    change_note = Column(Text)
    entries = relationship(
        "NodeModelConfigEntry",
        back_populates="version_record",
        cascade="all, delete-orphan",
        order_by="NodeModelConfigEntry.node_key",
    )


class NodeModelConfigEntry(Base):  # type: ignore
    __tablename__ = "node_model_config_entry"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "node_key",
            name="uq_node_model_config_entry_version_node",
        ),
        CheckConstraint(
            _non_whitespace_check("node_key"),
            name="ck_node_model_config_entry_node_key_nonempty",
        ),
        CheckConstraint(
            "channel IN ('rd_llm', 'pi_agent', 'local_llm')",
            name="ck_node_model_config_entry_channel",
        ),
        CheckConstraint(
            _non_whitespace_check("primary_model"),
            name="ck_node_model_config_entry_primary_model_nonempty",
        ),
        CheckConstraint(
            "fallback_model IS NULL OR " + _non_whitespace_check("fallback_model"),
            name="ck_node_model_config_entry_fallback_model_nonempty",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    version_id = Column(
        Integer,
        ForeignKey("node_model_config_version.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_key = Column(String(160), nullable=False)
    channel = Column(String(20), nullable=False)
    primary_model = Column(String(120), nullable=False)
    fallback_model = Column(String(120))
    version_record = relationship("NodeModelConfigVersion", back_populates="entries")


# 6. 无结果词治理批次表
class NoResultGovernanceBatch(Base): # type: ignore
    __tablename__ = 'no_result_governance_batch'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    brand = Column(String(50))
    batch_date = Column(DateTime, default=datetime.datetime.now)
    status = Column(String(30), default='running')
    total_queries = Column(Integer, default=0)
    skipped_queries = Column(Integer, default=0)
    changed_queries = Column(Integer, default=0)
    retag_status = Column(String(30), default='pending')
    rollback_status = Column(String(30), default='pending')
    started_at = Column(DateTime, default=datetime.datetime.now)
    completed_at = Column(DateTime)
    summary_data = Column(Text)

    queries = relationship("NoResultQuery", back_populates="batch") # type: ignore
    actions = relationship("NoResultGovernanceAction", back_populates="batch") # type: ignore

# 7. 无结果词治理 Query 表
class NoResultQuery(Base): # type: ignore
    __tablename__ = 'no_result_query'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    batch_id = Column(Integer, ForeignKey('no_result_governance_batch.id'))
    brand = Column(String(50))
    query_text = Column(String(255))
    data_date = Column(DateTime, default=datetime.datetime.now)
    no_result_count = Column(Integer, default=0)
    search_pv = Column(Integer, default=0)
    search_uv = Column(Integer)
    before_result_count = Column(Integer, default=0)
    after_result_count = Column(Integer)
    source = Column(String(100))
    raw_payload = Column(Text)
    status = Column(String(30), default='pending')
    ai_decision = Column(String(30))
    ai_reason = Column(Text)
    final_strategy = Column(Text)
    candidate_tags = Column(Text)
    processed_at = Column(DateTime)
    reviewed_at = Column(DateTime)

    batch = relationship("NoResultGovernanceBatch", back_populates="queries") # type: ignore
    actions = relationship("NoResultGovernanceAction", back_populates="query") # type: ignore

# 8. 无结果词知识图谱动作流水表
class NoResultGovernanceAction(Base): # type: ignore
    __tablename__ = 'no_result_governance_action'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    query_id = Column(Integer, ForeignKey('no_result_query.id'))
    batch_id = Column(Integer, ForeignKey('no_result_governance_batch.id'))
    brand = Column(String(50))
    query_text = Column(String(255))
    action_type = Column(String(50))
    target_interface = Column(String(120))
    request_payload = Column(Text)
    response_payload = Column(Text)
    before_snapshot = Column(Text)
    rollback_payload = Column(Text)
    status = Column(String(30), default='pending')
    rollback_status = Column(String(30), default='pending')
    risk_level = Column(String(20), default='low')
    agent_reason = Column(Text)
    error_message = Column(Text)

    query = relationship("NoResultQuery", back_populates="actions") # type: ignore
    batch = relationship("NoResultGovernanceBatch", back_populates="actions") # type: ignore

# 9. 无结果词 Agent Harness 事件表
class NoResultAgentEvent(Base): # type: ignore
    __tablename__ = 'no_result_agent_event'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    query_id = Column(Integer, ForeignKey('no_result_query.id'))
    batch_id = Column(Integer, ForeignKey('no_result_governance_batch.id'))
    brand = Column(String(50))
    query_text = Column(String(255))
    stage = Column(String(60))
    event_type = Column(String(40))
    tool_name = Column(String(80))
    summary = Column(Text)
    payload = Column(Text)

# 10. 无结果词品牌级重新打标签任务表
class NoResultRetagTask(Base): # type: ignore
    __tablename__ = 'no_result_retag_task'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    brand = Column(String(50))
    batch_ids = Column(Text)
    status = Column(String(30), default='pending')
    earliest_run_at = Column(DateTime, default=datetime.datetime.now)
    triggered_at = Column(DateTime)
    response_payload = Column(Text)
    error_message = Column(Text)

# 11. 无结果词“不处理”记录表
class NoResultIgnoreRecord(Base): # type: ignore
    __tablename__ = 'no_result_ignore_record'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    brand = Column(String(50))
    query_text = Column(String(255))
    reason = Column(Text)
    source = Column(String(80))
    model_version = Column(String(100))
    expires_at = Column(DateTime)

# 12. Top Query 品牌日指标表
class BrandDailyMetric(Base): # type: ignore
    __tablename__ = 'brand_daily_metric'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    brand = Column(String(50))
    data_date = Column(Date)
    brand_uv = Column(Integer, default=0)
    brand_pv = Column(Integer, default=0)
    ctr = Column(Float, default=0)
    cvr = Column(Float, default=0)
    top_query_count = Column(Integer, default=0)
    top_query_pv = Column(Integer, default=0)
    top_query_pv_share = Column(Float, default=0)
    raw_payload = Column(Text)


# 13. Top Query 治理任务表
class TopQueryTask(Base): # type: ignore
    __tablename__ = 'top_query_task'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    brand = Column(String(50))
    data_date = Column(Date)
    query_text = Column(String(255))
    query_rank = Column(Integer)
    search_uv = Column(Integer, default=0)
    search_pv = Column(Integer, default=0)
    ctr = Column(Float, default=0)
    cvr = Column(Float, default=0)
    avg_ctr = Column(Float, default=0)
    avg_cvr = Column(Float, default=0)
    query_pv_share = Column(Float, default=0)
    status = Column(String(40), default='pending')
    display_status = Column(String(40), default='normal')
    result_count = Column(Integer)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    error_message = Column(Text)
    raw_payload = Column(Text)
    query_source = Column(
        String(30),
        default='top_query',
        server_default=text("'top_query'"),
    )
    hot_word_type = Column(String(20))
    hot_status = Column(String(20))
# 14. Top Query 环节状态表
class TopQueryStepStatus(Base): # type: ignore
    __tablename__ = 'top_query_step_status'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    task_id = Column(Integer)
    brand = Column(String(50))
    query_text = Column(String(255))
    check_key = Column(String(50))
    status = Column(String(40))
    display_status = Column(String(40))
    summary = Column(Text)
    evidence = Column(Text)
    before_snapshot = Column(Text)
    after_snapshot = Column(Text)


# 15. Top Query 上下文快照表
class TopQueryContextSnapshot(Base): # type: ignore
    __tablename__ = 'top_query_context_snapshot'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    task_id = Column(Integer)
    brand = Column(String(50))
    query_text = Column(String(255))
    context_type = Column(String(80))
    payload = Column(Text)


# 16. Top Query Agent 事件表
class TopQueryAgentEvent(Base): # type: ignore
    __tablename__ = 'top_query_agent_event'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    task_id = Column(Integer)
    brand = Column(String(50))
    query_text = Column(String(255))
    agent_name = Column(String(80))
    stage = Column(String(80))
    event_type = Column(String(80))
    summary = Column(Text)
    input_payload = Column(Text)
    output_payload = Column(Text)
    duration = Column(Float)
    token_usage = Column(Integer, default=0)


# 17. Top Query 动作计划表
class TopQueryActionPlan(Base): # type: ignore
    __tablename__ = 'top_query_action_plan'
    __table_args__ = (
        UniqueConstraint('idempotency_key', name='uq_top_query_action_plan_idempotency_key'),
        UniqueConstraint('external_request_id', name='uq_top_query_action_plan_external_request_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    task_id = Column(Integer)
    workflow_run_id = Column(Integer)
    round_no = Column(Integer, default=0)
    idempotency_key = Column(String(255))
    external_request_id = Column(String(255))
    brand = Column(String(50))
    query_text = Column(String(255))
    check_key = Column(String(50))
    tool_key = Column(String(80))
    action_type = Column(String(80))
    execution_mode = Column(String(40))
    status = Column(String(40), default='pending')
    request_payload = Column(Text)
    response_payload = Column(Text)
    before_snapshot = Column(Text)
    after_snapshot = Column(Text)
    reason = Column(Text)
    error_message = Column(Text)
    execution_started_at = Column(DateTime)
    execution_finished_at = Column(DateTime)
    execution_lease_expires_at = Column(DateTime)
    expert_key = Column(String(80))
    risk_level = Column(
        String(20),
        default='low',
        server_default=text("'low'"),
    )
    superseded_by_action_id = Column(Integer)
    proposal_kind = Column(String(30))
    priority_mode = Column(String(30))
    baseline_hash = Column(String(64), index=True)
    alternative_of_action_id = Column(Integer)


class TopQueryWorkflowRun(Base): # type: ignore
    __tablename__ = 'top_query_workflow_run'
    __table_args__ = (
        UniqueConstraint('run_key', name='uq_top_query_workflow_run_run_key'),
        UniqueConstraint('task_id', 'active_slot', name='uq_top_query_workflow_run_task_active'),
        Index('ix_top_query_workflow_run_task_status_heartbeat', 'task_id', 'status', 'heartbeat_at'),
        Index('ix_top_query_workflow_run_task_status_lease', 'task_id', 'status', 'lease_expires_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, nullable=False)
    run_key = Column(String(255), nullable=False)
    active_slot = Column(String(20), nullable=True)
    status = Column(String(40), nullable=False, default='running')
    current_node = Column(String(80))
    round_no = Column(Integer, nullable=False, default=0)
    checkpoint_payload = Column(
        Text().with_variant(MEDIUMTEXT(), "mysql", "mariadb")
    )
    checkpoint_version = Column(Integer, nullable=False, default=1)
    consecutive_planner_failures = Column(Integer, nullable=False, default=0)
    consecutive_tool_failures = Column(Integer, nullable=False, default=0)
    consecutive_no_improvement = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime, default=datetime.datetime.now)
    heartbeat_at = Column(DateTime)
    lease_owner = Column(String(255))
    lease_expires_at = Column(DateTime)
    finished_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)


class TopQueryBudgetAccount(Base): # type: ignore
    __tablename__ = 'top_query_budget_account'
    __table_args__ = (
        UniqueConstraint(
            'scope_type',
            'scope_key',
            'period_start',
            name='uq_top_query_budget_account_scope_period',
        ),
        Index(
            'ix_top_query_budget_account_scope_period_end',
            'scope_type',
            'scope_key',
            'period_end',
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope_type = Column(String(40), nullable=False)
    scope_key = Column(String(255), nullable=False)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    token_limit = Column(Integer, nullable=False, default=0)
    action_limit = Column(Integer, nullable=False, default=0)
    reserved_tokens = Column(Integer, nullable=False, default=0)
    consumed_tokens = Column(Integer, nullable=False, default=0)
    reserved_actions = Column(Integer, nullable=False, default=0)
    consumed_actions = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)


class TopQueryBudgetLedger(Base): # type: ignore
    __tablename__ = 'top_query_budget_ledger'
    __table_args__ = (
        UniqueConstraint('idempotency_key', name='uq_top_query_budget_ledger_idempotency_key'),
        Index('ix_top_query_budget_ledger_account_created', 'account_id', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('top_query_budget_account.id'), nullable=False)
    workflow_run_id = Column(Integer, ForeignKey('top_query_workflow_run.id'), nullable=False)
    task_id = Column(Integer, nullable=False)
    round_no = Column(Integer, nullable=False, default=0)
    entry_type = Column(String(20), nullable=False)
    token_delta = Column(Integer, nullable=False, default=0)
    action_delta = Column(Integer, nullable=False, default=0)
    idempotency_key = Column(String(255), nullable=False)
    reason = Column(Text)
    detail_payload = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.now)


# 18. Top Query 品牌实验主表
class TopQueryExperiment(Base): # type: ignore
    __tablename__ = 'top_query_experiment'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    brand = Column(String(50))
    experiment_name = Column(String(255))
    status = Column(String(30), default='draft')
    sample_data_date = Column(Date)
    planned_start_date = Column(Date)
    planned_end_date = Column(Date)
    baseline_start_date = Column(Date)
    baseline_end_date = Column(Date)
    baseline_locked_at = Column(DateTime)
    calculation_version = Column(String(64))
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    created_by = Column(String(120))
    note = Column(Text)


class TopQueryExperimentSample(Base): # type: ignore
    __tablename__ = 'top_query_experiment_sample'
    __table_args__ = (
        UniqueConstraint('experiment_id', 'query_text', name='uq_top_query_experiment_sample_exp_query'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    experiment_id = Column(Integer, ForeignKey('top_query_experiment.id'))
    brand = Column(String(50))
    query_text = Column(String(255))
    group_type = Column(String(20))
    sample_rank = Column(Integer)
    sample_search_uv = Column(Integer, default=0)
    sample_search_pv = Column(Integer, default=0)
    sample_ctr = Column(Float, default=0)
    sample_cvr = Column(Float, default=0)
    sample_avg_ctr = Column(Float, default=0)
    sample_avg_cvr = Column(Float, default=0)
    sample_query_pv_share = Column(Float, default=0)
    baseline_observed_days = Column(Integer)
    baseline_search_uv = Column(Integer)
    baseline_search_pv = Column(Integer)
    baseline_click_uv = Column(Float)
    baseline_purchase_uv = Column(Float)
    baseline_ctr = Column(Float)
    baseline_cvr = Column(Float)


class TopQueryExperimentDailyMetric(Base): # type: ignore
    __tablename__ = 'top_query_experiment_daily_metric'
    __table_args__ = (
        UniqueConstraint('experiment_id', 'query_text', 'data_date', name='uq_top_query_experiment_daily_metric_exp_query_date'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    experiment_id = Column(Integer, ForeignKey('top_query_experiment.id'))
    brand = Column(String(50))
    query_text = Column(String(255))
    group_type = Column(String(20))
    data_date = Column(Date)
    observed_status = Column(String(20), default='observed')
    contamination_status = Column(String(20), default='clean')
    contamination_reason = Column(Text)
    contaminated_from_date = Column(Date)
    query_rank = Column(Integer)
    search_uv = Column(Integer, default=0)
    search_pv = Column(Integer, default=0)
    ctr = Column(Float, default=0)
    cvr = Column(Float, default=0)
    governance_task_id = Column(Integer)
    governance_status = Column(String(40))
    action_count = Column(Integer, default=0)
    auto_action_count = Column(Integer, default=0)
    manual_action_count = Column(Integer, default=0)
    token_usage = Column(Integer, default=0)
    duration_seconds = Column(Float, default=0)
    raw_payload = Column(Text)


# 20. 用户表 (Req: 登录与权限管理)
class User(Base): # type: ignore
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, index=True)
    password_hash = Column(String(255))
    page_permissions = Column(Text)  # 存储为 JSON 字符串: ["/", "/blacklist", ...]
    brand_permissions = Column(Text) # 存储为 JSON 字符串: ["atlas", "meadow", ...]
    is_admin = Column(Integer, default=0) # 1: 管理员, 0: 子账号
    role = Column(
        String(30),
        default=BRAND_MEMBER_ROLE,
        server_default=text(f"'{BRAND_MEMBER_ROLE}'"),
    )
    action_permissions = Column(Text, default='[]')
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)


class AssistantBrandSpace(Base): # type: ignore
    __tablename__ = 'assistant_brand_space'

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand = Column(String(50), unique=True, index=True)
    display_name = Column(String(100))
    default_agent_binding_id = Column(Integer)
    enabled = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)


class AssistantAgentBinding(Base): # type: ignore
    __tablename__ = 'assistant_agent_binding'

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand_space_id = Column(Integer, ForeignKey('assistant_brand_space.id'))
    display_name = Column(String(120))
    model_display_name = Column(String(120))
    model_name = Column(String(120))
    available_models_json = Column(Text, default='[]')
    supports_model_switch = Column(Integer, default=0, server_default=text("0"))
    binding_type = Column(String(20), default='agent')
    enabled = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)

    brand_space = relationship("AssistantBrandSpace") # type: ignore


class AssistantUserBinding(Base): # type: ignore
    __tablename__ = 'assistant_user_binding'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    brand_space_id = Column(Integer, ForeignKey('assistant_brand_space.id'))
    agent_binding_id = Column(Integer, ForeignKey('assistant_agent_binding.id'))
    created_at = Column(DateTime, default=datetime.datetime.now)

    brand_space = relationship("AssistantBrandSpace") # type: ignore
    agent_binding = relationship("AssistantAgentBinding") # type: ignore
    user = relationship("User") # type: ignore


class AssistantSessionMeta(Base): # type: ignore
    __tablename__ = 'assistant_session_meta'

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(120), index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    brand = Column(String(50), index=True)
    agent_binding_id = Column(Integer, ForeignKey('assistant_agent_binding.id'))
    manual_title = Column(String(255))
    generated_title = Column(String(255))
    last_message_preview = Column(Text)
    message_log = Column(Text, default='[]')
    proposed_actions_snapshot = Column(Text, default='[]')
    pinned = Column(Integer, default=0)
    last_opened_at = Column(DateTime, default=datetime.datetime.now)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    parent_session_id = Column(String(120), index=True)

    user = relationship("User") # type: ignore
    agent_binding = relationship("AssistantAgentBinding") # type: ignore


class AssistantActionRequest(Base): # type: ignore
    __tablename__ = 'assistant_action_request'

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(120), index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    brand = Column(String(50), index=True)
    action_type = Column(String(80))
    query_text = Column(String(255))
    target = Column(String(255))
    reason = Column(Text)
    risk_level = Column(String(20))
    payload_json = Column(Text, default='{}')
    status = Column(String(30), default='prepared')
    result_summary = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)

    user = relationship("User") # type: ignore
