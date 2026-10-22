"""Small domain types shared by the deterministic workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT

from src.persistence.models import Base


def _utc_now_naive() -> datetime.datetime:
    """Return a UTC timestamp for timezone-naive database columns."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


NODE_ORDER = ("s0", "reorder", "relevance", "recall")
TERMINAL_NODE_STATUSES = frozenset({"executed", "skipped", "error"})


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    EXECUTED = "executed"
    SKIPPED = "skipped"
    ERROR = "error"


class ActionRecoveryStatus(str, Enum):
    COMPLETED = "completed"
    SAFE_TO_RETRY = "safe_to_retry"
    UNRESOLVED = "unresolved"


class GovernancePlanStatus(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    VERIFIED = "verified"
    FAILED = "failed"
    EXECUTION_UNKNOWN = "execution_unknown"
    ROLLBACK_PENDING = "rollback_pending"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    REJECTED = "rejected"


class GovernancePlanExecutorType(str, Enum):
    AI_AUTO = "ai_auto"
    HUMAN = "human"


class GovernancePlanFailureStage(str, Enum):
    GENERATION = "generation"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class QuerySpec:
    """The minimum identity needed to run one Query through the mainline."""

    query_id: str
    brand: str
    query: str
    batch_id: str | None = None
    # ``None`` keeps direct workflow callers backwards-compatible.  Selection
    # supplies an explicit value so the reorder node can be deterministically
    # skipped without invoking a runner.
    reorder_eligible: bool | None = None
    reorder_skip_reason: str | None = None
    recall_enabled: bool | None = None
    relevance_enabled: bool | None = None
    experiment_id: int | str | None = None
    group_type: str | None = None

    @property
    def is_control(self) -> bool:
        return str(self.group_type or "").strip().lower() == "control"


@dataclass
class NodeResult:
    status: NodeStatus
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None
    action: Any = None
    telemetry: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def executed(
        cls, result: Any = None, *, action: Any = None
    ) -> "NodeResult":
        return cls(NodeStatus.EXECUTED, result=result, action=action)

    @classmethod
    def skipped(cls, reason: str) -> "NodeResult":
        return cls(NodeStatus.SKIPPED, result={"reason": reason})

    @classmethod
    def error(
        cls, error_code: str, error_message: str
    ) -> "NodeResult":
        return cls(
            NodeStatus.ERROR,
            error_code=error_code,
            error_message=error_message,
        )

    @property
    def terminal(self) -> bool:
        return self.status.value in TERMINAL_NODE_STATUSES


@dataclass(frozen=True)
class ActionRecoveryResult:
    status: ActionRecoveryStatus
    result: NodeResult | None = None
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def completed(cls, result: NodeResult) -> "ActionRecoveryResult":
        if not result.terminal:
            raise ValueError("recovered action result must be terminal")
        return cls(ActionRecoveryStatus.COMPLETED, result=result)

    @classmethod
    def safe_to_retry(cls) -> "ActionRecoveryResult":
        return cls(ActionRecoveryStatus.SAFE_TO_RETRY)

    @classmethod
    def unresolved(
        cls, error_code: str, error_message: str
    ) -> "ActionRecoveryResult":
        if not str(error_code).strip() or not str(error_message).strip():
            raise ValueError("unresolved action requires code and message")
        return cls(
            ActionRecoveryStatus.UNRESOLVED,
            error_code=error_code,
            error_message=error_message,
        )


@dataclass
class QueryRunResult:
    spec: QuerySpec
    s0: Mapping[str, Any] | None = None
    observation: Mapping[str, Any] | None = None
    observation_error: str | None = None
    nodes: dict[str, NodeResult] = field(default_factory=dict)
    final_status: str = "completed"


# The rewrite owns these tables.  They deliberately use a separate namespace
# so the local rebuild can remove the legacy Top Query tables without
# affecting No Result, Assistant, or shared action infrastructure.
class QueryGovernanceSelectionBatch(Base):  # type: ignore
    """Frozen Query-selection input and configuration for one brand batch."""

    __tablename__ = "query_governance_selection_batch"
    __table_args__ = (
        UniqueConstraint("batch_id", "brand", name="uq_qg_selection_batch_brand"),
        Index("ix_qg_selection_batch_brand_date", "brand", "data_date", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String(120), nullable=False)
    brand = Column(String(50), nullable=False)
    data_date = Column(Date, nullable=False)
    config_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)


class QueryGovernanceCandidate(Base):  # type: ignore
    """Rule and ranking evidence for every candidate considered in a batch."""

    __tablename__ = "query_governance_candidate"
    __table_args__ = (
        UniqueConstraint(
            "selection_batch_id", "query_key", name="uq_qg_candidate_batch_query"
        ),
        Index(
            "ix_qg_candidate_batch_selected_rank",
            "selection_batch_id",
            "selected",
            "selection_rank",
            "id",
        ),
        Index(
            "ix_qg_candidate_batch_brand_query",
            "batch_id",
            "brand",
            "query_text",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    selection_batch_id = Column(
        Integer,
        ForeignKey("query_governance_selection_batch.id", ondelete="CASCADE"),
        nullable=False,
    )
    batch_id = Column(String(120), nullable=False)
    brand = Column(String(50), nullable=False)
    data_date = Column(Date, nullable=False)
    query_id = Column(String(255), nullable=False)
    query_key = Column(String(255), nullable=False)
    query_text = Column(String(255), nullable=False)
    experiment_id = Column(Integer)
    group_type = Column(String(20))
    matched_rules_json = Column(Text, nullable=False)
    metrics_json = Column(Text, nullable=False)
    source_type = Column(String(50), nullable=False)
    rule_count = Column(Integer, nullable=False, default=0)
    ctr_decline_pp = Column(Float, nullable=False, default=0)
    target_day_pv = Column(Float, nullable=False, default=0)
    selection_rank = Column(Integer, nullable=False)
    selected = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)


class QueryGovernanceBatchSummary(Base):  # type: ignore
    """Materialized, read-only summary used by the operations overview."""

    __tablename__ = "query_governance_batch_summary"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "brand", name="uq_qg_batch_summary_batch_brand"
        ),
        Index("ix_qg_batch_summary_brand_date", "brand", "data_date", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String(120), nullable=False)
    brand = Column(String(50), nullable=False)
    data_date = Column(Date, nullable=False)
    payload_json = Column(
        Text().with_variant(MEDIUMTEXT(), "mysql", "mariadb"), nullable=False
    )
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryMetricSnapshot(Base):  # type: ignore
    """Local daily metrics snapshot used by fast Query detail reads."""

    __tablename__ = "query_metric_snapshot"
    __table_args__ = (
        UniqueConstraint("brand", "query_text", "data_date", name="uq_query_metric_snapshot_identity"),
        Index("ix_query_metric_snapshot_brand_date_query", "brand", "data_date", "query_text"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    brand = Column(String(50), nullable=False)
    query_text = Column(String(255), nullable=False)
    data_date = Column(Date, nullable=False)
    pv = Column(Float, nullable=False, default=0)
    uv = Column(Float, nullable=False, default=0)
    click_uv = Column(Float, nullable=False, default=0)
    add_to_cart_uv = Column(Float, nullable=False, default=0)
    purchase_uv = Column(Float, nullable=False, default=0)
    ctr = Column(Float, nullable=False, default=0)
    cvr = Column(Float, nullable=False, default=0)
    query_rank = Column(Integer)
    source_updated_at = Column(DateTime)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, nullable=False)


class QueryGovernanceTask(Base):  # type: ignore
    __tablename__ = "query_governance_task"
    __table_args__ = (
        UniqueConstraint("batch_id", "brand", "query_text", name="uq_qg_task_batch_brand_query"),
        Index("ix_qg_task_brand_date_status", "brand", "data_date", "status", "id"),
        Index("ix_qg_task_batch_brand_date", "batch_id", "brand", "data_date", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String(120), nullable=False)
    brand = Column(String(50), nullable=False)
    data_date = Column(Date, nullable=False)
    query_text = Column(String(255), nullable=False)
    experiment_id = Column(Integer)
    group_type = Column(String(20))
    query_rank = Column(Integer)
    status = Column(String(30), nullable=False, default="pending")
    s0_snapshot = Column(
        # Super-generic Queries recall tens of thousands of product ids and
        # overflow the 64KB TEXT column.
        Text().with_variant(MEDIUMTEXT(), "mysql", "mariadb")
    )
    error_code = Column(String(80))
    error_message = Column(Text)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceStepStatus(Base):  # type: ignore
    __tablename__ = "query_governance_step_status"
    __table_args__ = (
        UniqueConstraint("task_id", "node_key", name="uq_qg_step_task_node"),
        Index("ix_qg_step_task_status", "task_id", "status", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("query_governance_task.id", ondelete="CASCADE"), nullable=False)
    node_key = Column(String(60), nullable=False)
    status = Column(String(30), nullable=False, default="pending")
    result_json = Column(
        Text().with_variant(MEDIUMTEXT(), "mysql", "mariadb")
    )
    error_code = Column(String(80))
    error_message = Column(Text)
    duration_seconds = Column(Float)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceContextSnapshot(Base):  # type: ignore
    __tablename__ = "query_governance_context_snapshot"
    __table_args__ = (
        UniqueConstraint("batch_id", "brand", "context_type", name="uq_qg_context_batch_brand_type"),
        Index("ix_qg_context_batch_type", "batch_id", "context_type", "id"),
        {
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String(120), nullable=False)
    brand = Column(String(50), nullable=False)
    context_type = Column(String(80), nullable=False)
    payload = Column(Text, nullable=False)
    captured_at = Column(DateTime, default=_utc_now_naive, nullable=False)


class QueryGovernanceAgentEvent(Base):  # type: ignore
    __tablename__ = "query_governance_agent_event"
    __table_args__ = (
        Index("ix_qg_agent_event_task_created", "task_id", "created_at", "id"),
        Index(
            "ix_qg_agent_event_node_model_created",
            "node_key",
            "model_name",
            "created_at",
            "id",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("query_governance_task.id", ondelete="CASCADE"), nullable=False)
    brand = Column(String(50), nullable=False)
    query_text = Column(String(255), nullable=False)
    agent_name = Column(String(100), nullable=False)
    node_key = Column(String(100), nullable=False)
    event_type = Column(String(80), nullable=False)
    input_json = Column(Text)
    output_json = Column(Text)
    error_code = Column(String(80))
    duration_seconds = Column(Float)
    model_name = Column(String(120))
    model_role = Column(String(20))
    fallback_used = Column(Boolean)
    token_usage = Column(Integer)
    model_error_code = Column(String(80))
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)


class QueryGovernanceActionPlan(Base):  # type: ignore
    __tablename__ = "query_governance_action_plan"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_qg_action_idempotency_key"),
        Index("ix_qg_action_task_status", "task_id", "status", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("query_governance_task.id", ondelete="CASCADE"), nullable=False)
    batch_id = Column(String(120), nullable=False)
    brand = Column(String(50), nullable=False)
    query_text = Column(String(255), nullable=False)
    node_key = Column(String(60), nullable=False)
    action_type = Column(String(100), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    status = Column(String(30), nullable=False, default="pending")
    request_json = Column(Text)
    response_json = Column(Text)
    error_code = Column(String(80))
    error_message = Column(Text)
    executor_type = Column(String(20))
    executor_user_id = Column(String(120))
    executed_at = Column(DateTime)
    failure_stage = Column(String(30))
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceWorkflowRun(Base):  # type: ignore
    __tablename__ = "query_governance_workflow_run"
    __table_args__ = (
        UniqueConstraint("batch_id", name="uq_qg_workflow_batch"),
        Index("ix_qg_workflow_status_heartbeat", "status", "heartbeat_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String(120), nullable=False)
    status = Column(String(30), nullable=False, default="running")
    current_node = Column(String(60))
    checkpoint_payload = Column(
        Text().with_variant(MEDIUMTEXT(), "mysql", "mariadb")
    )
    checkpoint_version = Column(Integer, nullable=False, default=1)
    model_config_version = Column(Integer)
    heartbeat_at = Column(DateTime)
    started_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    finished_at = Column(DateTime)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceAsyncTask(Base):  # type: ignore
    __tablename__ = "query_governance_async_task"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_qg_async_idempotency_key"),
        Index("ix_qg_async_status_created", "status", "created_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("query_governance_task.id", ondelete="CASCADE"), nullable=False)
    batch_id = Column(String(120), nullable=False)
    brand = Column(String(50), nullable=False)
    query_text = Column(String(255), nullable=False)
    trigger_source = Column(String(80), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    status = Column(String(30), nullable=False, default="pending")
    evidence_json = Column(Text)
    # Only serializable Trace identity/metadata is persisted.  Callback
    # objects and credentials are recreated by the async worker.
    langfuse_context_json = Column(Text)
    plan_json = Column(Text)
    error_code = Column(String(80))
    error_message = Column(Text)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceManualRelevanceCheck(Base):  # type: ignore
    """Durable state for caller-submitted, read-only relevance checks.

    This table is deliberately independent from the main Query-governance
    task/action tables.  A manual check may call product-detail and model
    services, but it must never become a governance record or an action.
    """

    __tablename__ = "query_governance_manual_relevance_check"
    __table_args__ = (
        UniqueConstraint(
            "brand", "idempotency_key", name="uq_qg_manual_relevance_brand_idempotency"
        ),
        Index("ix_qg_manual_relevance_brand_status_created", "brand", "status", "created_at"),
    )

    check_id = Column(String(120), primary_key=True)
    brand = Column(String(50), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    status = Column(String(40), nullable=False, default="queued")
    request_json = Column(Text().with_variant(LONGTEXT(), "mysql", "mariadb"), nullable=False)
    result_json = Column(Text().with_variant(LONGTEXT(), "mysql", "mariadb"))
    error_code = Column(String(80))
    error_message = Column(Text)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceExperiment(Base):  # type: ignore
    __tablename__ = "query_governance_experiment"
    __table_args__ = (Index("ix_qg_experiment_brand_status", "brand", "status", "id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand = Column(String(50), nullable=False)
    experiment_name = Column(String(255), nullable=False)
    status = Column(String(30), nullable=False, default="draft")
    sample_data_date = Column(Date)
    planned_start_date = Column(Date)
    planned_end_date = Column(Date)
    created_by = Column(String(120))
    note = Column(Text)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceExperimentSample(Base):  # type: ignore
    __tablename__ = "query_governance_experiment_sample"
    __table_args__ = (
        UniqueConstraint("experiment_id", "query_text", name="uq_qg_experiment_sample_query"),
        Index("ix_qg_experiment_sample_group", "experiment_id", "group_type", "sample_rank", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    experiment_id = Column(Integer, ForeignKey("query_governance_experiment.id", ondelete="CASCADE"), nullable=False)
    brand = Column(String(50), nullable=False)
    query_text = Column(String(255), nullable=False)
    group_type = Column(String(20), nullable=False)
    sample_rank = Column(Integer)
    sample_search_pv = Column(Integer, default=0, nullable=False)
    sample_ctr = Column(Float, default=0, nullable=False)
    sample_cvr = Column(Float, default=0, nullable=False)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)


class QueryGovernanceExperimentDailyMetric(Base):  # type: ignore
    __tablename__ = "query_governance_experiment_daily_metric"
    __table_args__ = (
        UniqueConstraint("experiment_id", "query_text", "data_date", name="uq_qg_experiment_metric_date"),
        Index("ix_qg_experiment_metric_group", "experiment_id", "data_date", "group_type", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    experiment_id = Column(Integer, ForeignKey("query_governance_experiment.id", ondelete="CASCADE"), nullable=False)
    brand = Column(String(50), nullable=False)
    query_text = Column(String(255), nullable=False)
    group_type = Column(String(20), nullable=False)
    data_date = Column(Date, nullable=False)
    search_pv = Column(Integer, default=0, nullable=False)
    ctr = Column(Float, default=0, nullable=False)
    cvr = Column(Float, default=0, nullable=False)
    governance_task_id = Column(Integer, ForeignKey("query_governance_task.id"))
    governance_status = Column(String(30))
    action_count = Column(Integer, default=0, nullable=False)
    token_usage = Column(Integer, default=0, nullable=False)
    duration_seconds = Column(Float, default=0, nullable=False)
    raw_payload = Column(Text)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceRelevanceRecord(Base):  # type: ignore
    __tablename__ = "query_governance_relevance_record"
    __table_args__ = (
        UniqueConstraint("brand", "query_key", "product_id", name="uq_qg_relevance_brand_query_product"),
        Index("ix_qg_relevance_query", "brand", "query_key", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand = Column(String(50), nullable=False)
    query_text = Column(String(255), nullable=False)
    query_key = Column(String(255), nullable=False)
    product_id = Column(String(255), nullable=False)
    relevance = Column(String(30), nullable=False)
    model_name = Column(String(120))
    detection_type = Column(String(30), nullable=False, default="multimodal")
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceImageAsset(Base):  # type: ignore
    """The current COS image mapping for one brand product."""

    __tablename__ = "query_governance_image_asset"
    __table_args__ = (
        UniqueConstraint("brand", "product_id", name="uq_qg_image_asset_brand_product"),
        Index("ix_qg_image_asset_brand_spu", "brand", "spu_id", "id"),
        Index("ix_qg_image_asset_source_hash", "source_url_hash"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand = Column(String(50), nullable=False)
    product_id = Column(String(255), nullable=False)
    spu_id = Column(String(255))
    source_url = Column(Text, nullable=False)
    source_url_hash = Column(String(64), nullable=False)
    storage_key = Column(String(500), nullable=False)
    cached_url = Column(Text, nullable=False)
    status = Column(String(30), nullable=False, default="ready")
    content_type = Column(String(100), nullable=False, default="image/jpeg")
    width = Column(Integer)
    height = Column(Integer)
    image_sha256 = Column(String(64))
    last_success_at = Column(DateTime)
    last_failure_code = Column(String(80))
    last_failure_message = Column(Text)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceRelevanceGuidance(Base):  # type: ignore
    __tablename__ = "query_governance_relevance_guidance"
    __table_args__ = (UniqueConstraint("brand", "query_text", name="uq_qg_relevance_guidance_brand_query"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand = Column(String(50), nullable=False)
    query_text = Column(String(255), nullable=False)
    rules_text = Column(Text, nullable=False, default="")
    status = Column(String(20), nullable=False, default="active")
    updated_by = Column(String(120))
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)


class QueryGovernanceRelevanceGuidanceReason(Base):  # type: ignore
    """Immutable audit record for every non-empty operator correction reason."""

    __tablename__ = "query_governance_relevance_guidance_reason"
    __table_args__ = (
        Index("ix_qg_relevance_guidance_reason_brand_query", "brand", "query_text", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand = Column(String(50), nullable=False)
    query_text = Column(String(255), nullable=False)
    product_id = Column(String(255), nullable=False)
    reason_text = Column(Text, nullable=False)
    operator = Column(String(120), nullable=False)
    created_at = Column(DateTime, default=_utc_now_naive, nullable=False)
