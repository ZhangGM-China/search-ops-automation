import hashlib
import json
import os
import re
import warnings
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, MetaData, UniqueConstraint, inspect, text
from sqlalchemy.exc import SAWarning
from sqlalchemy.schema import CreateColumn, CreateTable
from sqlalchemy.sql.sqltypes import Boolean, Float, Integer, Numeric

from src.persistence import (
    ActionLog,
    ActionAttempt,
    ActionFenceEvidence,
    ActionIntent,
    ActionReadback,
    ActionTerminalFact,
    AssistantAgentBinding,
    AssistantBrandSpace,
    AssistantUserBinding,
    AssistantSessionMeta,
    Base,
    OperationLog,
    NodeModelConfigEntry,
    NodeModelConfigVersion,
    SchedulerLeaderLease,
    SchedulerJobRun,
    TopQueryActionPlan,
    TopQueryBudgetAccount,
    TopQueryBudgetLedger,
    TopQueryExperiment,
    TopQueryExperimentSample,
    TopQueryTask,
    TopQueryWorkflowRun,
    User,
    InventoryRecoveryTask,
    BRAND_MEMBER_ROLE,
)
from src.query_governance.models import (
    QueryGovernanceActionPlan,
    QueryGovernanceBatchSummary,
    QueryGovernanceContextSnapshot,
    QueryGovernanceImageAsset,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
)


Logger = Callable[[str], None]


class ReleaseSchemaMigrationError(RuntimeError):
    """Expose a bounded schema-migration stage while preserving the cause."""

    def __init__(self, stage: str, cause: Exception):
        super().__init__(f"release schema migration failed at {stage}: {cause}")
        self.stage = stage

RELEASE_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, str]]] = {
    ActionLog.__tablename__: (
        ("image_url", "TEXT"),
    ),
    User.__tablename__: (
        ("role", f"VARCHAR(30) DEFAULT '{BRAND_MEMBER_ROLE}'"),
        ("action_permissions", "TEXT"),
    ),
    TopQueryTask.__tablename__: (
        ("query_source", "VARCHAR(30) DEFAULT 'top_query'"),
        ("hot_word_type", "VARCHAR(20)"),
        ("hot_status", "VARCHAR(20)"),
    ),
    TopQueryActionPlan.__tablename__: (
        ("expert_key", "VARCHAR(80)"),
        ("risk_level", "VARCHAR(20) DEFAULT 'low'"),
        ("idempotency_key", "VARCHAR(255)"),
        ("superseded_by_action_id", "INTEGER"),
        ("proposal_kind", "VARCHAR(30)"),
        ("priority_mode", "VARCHAR(30)"),
        ("baseline_hash", "VARCHAR(64)"),
        ("alternative_of_action_id", "INTEGER"),
    ),
    QueryGovernanceActionPlan.__tablename__: (
        ("executor_type", "VARCHAR(20)"),
        ("executor_user_id", "VARCHAR(120)"),
        ("executed_at", "DATETIME"),
        ("failure_stage", "VARCHAR(30)"),
    ),
    AssistantAgentBinding.__tablename__: (
        ("model_name", "VARCHAR(120)"),
        ("available_models_json", "TEXT"),
        ("supports_model_switch", "INTEGER DEFAULT 0"),
    ),
    "assistant_session_meta": (
        ("parent_session_id", "VARCHAR(120)"),
    ),
    TopQueryExperiment.__tablename__: (
        ("baseline_start_date", "DATE"),
        ("baseline_end_date", "DATE"),
        ("baseline_locked_at", "DATETIME"),
        ("calculation_version", "VARCHAR(64)"),
    ),
    TopQueryExperimentSample.__tablename__: (
        ("baseline_observed_days", "INTEGER"),
        ("baseline_search_uv", "INTEGER"),
        ("baseline_search_pv", "INTEGER"),
        ("baseline_click_uv", "FLOAT"),
        ("baseline_purchase_uv", "FLOAT"),
        ("baseline_ctr", "FLOAT"),
        ("baseline_cvr", "FLOAT"),
    ),
}

RELEASE_INDEX_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": AssistantAgentBinding.__tablename__,
        "name": "ix_assistant_agent_binding_brand_space_id",
        "columns": ("brand_space_id",),
    },
    {
        "table": AssistantUserBinding.__tablename__,
        "name": "ix_assistant_user_binding_user_id",
        "columns": ("user_id",),
    },
    {
        "table": AssistantUserBinding.__tablename__,
        "name": "ix_assistant_user_binding_agent_binding_id",
        "columns": ("agent_binding_id",),
    },
    {
        "table": "assistant_session_meta",
        "name": "ix_assistant_session_meta_user_id",
        "columns": ("user_id",),
    },
    {
        "table": "assistant_session_meta",
        "name": "ix_assistant_session_meta_agent_binding_id",
        "columns": ("agent_binding_id",),
    },
    {
        "table": "assistant_action_request",
        "name": "ix_assistant_action_request_user_id",
        "columns": ("user_id",),
    },
    {
        "table": "top_query_experiment",
        "name": "ix_top_query_experiment_brand_status_start_id",
        "columns": ("brand", "status", "planned_start_date", "id"),
    },
    {
        "table": TopQueryTask.__tablename__,
        "name": "ix_top_query_task_source_brand_date_rank_id",
        "columns": ("query_source", "brand", "data_date", "query_rank", "id"),
    },
    {
        "table": "top_query_experiment_sample",
        "name": "ix_top_query_experiment_sample_experiment_group_rank_id",
        "columns": ("experiment_id", "group_type", "sample_rank", "id"),
    },
    {
        "table": "top_query_experiment_daily_metric",
        "name": "ix_top_query_experiment_daily_metric_experiment_date_group_id",
        "columns": ("experiment_id", "data_date", "group_type", "id"),
    },
    {
        "table": OperationLog.__tablename__,
        "name": "ix_operation_log_search_lookup",
        "columns": ("brand", "query_text", "module", "action", "status", "created_at"),
    },
    {
        "table": OperationLog.__tablename__,
        "name": "ix_operation_log_retention_created_id",
        "columns": ("created_at", "id"),
        "mysql_online": True,
    },
)

AIOPS127_ACTION_FACT_MODELS = (
    ActionIntent,
    ActionAttempt,
    ActionFenceEvidence,
    ActionReadback,
    ActionTerminalFact,
)

AIOPS130B_RUN_MODELS = (SchedulerJobRun, SchedulerLeaderLease)

AIOPS135_NODE_CONFIG_MODELS = (
    NodeModelConfigVersion,
    NodeModelConfigEntry,
)

AIOPS135_NODE_CONFIG_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, Any]]] = {
    model.__tablename__: tuple(
        (column.name, column) for column in model.__table__.columns
    )
    for model in AIOPS135_NODE_CONFIG_MODELS
}

AIOPS135_NODE_CONFIG_TABLE_MODELS = {
    model.__tablename__: model for model in AIOPS135_NODE_CONFIG_MODELS
}

AIOPS135_NODE_CONFIG_UNIQUE_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": NodeModelConfigVersion.__tablename__,
        "name": "uq_node_model_config_version_version",
        "columns": ("version",),
        "unique": True,
        "mysql_online": True,
    },
    {
        "table": NodeModelConfigVersion.__tablename__,
        "name": "uq_node_model_config_version_active_slot",
        "columns": ("active_slot",),
        "unique": True,
        "mysql_online": True,
    },
    {
        "table": NodeModelConfigEntry.__tablename__,
        "name": "uq_node_model_config_entry_version_node",
        "columns": ("version_id", "node_key"),
        "unique": True,
        "mysql_online": True,
    },
)

AIOPS135_NODE_CONFIG_FOREIGN_KEY_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": NodeModelConfigEntry.__tablename__,
        "name": "fk_node_model_config_entry_version_id",
        "columns": ("version_id",),
        "referred_table": NodeModelConfigVersion.__tablename__,
        "referred_columns": ("id",),
        "ondelete": "CASCADE",
    },
)

AIOPS135_NODE_CONFIG_CHECK_SPECS: Sequence[Dict[str, Any]] = tuple(sorted(
    (
        {
            "table": model.__tablename__,
            "name": str(constraint.name),
            "sqltext": str(constraint.sqltext),
            "columns": tuple(
                column.name
                for column in model.__table__.columns
                if re.search(
                    rf"\b{re.escape(column.name)}\b",
                    str(constraint.sqltext),
                    re.IGNORECASE,
                )
            ),
        }
        for model in AIOPS135_NODE_CONFIG_MODELS
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    ),
    key=lambda item: (str(item["table"]), str(item["name"])),
))

QUERY_GOVERNANCE_BATCH_SUMMARY_TABLE_MODELS = {
    QueryGovernanceBatchSummary.__tablename__: QueryGovernanceBatchSummary,
}

QUERY_GOVERNANCE_CONTEXT_SNAPSHOT_TABLE_MODELS = {
    QueryGovernanceContextSnapshot.__tablename__: QueryGovernanceContextSnapshot,
}

QUERY_GOVERNANCE_CONTEXT_SNAPSHOT_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, Any]]] = {
    QueryGovernanceContextSnapshot.__tablename__: tuple(
        (column.name, column)
        for column in QueryGovernanceContextSnapshot.__table__.columns
    ),
}

QUERY_GOVERNANCE_CONTEXT_SNAPSHOT_UNIQUE_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": QueryGovernanceContextSnapshot.__tablename__,
        "name": "uq_qg_context_batch_brand_type",
        "columns": ("batch_id", "brand", "context_type"),
        "unique": True,
        "mysql_online": True,
    },
)

QUERY_GOVERNANCE_CONTEXT_SNAPSHOT_INDEX_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": QueryGovernanceContextSnapshot.__tablename__,
        "name": "ix_qg_context_batch_type",
        "columns": ("batch_id", "context_type", "id"),
    },
)

RELEASE_TABLE_CHARSET_SPECS: Dict[str, Dict[str, str]] = {
    QueryGovernanceContextSnapshot.__tablename__: {
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci",
    },
}

QUERY_GOVERNANCE_BATCH_SUMMARY_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, Any]]] = {
    QueryGovernanceBatchSummary.__tablename__: tuple(
        (column.name, column) for column in QueryGovernanceBatchSummary.__table__.columns
    ),
}

QUERY_GOVERNANCE_IMAGE_ASSET_TABLE_MODELS = {
    QueryGovernanceImageAsset.__tablename__: QueryGovernanceImageAsset,
}

QUERY_GOVERNANCE_IMAGE_ASSET_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, Any]]] = {
    QueryGovernanceImageAsset.__tablename__: tuple(
        (column.name, column) for column in QueryGovernanceImageAsset.__table__.columns
    ),
}

QUERY_GOVERNANCE_IMAGE_ASSET_UNIQUE_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": QueryGovernanceImageAsset.__tablename__,
        "name": "uq_qg_image_asset_brand_product",
        "columns": ("brand", "product_id"),
        "unique": True,
        "mysql_online": True,
    },
)

QUERY_GOVERNANCE_IMAGE_ASSET_INDEX_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": QueryGovernanceImageAsset.__tablename__,
        "name": "ix_qg_image_asset_brand_spu",
        "columns": ("brand", "spu_id", "id"),
    },
    {
        "table": QueryGovernanceImageAsset.__tablename__,
        "name": "ix_qg_image_asset_source_hash",
        "columns": ("source_url_hash",),
    },
)

QUERY_GOVERNANCE_BATCH_SUMMARY_UNIQUE_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": QueryGovernanceBatchSummary.__tablename__,
        "name": "uq_qg_batch_summary_batch_brand",
        "columns": ("batch_id", "brand"),
        "unique": True,
        "mysql_online": True,
    },
)

QUERY_GOVERNANCE_BATCH_SUMMARY_INDEX_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": QueryGovernanceBatchSummary.__tablename__,
        "name": "ix_qg_batch_summary_brand_date",
        "columns": ("brand", "data_date", "id"),
    },
)

QUERY_GOVERNANCE_INDEX_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": QueryGovernanceTask.__tablename__,
        "name": "ix_qg_task_batch_brand_date",
        "columns": ("batch_id", "brand", "data_date", "id"),
        "mysql_online": True,
    },
    {
        "table": "query_governance_candidate",
        "name": "ix_qg_candidate_batch_brand_query",
        "columns": ("batch_id", "brand", "query_text"),
        "mysql_online": True,
    },
)

AIOPS130B_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, Any]]] = {
    SchedulerJobRun.__tablename__: tuple(
        (column.name, column)
        for column in SchedulerJobRun.__table__.columns
    ),
}
AIOPS130B_LEADER_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, Any]]] = {
    SchedulerLeaderLease.__tablename__: tuple(
        (column.name, column) for column in SchedulerLeaderLease.__table__.columns
    ),
}

AIOPS130B_TABLE_MODELS = {
    SchedulerJobRun.__tablename__: SchedulerJobRun,
    SchedulerLeaderLease.__tablename__: SchedulerLeaderLease,
}

AIOPS130B_UNIQUE_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": SchedulerJobRun.__tablename__,
        "name": "uq_scheduler_job_run_idempotency_key",
        "columns": ("idempotency_key",),
        "unique": True,
        "mysql_online": True,
    },
    {
        "table": SchedulerJobRun.__tablename__,
        "name": "uq_scheduler_job_run_slot",
        "columns": ("job_id", "scheduled_for"),
        "unique": True,
        "mysql_online": True,
    },
    {
        "table": SchedulerLeaderLease.__tablename__,
        "name": "uq_scheduler_leader_lease_name",
        "columns": ("leader_name",),
        "unique": True,
        "mysql_online": True,
    },
)

AIOPS130B_INDEX_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": SchedulerJobRun.__tablename__,
        "name": "ix_scheduler_job_run_job_status_heartbeat",
        "columns": ("job_id", "status", "heartbeat_at", "id"),
        "mysql_online": True,
    },
    {
        "table": SchedulerJobRun.__tablename__,
        "name": "ix_scheduler_job_run_status_created",
        "columns": ("status", "created_at", "id"),
        "mysql_online": True,
    },
    {
        "table": SchedulerLeaderLease.__tablename__,
        "name": "ix_scheduler_leader_lease_status_expiry",
        "columns": ("status", "lease_expires_at", "id"),
        "mysql_online": True,
    },
)

AIOPS127_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, Any]]] = {
    InventoryRecoveryTask.__tablename__: (
        (
            "last_action_request_id",
            InventoryRecoveryTask.__table__.c.last_action_request_id,
        ),
        (
            "last_action_generation",
            InventoryRecoveryTask.__table__.c.last_action_generation,
        ),
    ),
    **{
        model.__tablename__: tuple(
            (column.name, column)
            for column in model.__table__.columns
        )
        for model in AIOPS127_ACTION_FACT_MODELS
    },
}

AIOPS127_TABLE_MODELS = {
    InventoryRecoveryTask.__tablename__: InventoryRecoveryTask,
    **{
        model.__tablename__: model
        for model in AIOPS127_ACTION_FACT_MODELS
    },
}

AIOPS127_FOREIGN_KEY_SPECS: Sequence[Dict[str, Any]] = tuple(sorted(
    (
        {
            "table": model.__tablename__,
            "name": str(
                constraint.name
                or "fk_"
                + model.__tablename__
                + "_"
                + "_".join(element.parent.name for element in constraint.elements)
            ),
            "columns": tuple(
                element.parent.name for element in constraint.elements
            ),
            "referred_table": constraint.elements[0].column.table.name,
            "referred_columns": tuple(
                element.column.name for element in constraint.elements
            ),
        }
        for model in AIOPS127_ACTION_FACT_MODELS
        for constraint in model.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ),
    key=lambda item: (str(item["table"]), str(item["name"])),
))

AIOPS127_UNIQUE_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": ActionIntent.__tablename__,
        "name": "uq_action_intent_idempotency_key",
        "columns": ("idempotency_key",),
        "unique": True,
        "mysql_online": True,
    },
    {
        "table": ActionAttempt.__tablename__,
        "name": "uq_action_attempt_intent_no",
        "columns": ("intent_id", "attempt_no"),
        "unique": True,
        "mysql_online": True,
    },
    {
        "table": ActionAttempt.__tablename__,
        "name": "uq_action_attempt_id_intent",
        "columns": ("id", "intent_id"),
        "unique": True,
        "mysql_online": True,
    },
    {
        "table": ActionAttempt.__tablename__,
        "name": "uq_action_attempt_identity_facts",
        "columns": (
            "id",
            "intent_id",
            "external_request_id",
            "execution_generation",
        ),
        "unique": True,
        "mysql_online": True,
    },
    {
        "table": ActionFenceEvidence.__tablename__,
        "name": "uq_action_fence_attempt_generation",
        "columns": ("attempt_id", "execution_generation"),
        "unique": True,
        "mysql_online": True,
    },
    {
        "table": ActionTerminalFact.__tablename__,
        "name": "uq_action_terminal_fact_intent",
        "columns": ("intent_id",),
        "unique": True,
        "mysql_online": True,
    },
)

AIOPS127_INDEX_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": ActionIntent.__tablename__,
        "name": "ix_action_intent_status_created",
        "columns": ("status", "created_at", "id"),
        "mysql_online": True,
    },
    {
        "table": ActionAttempt.__tablename__,
        "name": "ix_action_attempt_intent_created",
        "columns": ("intent_id", "created_at", "id"),
        "mysql_online": True,
    },
    {
        "table": ActionAttempt.__tablename__,
        "name": "ix_action_attempt_status_lease",
        "columns": ("status", "lease_expires_at", "id"),
        "mysql_online": True,
    },
    {
        "table": ActionFenceEvidence.__tablename__,
        "name": "ix_action_fence_intent_created",
        "columns": ("intent_id", "created_at", "id"),
        "mysql_online": True,
    },
    {
        "table": ActionTerminalFact.__tablename__,
        "name": "ix_action_terminal_fact_status_created",
        "columns": ("status", "created_at", "id"),
        "mysql_online": True,
    },
)

AIOPS127_MYSQL_COLUMN_RISK_EVIDENCE = {
    "transactionalRollback": False,
    "failureRecovery": "verify_and_rerun_idempotently",
    "columns": {
        "inventory_recovery_task.last_action_request_id",
        "inventory_recovery_task.last_action_generation",
        "action_attempt.execution_generation",
    },
}

AIOPS127_TXSQL_QUALIFIER_COMPATIBLE_COLUMNS = frozenset({
    (InventoryRecoveryTask.__tablename__, "last_action_request_id"),
    (ActionLog.__tablename__, "image_url"),
})

AIOPS131_CHECK_SPECS: Sequence[Dict[str, Any]] = tuple(sorted(
    (
        {
            "table": model.__tablename__,
            "name": str(constraint.name),
            "sqltext": str(constraint.sqltext),
            "columns": tuple(
                column.name
                for column in model.__table__.columns
                if re.search(
                    rf"\b{re.escape(column.name)}\b",
                    str(constraint.sqltext),
                    re.IGNORECASE,
                )
            ),
        }
        for model in AIOPS127_ACTION_FACT_MODELS
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name is not None
    ),
    key=lambda item: (str(item["table"]), str(item["name"])),
))

AIOPS130B_CHECK_SPECS: Sequence[Dict[str, Any]] = tuple(sorted(
    (
        {
            "table": SchedulerJobRun.__tablename__,
            "name": str(constraint.name),
            "sqltext": str(constraint.sqltext),
            "columns": tuple(
                column.name
                for column in SchedulerJobRun.__table__.columns
                if re.search(
                    rf"\b{re.escape(column.name)}\b",
                    str(constraint.sqltext),
                    re.IGNORECASE,
                )
            ),
        }
        for constraint in SchedulerJobRun.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    ),
    key=lambda item: (str(item["table"]), str(item["name"])),
))

AIOPS130B_LEADER_CHECK_SPECS: Sequence[Dict[str, Any]] = tuple(sorted(
    (
        {
            "table": SchedulerLeaderLease.__tablename__,
            "name": str(constraint.name),
            "sqltext": str(constraint.sqltext),
            "columns": tuple(
                column.name
                for column in SchedulerLeaderLease.__table__.columns
                if re.search(
                    rf"\b{re.escape(column.name)}\b",
                    str(constraint.sqltext),
                    re.IGNORECASE,
                )
            ),
        }
        for constraint in SchedulerLeaderLease.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    ),
    key=lambda item: (str(item["table"]), str(item["name"])),
))

RELEASE_CHECK_SPECS: Sequence[Dict[str, Any]] = (
    *AIOPS131_CHECK_SPECS,
    *AIOPS130B_CHECK_SPECS,
    *AIOPS130B_LEADER_CHECK_SPECS,
    *AIOPS135_NODE_CONFIG_CHECK_SPECS,
)

AIOPS131_LEGACY_CHECK_SQL: Dict[tuple[str, str], tuple[str, ...]] = {
    (
        ActionIntent.__tablename__,
        "ck_action_intent_payload_hash_sha256",
    ): ("length(payload_hash) = 64",),
    (
        ActionFenceEvidence.__tablename__,
        "ck_action_fence_fact_hash_sha256",
    ): ("length(fact_hash) = 64",),
    (
        ActionReadback.__tablename__,
        "ck_action_readback_fact_hash_sha256",
    ): ("length(fact_hash) = 64",),
    (
        ActionTerminalFact.__tablename__,
        "ck_action_terminal_fact_fact_hash_sha256",
    ): ("length(fact_hash) = 64",),
}

AIOPS131_LEGACY_CHECK_SQL.update({
    (ActionIntent.__tablename__, "ck_action_intent_action_type_nonempty"):
        ("length(trim(action_type)) > 0",),
    (ActionIntent.__tablename__, "ck_action_intent_idempotency_key_nonempty"):
        ("length(trim(idempotency_key)) > 0",),
    (ActionIntent.__tablename__, "ck_action_intent_payload_json_nonempty"):
        ("length(trim(payload_json)) > 0",),
    (ActionIntent.__tablename__, "ck_action_intent_operator_nonempty"):
        ("length(trim(operator)) > 0",),
    (ActionIntent.__tablename__, "ck_action_intent_brand_nonempty"):
        ("length(trim(brand)) > 0",),
    (ActionIntent.__tablename__, "ck_action_intent_release_nonempty"):
        ("length(trim(`release`)) > 0",),
    (ActionIntent.__tablename__, "ck_action_intent_permission_nonempty"):
        ("length(trim(permission)) > 0",),
    (ActionAttempt.__tablename__, "ck_action_attempt_external_request_id_nonempty"):
        ("external_request_id IS NOT NULL AND length(trim(external_request_id)) > 0",),
    (ActionAttempt.__tablename__, "ck_action_attempt_state_facts"): (
        "(status = 'persisted' AND execution_generation = 0 "
        "AND claim_token IS NULL AND lease_expires_at IS NULL "
        "AND finished_at IS NULL) OR "
        "(status IN ('executing', 'fencing', 'reconciling') "
        "AND execution_generation > 0 "
        "AND claim_token IS NOT NULL AND length(trim(claim_token)) > 0 "
        "AND lease_expires_at IS NOT NULL AND finished_at IS NULL) OR "
        "(status = 'execution_unknown' AND execution_generation > 0 "
        "AND claim_token IS NULL AND lease_expires_at IS NULL "
        "AND finished_at IS NULL) OR "
        "(status IN ('success', 'failed', 'inconclusive') "
        "AND execution_generation > 0 AND claim_token IS NULL "
        "AND lease_expires_at IS NULL AND finished_at IS NOT NULL)",
    ),
    (ActionFenceEvidence.__tablename__, "ck_action_fence_external_request_id_nonempty"):
        ("length(trim(external_request_id)) > 0",),
    (ActionFenceEvidence.__tablename__, "ck_action_fence_claim_token_nonempty"):
        ("length(trim(fence_claim_token)) > 0",),
    (ActionFenceEvidence.__tablename__, "ck_action_fence_evidence_payload_nonempty"):
        ("length(trim(evidence_payload)) > 0",),
    (ActionReadback.__tablename__, "ck_action_readback_observed_payload_nonempty"):
        ("length(trim(observed_payload)) > 0",),
    (ActionTerminalFact.__tablename__, "ck_action_terminal_fact_terminal_code_nonempty"):
        ("length(trim(terminal_code)) > 0",),
    (ActionTerminalFact.__tablename__, "ck_action_terminal_fact_result_json_nonempty"):
        ("length(trim(result_json)) > 0",),
    (ActionTerminalFact.__tablename__, "ck_action_terminal_fact_replay_result_json_nonempty"):
        ("length(trim(replay_result_json)) > 0",),
})

ZHA26_TABLE_MODELS = {
    TopQueryActionPlan.__tablename__: TopQueryActionPlan,
    TopQueryWorkflowRun.__tablename__: TopQueryWorkflowRun,
    TopQueryBudgetAccount.__tablename__: TopQueryBudgetAccount,
    TopQueryBudgetLedger.__tablename__: TopQueryBudgetLedger,
}

ZHA26_ACTION_PLAN_EXTENSION_COLUMNS: Sequence[str] = (
    "workflow_run_id",
    "round_no",
    "idempotency_key",
    "external_request_id",
    "execution_started_at",
    "execution_finished_at",
    "execution_lease_expires_at",
)

ZHA26_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, Any]]] = {
    TopQueryActionPlan.__tablename__: tuple(
        (column_name, TopQueryActionPlan.__table__.c[column_name])
        for column_name in ZHA26_ACTION_PLAN_EXTENSION_COLUMNS
    ),
    TopQueryWorkflowRun.__tablename__: tuple(
        (column.name, column)
        for column in TopQueryWorkflowRun.__table__.columns
    ),
    TopQueryBudgetAccount.__tablename__: tuple(
        (column.name, column)
        for column in TopQueryBudgetAccount.__table__.columns
    ),
    TopQueryBudgetLedger.__tablename__: tuple(
        (column.name, column)
        for column in TopQueryBudgetLedger.__table__.columns
    ),
}

CHECKPOINT_COLUMN_TYPE_UPGRADES: Sequence[Dict[str, Any]] = (
    {
        "table": TopQueryWorkflowRun.__tablename__,
        "column": "checkpoint_payload",
        "allowedSourceTypes": ("TEXT",),
        "definition": TopQueryWorkflowRun.__table__.c.checkpoint_payload,
    },
    {
        "table": QueryGovernanceWorkflowRun.__tablename__,
        "column": "checkpoint_payload",
        "allowedSourceTypes": ("TEXT",),
        "definition": QueryGovernanceWorkflowRun.__table__.c.checkpoint_payload,
    },
    {
        "table": QueryGovernanceStepStatus.__tablename__,
        "column": "result_json",
        "allowedSourceTypes": ("TEXT",),
        "definition": QueryGovernanceStepStatus.__table__.c.result_json,
    },
    {
        "table": QueryGovernanceBatchSummary.__tablename__,
        "column": "payload_json",
        "allowedSourceTypes": ("TEXT",),
        "definition": QueryGovernanceBatchSummary.__table__.c.payload_json,
    },
)

ZHA26_QUERY_INDEX_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": "top_query_workflow_run",
        "name": "ix_top_query_workflow_run_task_status_heartbeat",
        "columns": ("task_id", "status", "heartbeat_at"),
    },
    {
        "table": "top_query_workflow_run",
        "name": "ix_top_query_workflow_run_task_status_lease",
        "columns": ("task_id", "status", "lease_expires_at"),
    },
    {
        "table": "top_query_budget_account",
        "name": "ix_top_query_budget_account_scope_period_end",
        "columns": ("scope_type", "scope_key", "period_end"),
    },
    {
        "table": "top_query_budget_ledger",
        "name": "ix_top_query_budget_ledger_account_created",
        "columns": ("account_id", "created_at"),
    },
)

ZHA26_UNIQUE_SPECS: Sequence[Dict[str, Any]] = (
    {
        "table": TopQueryActionPlan.__tablename__,
        "name": "uq_top_query_action_plan_idempotency_key",
        "columns": ("idempotency_key",),
        "unique": True,
    },
    {
        "table": TopQueryActionPlan.__tablename__,
        "name": "uq_top_query_action_plan_external_request_id",
        "columns": ("external_request_id",),
        "unique": True,
    },
    {
        "table": "top_query_workflow_run",
        "name": "uq_top_query_workflow_run_run_key",
        "columns": ("run_key",),
        "unique": True,
    },
    {
        "table": "top_query_workflow_run",
        "name": "uq_top_query_workflow_run_task_active",
        "columns": ("task_id", "active_slot"),
        "unique": True,
    },
    {
        "table": "top_query_budget_account",
        "name": "uq_top_query_budget_account_scope_period",
        "columns": ("scope_type", "scope_key", "period_start"),
        "unique": True,
    },
    {
        "table": "top_query_budget_ledger",
        "name": "uq_top_query_budget_ledger_idempotency_key",
        "columns": ("idempotency_key",),
        "unique": True,
    },
)

RELEASE_UNIQUE_SPECS: Sequence[Dict[str, Any]] = (
    *ZHA26_UNIQUE_SPECS,
    *AIOPS127_UNIQUE_SPECS,
    *AIOPS130B_UNIQUE_SPECS,
    *AIOPS135_NODE_CONFIG_UNIQUE_SPECS,
    *QUERY_GOVERNANCE_BATCH_SUMMARY_UNIQUE_SPECS,
    *QUERY_GOVERNANCE_CONTEXT_SNAPSHOT_UNIQUE_SPECS,
    *QUERY_GOVERNANCE_IMAGE_ASSET_UNIQUE_SPECS,
)

ZHA26_REQUIRED_TABLES: Sequence[str] = (
    "top_query_budget_account",
    "top_query_budget_ledger",
    "top_query_workflow_run",
)

ZHA26_FOREIGN_KEY_SPECS: Sequence[Dict[str, Any]] = tuple(
    {
        "table": TopQueryBudgetLedger.__tablename__,
        "name": f"fk_top_query_budget_ledger_{column_name}",
        "columns": (column_name,),
        "referred_table": next(iter(
            TopQueryBudgetLedger.__table__.c[column_name].foreign_keys
        )).column.table.name,
        "referred_columns": (next(iter(
            TopQueryBudgetLedger.__table__.c[column_name].foreign_keys
        )).column.name,),
    }
    for column_name in ("account_id", "workflow_run_id")
)

RELEASE_FOREIGN_KEY_SPECS: Sequence[Dict[str, Any]] = (
    *ZHA26_FOREIGN_KEY_SPECS,
    *AIOPS127_FOREIGN_KEY_SPECS,
    *AIOPS135_NODE_CONFIG_FOREIGN_KEY_SPECS,
)

RELEASE_FOREIGN_KEY_TABLE_MODELS = {
    **ZHA26_TABLE_MODELS,
    **AIOPS127_TABLE_MODELS,
    **AIOPS130B_TABLE_MODELS,
    **AIOPS135_NODE_CONFIG_TABLE_MODELS,
    **QUERY_GOVERNANCE_BATCH_SUMMARY_TABLE_MODELS,
}

RELEASE_SCHEMA_COLUMN_MODELS = {
    ActionLog.__tablename__: ActionLog,
    User.__tablename__: User,
    TopQueryTask.__tablename__: TopQueryTask,
    TopQueryActionPlan.__tablename__: TopQueryActionPlan,
    QueryGovernanceActionPlan.__tablename__: QueryGovernanceActionPlan,
    AssistantAgentBinding.__tablename__: AssistantAgentBinding,
    AssistantSessionMeta.__tablename__: AssistantSessionMeta,
    TopQueryExperiment.__tablename__: TopQueryExperiment,
    TopQueryExperimentSample.__tablename__: TopQueryExperimentSample,
    QueryGovernanceBatchSummary.__tablename__: QueryGovernanceBatchSummary,
    QueryGovernanceContextSnapshot.__tablename__: QueryGovernanceContextSnapshot,
    QueryGovernanceImageAsset.__tablename__: QueryGovernanceImageAsset,
}

ZHA26_VERIFICATION_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, Any]]] = {
    **ZHA26_SCHEMA_COLUMNS,
    TopQueryActionPlan.__tablename__: tuple(
        (column.name, column)
        for column in TopQueryActionPlan.__table__.columns
    ),
}

RELEASE_VERIFICATION_SCHEMA_COLUMNS: Dict[str, Sequence[tuple[str, Any]]] = {
    **AIOPS127_SCHEMA_COLUMNS,
    **ZHA26_VERIFICATION_SCHEMA_COLUMNS,
    **AIOPS130B_SCHEMA_COLUMNS,
    **AIOPS130B_LEADER_SCHEMA_COLUMNS,
    **AIOPS135_NODE_CONFIG_SCHEMA_COLUMNS,
    **QUERY_GOVERNANCE_BATCH_SUMMARY_SCHEMA_COLUMNS,
    **QUERY_GOVERNANCE_CONTEXT_SNAPSHOT_SCHEMA_COLUMNS,
    **QUERY_GOVERNANCE_IMAGE_ASSET_SCHEMA_COLUMNS,
    **{
        table_name: tuple(
            (
                column_name,
                RELEASE_SCHEMA_COLUMN_MODELS[table_name].__table__.c[column_name],
            )
            for column_name, _definition in column_specs
        )
        for table_name, column_specs in RELEASE_SCHEMA_COLUMNS.items()
    },
    TopQueryActionPlan.__tablename__: tuple({
        **dict(ZHA26_VERIFICATION_SCHEMA_COLUMNS[TopQueryActionPlan.__tablename__]),
        **{
            column_name: RELEASE_SCHEMA_COLUMN_MODELS[
                TopQueryActionPlan.__tablename__
            ].__table__.c[column_name]
            for column_name, _definition
            in RELEASE_SCHEMA_COLUMNS[TopQueryActionPlan.__tablename__]
        },
    }.items()),
    ActionFenceEvidence.__tablename__: tuple(
        (column.name, column)
        for column in ActionFenceEvidence.__table__.columns
    ),
}

RELEASE_VERIFICATION_INDEX_SPECS: Sequence[Dict[str, Any]] = (
    *RELEASE_INDEX_SPECS,
    *ZHA26_QUERY_INDEX_SPECS,
    *AIOPS127_INDEX_SPECS,
    *AIOPS130B_INDEX_SPECS,
    *QUERY_GOVERNANCE_BATCH_SUMMARY_INDEX_SPECS,
    *QUERY_GOVERNANCE_CONTEXT_SNAPSHOT_INDEX_SPECS,
    *QUERY_GOVERNANCE_IMAGE_ASSET_INDEX_SPECS,
    *QUERY_GOVERNANCE_INDEX_SPECS,
)

RELEASE_VERIFICATION_TABLES: Sequence[str] = tuple(dict.fromkeys(
    (
        *RELEASE_VERIFICATION_SCHEMA_COLUMNS,
        *(str(spec["table"]) for spec in RELEASE_VERIFICATION_INDEX_SPECS),
    )
))

RELEASE_IDENTITY_TABLE_MODELS = {
    **ZHA26_TABLE_MODELS,
    **AIOPS127_TABLE_MODELS,
    **AIOPS130B_TABLE_MODELS,
    **AIOPS135_NODE_CONFIG_TABLE_MODELS,
    **QUERY_GOVERNANCE_BATCH_SUMMARY_TABLE_MODELS,
    **QUERY_GOVERNANCE_CONTEXT_SNAPSHOT_TABLE_MODELS,
    **QUERY_GOVERNANCE_IMAGE_ASSET_TABLE_MODELS,
}


def mask_uri(uri: str) -> str:
    return re.sub(r"(://[^:]+:)[^@]+@", r"\1***@", uri)


def parse_assistant_bootstrap(raw_payload: str | None) -> Dict[str, Any]:
    if not raw_payload:
        return {"brandSpaces": [], "adminUsernames": ["admin"]}

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid ASSISTANT_BOOTSTRAP_JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("ASSISTANT_BOOTSTRAP_JSON must be an object")

    brand_spaces = payload.get("brandSpaces") or []
    if not isinstance(brand_spaces, list):
        raise ValueError("brandSpaces must be a list")

    normalized_spaces: List[Dict[str, Any]] = []
    for item in brand_spaces:
        if not isinstance(item, dict):
            raise ValueError("each brandSpace must be an object")
        brand = str(item.get("brand") or "").strip()
        display_name = str(item.get("displayName") or brand).strip()
        binding = item.get("defaultBinding") or {}
        if not brand or not display_name or not isinstance(binding, dict):
            raise ValueError("each brandSpace must include brand and displayName")
        normalized_spaces.append({
            "brand": brand,
            "displayName": display_name,
            "defaultBinding": {
                "bindingType": str(binding.get("bindingType") or "agent"),
                "displayName": str(binding.get("displayName") or display_name),
                "modelDisplayName": str(binding.get("modelDisplayName") or binding.get("modelName") or "PI Agent"),
                "modelName": str(binding.get("modelName") or os.getenv("PI_AGENT_MODEL") or ""),
                "availableModels": [
                    str(model).strip()
                    for model in (binding.get("availableModels") or [])
                    if str(model).strip()
                ],
                "supportsModelSwitch": bool(binding.get("supportsModelSwitch")),
            },
        })

    admin_usernames = payload.get("adminUsernames") or ["admin"]
    if not isinstance(admin_usernames, list) or not admin_usernames:
        raise ValueError("adminUsernames must be a non-empty list when provided")

    return {
        "brandSpaces": normalized_spaces,
        "adminUsernames": [str(username).strip() for username in admin_usernames if str(username).strip()],
    }


def _ensure_release_schema_impl(
    engine,
    log: Logger = print,
    *,
    fault_injector: Callable[[str], None] | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> Dict[str, Any]:
    def set_stage(stage: str) -> None:
        if stage_callback is not None:
            stage_callback(stage)

    set_stage("preflight")
    if fault_injector is not None and os.getenv("ENV", "test") == "prod":
        raise RuntimeError("migration fault injection is forbidden in ENV=prod")

    def checkpoint(stage: str) -> None:
        if fault_injector is not None:
            fault_injector(stage)

    _raise_on_release_schema_preflight_blockers(engine)
    if engine.dialect.name in {"mysql", "mariadb"}:
        log(
            "AIOPS127_MYSQL_COLUMN_RISK: "
            + json.dumps({
                **AIOPS127_MYSQL_COLUMN_RISK_EVIDENCE,
                "columns": sorted(
                    AIOPS127_MYSQL_COLUMN_RISK_EVIDENCE["columns"]
                ),
            }, ensure_ascii=False, sort_keys=True)
        )
    set_stage("create_tables")
    Base.metadata.create_all(bind=engine)
    checkpoint("create_tables")
    set_stage("charsets")
    ensured_table_charsets = ensure_release_table_charsets(
        engine, RELEASE_TABLE_CHARSET_SPECS
    )
    checkpoint("charsets")
    set_stage("primary_keys")
    ensured_primary_keys = ensure_release_primary_keys(
        engine,
        {
            **AIOPS127_TABLE_MODELS,
            **AIOPS130B_TABLE_MODELS,
            **AIOPS135_NODE_CONFIG_TABLE_MODELS,
            **QUERY_GOVERNANCE_BATCH_SUMMARY_TABLE_MODELS,
        },
    )
    checkpoint("primary_keys")
    set_stage("columns")
    added_columns = ensure_release_schema_upgrades(engine, RELEASE_SCHEMA_COLUMNS)
    normalized_query_governance_action_plans = (
        normalize_query_governance_action_plan_statuses(engine)
    )
    retired_assistant_binding_columns = retire_legacy_assistant_binding_columns(engine)
    added_columns.extend(ensure_release_schema_upgrades(engine, ZHA26_SCHEMA_COLUMNS))
    added_columns.extend(ensure_release_schema_upgrades(engine, AIOPS127_SCHEMA_COLUMNS))
    added_columns.extend(ensure_release_schema_upgrades(engine, AIOPS130B_SCHEMA_COLUMNS))
    added_columns.extend(
        ensure_release_schema_upgrades(engine, AIOPS130B_LEADER_SCHEMA_COLUMNS)
    )
    backfilled_action_attempt_request_ids = (
        ensure_action_attempt_external_request_ids(engine)
    )
    upgraded_column_types = ensure_release_column_type_upgrades(
        engine,
        CHECKPOINT_COLUMN_TYPE_UPGRADES,
    )
    checkpoint("columns")
    _raise_on_column_contract_mismatches(
        engine,
        allow_action_attempt_request_id_nullable=True,
    )
    inspector = inspect(engine)
    missing_foreign_keys = {
        str(spec["name"])
        for spec in RELEASE_FOREIGN_KEY_SPECS
        if not _schema_foreign_key_matches(inspector, spec)
    }
    set_stage("checks")
    ensured_check_constraints = ensure_action_fact_check_constraints(engine)
    checkpoint("checks")
    set_stage("foreign_keys")
    ensure_action_attempt_external_request_id_not_null(engine)
    ensured_indexes: List[str] = []
    if engine.dialect.name in {"mysql", "mariadb"}:
        ensured_indexes.extend(
            ensure_release_indexes(engine, AIOPS127_UNIQUE_SPECS)
        )
        ensured_indexes.extend(
            ensure_release_indexes(engine, AIOPS135_NODE_CONFIG_UNIQUE_SPECS)
        )
    ensured_foreign_keys = ensure_release_foreign_keys(
        engine,
        RELEASE_FOREIGN_KEY_SPECS,
    )
    inspector = inspect(engine)
    ensured_foreign_keys = list(dict.fromkeys((
        *ensured_foreign_keys,
        *(
            str(spec["name"])
            for spec in RELEASE_FOREIGN_KEY_SPECS
            if str(spec["name"]) in missing_foreign_keys
            and _schema_foreign_key_matches(inspector, spec)
        ),
    )))
    _raise_on_column_contract_mismatches(engine)
    checkpoint("foreign_keys")
    set_stage("indexes")
    ensured_indexes.extend(ensure_release_indexes(engine, RELEASE_UNIQUE_SPECS))
    ensured_indexes.extend(ensure_release_indexes(engine, RELEASE_INDEX_SPECS))
    ensured_indexes.extend(ensure_release_indexes(engine, ZHA26_QUERY_INDEX_SPECS))
    ensured_indexes.extend(ensure_release_indexes(engine, AIOPS127_INDEX_SPECS))
    ensured_indexes.extend(ensure_release_indexes(engine, AIOPS130B_INDEX_SPECS))
    ensured_indexes.extend(
        ensure_release_indexes(engine, AIOPS135_NODE_CONFIG_UNIQUE_SPECS)
    )
    ensured_indexes.extend(
        ensure_release_indexes(engine, QUERY_GOVERNANCE_BATCH_SUMMARY_INDEX_SPECS)
    )
    ensured_indexes.extend(
        ensure_release_indexes(engine, QUERY_GOVERNANCE_IMAGE_ASSET_INDEX_SPECS)
    )
    ensured_indexes.extend(
        ensure_release_indexes(engine, QUERY_GOVERNANCE_INDEX_SPECS)
    )
    checkpoint("indexes")
    set_stage("verification")
    verification = verify_release_schema(engine)
    if not verification["ok"]:
        raise RuntimeError(
            "release schema verification failed: "
            + ", ".join(_failed_release_schema_checks(verification))
        )
    checkpoint("verification")
    if added_columns:
        log(f"ADDED_COLUMNS: {json.dumps(added_columns, ensure_ascii=False)}")
    if upgraded_column_types:
        log(
            "UPGRADED_COLUMN_TYPES: "
            + json.dumps(upgraded_column_types, ensure_ascii=False)
        )
    if ensured_primary_keys:
        log(
            "ENSURED_PRIMARY_KEYS: "
            + json.dumps(ensured_primary_keys, ensure_ascii=False)
        )
    if ensured_indexes:
        log(f"ENSURED_INDEXES: {json.dumps(ensured_indexes, ensure_ascii=False)}")
    if retired_assistant_binding_columns:
        log(
            "RETIRED_ASSISTANT_BINDING_COLUMNS: "
            + json.dumps(retired_assistant_binding_columns, ensure_ascii=False)
        )
    if any(normalized_query_governance_action_plans.values()):
        log(
            "NORMALIZED_QUERY_GOVERNANCE_ACTION_PLAN_STATUSES: "
            + json.dumps(
                normalized_query_governance_action_plans,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return {
        "addedColumns": added_columns,
        "upgradedColumnTypes": upgraded_column_types,
        "ensuredPrimaryKeys": ensured_primary_keys,
        "ensuredIndexes": ensured_indexes,
        "ensuredTableCharsets": ensured_table_charsets,
        "ensuredForeignKeys": ensured_foreign_keys,
        "ensuredCheckConstraints": ensured_check_constraints,
        "backfilledActionAttemptRequestIds": backfilled_action_attempt_request_ids,
        "retiredAssistantBindingColumns": retired_assistant_binding_columns,
        "normalizedQueryGovernanceActionPlans": (
            normalized_query_governance_action_plans
        ),
    }


def ensure_release_schema(
    engine,
    log: Logger = print,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> Dict[str, Any]:
    stage = "preflight"

    def remember_stage(value: str) -> None:
        nonlocal stage
        stage = value

    try:
        return _ensure_release_schema_impl(
            engine,
            log=log,
            fault_injector=fault_injector,
            stage_callback=remember_stage,
        )
    except ReleaseSchemaMigrationError:
        raise
    except Exception as exc:
        raise ReleaseSchemaMigrationError(stage, exc) from exc


def ensure_release_schema_upgrades(
    engine,
    column_specs: Dict[str, Sequence[tuple[str, Any]]],
) -> List[Dict[str, str]]:
    inspector = inspect(engine)
    added: List[Dict[str, str]] = []
    quote = engine.dialect.identifier_preparer.quote
    with engine.begin() as conn:
        for table_name, columns in column_specs.items():
            existing_tables = set(inspector.get_table_names())
            if table_name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, definition in columns:
                if column_name in existing_columns:
                    continue
                if isinstance(definition, str):
                    column_ddl = f"{quote(column_name)} {definition}"
                else:
                    column_ddl = str(CreateColumn(definition).compile(
                        dialect=engine.dialect
                    ))
                conn.execute(text(
                    f"ALTER TABLE {quote(table_name)} ADD COLUMN {column_ddl}"
                ))
                added.append({"table": table_name, "column": column_name})
                inspector = inspect(engine)
                existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
    return added


def ensure_release_table_charsets(
    engine,
    charset_specs: Mapping[str, Mapping[str, str]],
) -> List[Dict[str, str]]:
    """Convert existing MySQL tables before storing arbitrary UTF-8 text.

    SQLite has no table charset metadata, so the contract is a no-op there.
    The operation is idempotent and only emits DDL when the current collation
    differs from the declared release contract.
    """

    if engine.dialect.name not in {"mysql", "mariadb"}:
        return []
    inspector = inspect(engine)
    if not hasattr(inspector, "get_table_names"):
        return []
    table_names = set(inspector.get_table_names())
    quote = engine.dialect.identifier_preparer.quote
    changes: List[Dict[str, str]] = []
    with engine.begin() as conn:
        for table_name, spec in charset_specs.items():
            if table_name not in table_names:
                continue
            current = conn.execute(
                text(
                    "SELECT TABLE_COLLATION "
                    "FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name"
                ),
                {"table_name": table_name},
            ).scalar_one_or_none()
            expected_collation = str(spec["collation"])
            payload_collation = conn.execute(
                text(
                    "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name "
                    "AND COLUMN_NAME = 'payload'"
                ),
                {"table_name": table_name},
            ).scalar_one_or_none()
            if (str(current or "").lower() == expected_collation.lower()
                    and str(payload_collation or "").lower() == expected_collation.lower()):
                continue
            conn.execute(
                text(
                    f"ALTER TABLE {quote(table_name)} CONVERT TO CHARACTER SET "
                    f"{spec['charset']} COLLATE {spec['collation']}"
                )
            )
            changes.append(
                {
                    "table": table_name,
                    "charset": str(spec["charset"]),
                    "collation": expected_collation,
                }
            )
    return changes


def normalize_query_governance_action_plan_statuses(engine) -> Dict[str, int]:
    """Canonicalize statuses written by the retired automatic reorder writer."""
    counts = {
        "verified": 0,
        "rolledBack": 0,
        "rollbackFailed": 0,
        "executionUnknown": 0,
        "failed": 0,
    }
    table_name = QueryGovernanceActionPlan.__tablename__
    inspector = inspect(engine)
    if not hasattr(inspector, "get_table_names"):
        return counts
    if table_name not in set(inspector.get_table_names()):
        return counts
    required_columns = {
        "id",
        "action_type",
        "status",
        "response_json",
        "error_code",
        "updated_at",
        "executor_type",
        "executed_at",
        "failure_stage",
    }
    actual_columns = {
        str(column["name"]) for column in inspector.get_columns(table_name)
    }
    if not required_columns.issubset(actual_columns):
        return counts

    quote = engine.dialect.identifier_preparer.quote
    quoted_table = quote(table_name)
    with engine.begin() as conn:
        rows = conn.execute(text(
            f"SELECT {quote('id')}, {quote('status')}, {quote('response_json')}, "
            f"{quote('error_code')} FROM {quoted_table} "
            f"WHERE {quote('action_type')} = :action_type "
            f"AND {quote('status')} IN (:executed_status, :error_status)"
        ), {
            "action_type": "query_reorder",
            "executed_status": "executed",
            "error_status": "error",
        }).mappings().all()
        for row in rows:
            legacy_status = str(row["status"])
            if legacy_status == "executed":
                target_status = "verified"
                failure_stage = None
                count_key = "verified"
            else:
                target_status, failure_stage, count_key = (
                    _legacy_query_reorder_error_status(
                        row.get("response_json"),
                        row.get("error_code"),
                    )
                )
            result = conn.execute(text(
                f"UPDATE {quoted_table} SET "
                f"{quote('status')} = :target_status, "
                f"{quote('executor_type')} = :executor_type, "
                f"{quote('failure_stage')} = :failure_stage "
                f"WHERE {quote('id')} = :row_id "
                f"AND {quote('status')} = :legacy_status"
            ), {
                "target_status": target_status,
                "executor_type": "ai_auto",
                "failure_stage": failure_stage,
                "row_id": row["id"],
                "legacy_status": legacy_status,
            })
            if result.rowcount not in {-1, 1}:
                raise RuntimeError(
                    "query governance action plan status migration conflict: "
                    f"id={row['id']} status={legacy_status}"
                )
            counts[count_key] += 1
    return counts


def _legacy_query_reorder_error_status(
    response_json: Any,
    error_code: Any,
) -> tuple[str, str, str]:
    try:
        response = json.loads(str(response_json or "{}"))
    except (TypeError, ValueError):
        response = {}
    verification = response.get("verification") if isinstance(response, dict) else {}
    rollback = (
        verification.get("rollback")
        if isinstance(verification, dict)
        else {}
    )
    rollback_status = str(
        rollback.get("status") if isinstance(rollback, dict) else ""
    ).strip().lower()
    if rollback_status == "success":
        return "rolled_back", "verification", "rolledBack"
    if rollback_status == "error":
        return "rollback_failed", "rollback", "rollbackFailed"
    if str(error_code or "").strip().upper() in {
        "ACTION_READBACK_FAILED",
        "ACTION_READBACK_INCONCLUSIVE",
        "ACTION_RECOVERY_READBACK_FAILED",
    }:
        return "execution_unknown", "verification", "executionUnknown"
    return "failed", "execution", "failed"


def retire_legacy_assistant_binding_columns(engine) -> List[str]:
    """Remove obsolete agent/team identity columns from assistant bindings."""
    table_name = AssistantAgentBinding.__tablename__
    inspector = inspect(engine)
    # Some migration-order tests replace the SQLAlchemy inspector with a
    # sentinel after stubbing all schema operations.  There is no schema to
    # inspect in that case, so retirement is a no-op rather than a migration
    # failure.  Real SQLAlchemy inspectors always expose get_table_names().
    if not hasattr(inspector, "get_table_names"):
        return []
    if table_name not in set(inspector.get_table_names()):
        return []
    columns = [str(column["name"]) for column in inspector.get_columns(table_name)]
    retired = [
        name for name in columns
        if name.endswith("_agent_id") or name.endswith("_team_id")
    ]
    if not retired:
        return []
    if engine.dialect.name not in {"sqlite", "mysql", "mariadb"}:
        raise RuntimeError(
            f"legacy assistant binding column retirement unsupported for {engine.dialect.name}"
        )
    quote = engine.dialect.identifier_preparer.quote
    with engine.begin() as conn:
        for column_name in retired:
            conn.execute(text(
                f"ALTER TABLE {quote(table_name)} DROP COLUMN {quote(column_name)}"
            ))
    return retired


def ensure_release_primary_keys(
    engine,
    table_models: Dict[str, Any],
) -> List[str]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    ensured: List[str] = []
    quote = engine.dialect.identifier_preparer.quote

    for table_name, model in table_models.items():
        if table_name not in table_names or _primary_key_identity_matches(
            inspector,
            table_name,
            model,
            engine.dialect,
        ):
            continue
        if engine.dialect.name == "sqlite":
            _rebuild_sqlite_release_table_from_model(engine, table_name, model)
            ensured.append(table_name)
            inspector = inspect(engine)
            continue
        if engine.dialect.name not in {"mysql", "mariadb"}:
            raise RuntimeError(
                f"primary key migration unsupported for {engine.dialect.name}: {table_name}"
            )

        actual_columns = {
            str(column["name"]): column
            for column in inspector.get_columns(table_name)
        }
        id_column = actual_columns.get("id")
        primary_key = tuple(
            str(column)
            for column in (
                inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
            )
        )
        expected_id = model.__table__.c.id
        id_type = str(expected_id.type.compile(dialect=engine.dialect))
        with engine.begin() as conn:
            if id_column is None:
                conn.execute(text(
                    f"ALTER TABLE {quote(table_name)} "
                    f"ADD COLUMN {quote('id')} {id_type} NOT NULL "
                    "AUTO_INCREMENT PRIMARY KEY FIRST"
                ))
            elif not primary_key:
                conn.execute(text(
                    f"ALTER TABLE {quote(table_name)} "
                    f"MODIFY COLUMN {quote('id')} {id_type} NOT NULL AUTO_INCREMENT, "
                    f"ADD PRIMARY KEY ({quote('id')})"
                ))
            else:
                conn.execute(text(
                    f"ALTER TABLE {quote(table_name)} "
                    f"MODIFY COLUMN {quote('id')} {id_type} NOT NULL AUTO_INCREMENT"
                ))
        ensured.append(table_name)
        inspector = inspect(engine)
    return ensured


def _rebuild_sqlite_release_table_from_model(engine, table_name: str, model) -> None:
    inspector = inspect(engine)
    existing_columns = {
        str(column["name"]) for column in inspector.get_columns(table_name)
    }
    model_columns = {column.name for column in model.__table__.columns}
    extra_columns = existing_columns - model_columns
    if extra_columns:
        raise RuntimeError(
            f"release schema verification failed: unexpected columns on {table_name}: "
            + ", ".join(sorted(extra_columns))
        )

    temp_table_name = f"__release_{table_name}_new"
    metadata = MetaData()
    temp_table = model.__table__.to_metadata(metadata, name=temp_table_name)
    quote = engine.dialect.identifier_preparer.quote
    common_columns = [
        column.name
        for column in model.__table__.columns
        if column.name in existing_columns
    ]
    common_expr = ", ".join(quote(column) for column in common_columns)
    with engine.begin() as conn:
        index_statements = conn.execute(text("""
            SELECT sql
            FROM sqlite_master
            WHERE type = 'index'
              AND tbl_name = :table_name
              AND sql IS NOT NULL
            ORDER BY name
        """), {"table_name": table_name}).scalars().all()
        conn.execute(CreateTable(temp_table))
        if common_columns:
            conn.execute(text(
                f"INSERT INTO {quote(temp_table_name)} ({common_expr}) "
                f"SELECT {common_expr} FROM {quote(table_name)}"
            ))
        conn.execute(text(f"DROP TABLE {quote(table_name)}"))
        conn.execute(text(
            f"ALTER TABLE {quote(temp_table_name)} RENAME TO {quote(table_name)}"
        ))
        for statement in index_statements:
            conn.execute(text(str(statement)))


def ensure_release_column_type_upgrades(
    engine,
    type_upgrade_specs: Iterable[Dict[str, Any]],
) -> List[Dict[str, str]]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    quote = engine.dialect.identifier_preparer.quote
    planned: List[Dict[str, Any]] = []

    for spec in type_upgrade_specs:
        table_name = str(spec["table"])
        column_name = str(spec["column"])
        if table_name not in table_names:
            continue
        actual = next(
            (
                column
                for column in inspector.get_columns(table_name)
                if str(column.get("name")) == column_name
            ),
            None,
        )
        if actual is None:
            continue
        expected = spec["definition"]
        contract = _column_contract_check(actual, expected, engine.dialect)
        if contract["ok"]:
            continue

        actual_signature = _column_type_signature(actual["type"], engine.dialect)
        expected_signature = _column_type_signature(expected.type, engine.dialect)
        source_type = str(actual_signature[0])
        if not _column_type_upgrade_is_safe(
            actual,
            expected,
            engine.dialect,
            spec,
        ):
            raise RuntimeError(
                "column type upgrade blocked: "
                f"{table_name}.{column_name} {source_type} -> "
                f"{expected_signature[0]}"
            )
        planned.append({
            "table": table_name,
            "column": column_name,
            "from": source_type,
            "to": str(expected_signature[0]),
            "definition": expected,
        })

    if not planned:
        return []

    with engine.begin() as conn:
        for item in planned:
            definition = item["definition"]
            compiled_type = str(
                definition.type.compile(dialect=engine.dialect)
            )
            nullable_clause = "NULL" if definition.nullable else "NOT NULL"
            conn.execute(text(
                f"ALTER TABLE {quote(item['table'])} "
                f"MODIFY COLUMN {quote(item['column'])} "
                f"{compiled_type} {nullable_clause}"
            ))

    return [
        {
            "table": str(item["table"]),
            "column": str(item["column"]),
            "from": str(item["from"]),
            "to": str(item["to"]),
        }
        for item in planned
    ]


def _column_type_upgrade_is_safe(
    actual: Dict[str, Any],
    expected,
    dialect,
    spec: Dict[str, Any],
) -> bool:
    contract = _column_contract_check(actual, expected, dialect)
    actual_signature = _column_type_signature(actual["type"], dialect)
    expected_signature = _column_type_signature(expected.type, dialect)
    allowed_source_types = {
        str(value).upper() for value in spec.get("allowedSourceTypes", ())
    }
    return (
        dialect.name in {"mysql", "mariadb"}
        and str(actual_signature[0]) in allowed_source_types
        and expected_signature[0] == "MEDIUMTEXT"
        and contract["nullable"]
        and contract["default"]
    )


def _is_allowed_release_column_type_upgrade(
    table_name: str,
    column_name: str,
    actual: Dict[str, Any],
    expected,
    dialect,
) -> bool:
    spec = next(
        (
            item
            for item in CHECKPOINT_COLUMN_TYPE_UPGRADES
            if str(item["table"]) == table_name
            and str(item["column"]) == column_name
        ),
        None,
    )
    return bool(
        spec is not None
        and _column_type_upgrade_is_safe(actual, expected, dialect, spec)
    )


def ensure_release_foreign_keys(
    engine,
    foreign_key_specs: Iterable[Dict[str, Any]],
) -> List[str]:
    specs = tuple(foreign_key_specs)
    inspector = inspect(engine)
    missing: List[Dict[str, Any]] = []
    for spec in specs:
        table_name = str(spec["table"])
        if table_name not in set(inspector.get_table_names()):
            continue
        existing = inspector.get_foreign_keys(table_name)
        matching_columns = [
            foreign_key
            for foreign_key in existing
            if tuple(foreign_key.get("constrained_columns") or ())
            == tuple(spec["columns"])
        ]
        if any(_foreign_key_matches(foreign_key, spec) for foreign_key in matching_columns):
            continue
        if matching_columns or any(
            foreign_key.get("name") == spec["name"] for foreign_key in existing
        ):
            raise RuntimeError(
                f"foreign key mismatch: {spec['name']} on {table_name}"
            )
        missing.append(spec)

    if not missing:
        return []
    if engine.dialect.name == "sqlite":
        rebuilt_tables: List[str] = []
        for table_name in dict.fromkeys(str(spec["table"]) for spec in missing):
            _rebuild_sqlite_table_from_model(engine, table_name)
            rebuilt_tables.append(table_name)
        return [
            str(spec["name"])
            for spec in missing
            if str(spec["table"]) in rebuilt_tables
        ]

    quote = engine.dialect.identifier_preparer.quote
    ensured: List[str] = []
    with engine.begin() as conn:
        for spec in missing:
            columns = ", ".join(quote(str(column)) for column in spec["columns"])
            referred_columns = ", ".join(
                quote(str(column)) for column in spec["referred_columns"]
            )
            conn.execute(text(
                f"ALTER TABLE {quote(str(spec['table']))} "
                f"ADD CONSTRAINT {quote(str(spec['name']))} "
                f"FOREIGN KEY ({columns}) REFERENCES "
                f"{quote(str(spec['referred_table']))} ({referred_columns})"
            ))
            ensured.append(str(spec["name"]))
    return ensured


def _build_create_index_sql(engine, spec: Dict[str, Any]) -> str:
    quote = engine.dialect.identifier_preparer.quote
    columns = tuple(spec["columns"])
    column_expr = ", ".join(quote(str(column)) for column in columns)
    unique_prefix = "UNIQUE " if spec.get("unique") else ""
    statement = (
        f"CREATE {unique_prefix}INDEX {quote(str(spec['name']))} "
        f"ON {quote(str(spec['table']))} ({column_expr})"
    )
    if (
        spec.get("mysql_online") is True
        and engine.dialect.name in {"mysql", "mariadb"}
    ):
        statement += " ALGORITHM=INPLACE LOCK=NONE"
    return statement


def ensure_release_indexes(engine, index_specs: Iterable[Dict[str, Any]]) -> List[str]:
    inspector = inspect(engine)
    ensured: List[str] = []
    with engine.begin() as conn:
        for spec in index_specs:
            table_name = str(spec["table"])
            index_name = str(spec["name"])
            columns = tuple(spec["columns"])
            if table_name not in set(inspector.get_table_names()):
                continue
            existing_columns = {
                str(column["name"]) for column in inspector.get_columns(table_name)
            }
            if not set(columns).issubset(existing_columns):
                continue
            existing_index_names = {index["name"] for index in inspector.get_indexes(table_name)}
            existing_index_names.update(
                constraint["name"]
                for constraint in inspector.get_unique_constraints(table_name)
                if constraint.get("name")
            )
            if index_name in existing_index_names:
                continue
            conn.execute(text(_build_create_index_sql(engine, spec)))
            ensured.append(index_name)
            inspector = inspect(engine)
    return ensured


def _normalize_check_sql(value: Any) -> str:
    source = re.sub(
        r"\b_[a-z0-9_]+(?=\s*')",
        "",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    parts = re.split(r"('(?:''|[^'])*')", source)
    normalized = "".join(
        part
        if index % 2
        else _normalize_check_sql_code(part)
        for index, part in enumerate(parts)
    ).strip()
    return _canonicalize_check_sql_boolean(normalized)


def _normalize_check_sql_code(value: str) -> str:
    normalized = value.lower().replace("`", "").replace('"', "")
    normalized = re.sub(r"\boctet_length\s*(?=\()", "length", normalized)
    normalized = re.sub(
        r"\bconvert\s*\(\s*char\s*\(\s*(\d+)\s*\)\s+using\s+utf8mb4\s*\)",
        r"char(\1)",
        normalized,
    )
    return re.sub(r"\s+", " ", normalized)


def _strip_check_sql_outer_parentheses(value: str) -> str:
    normalized = value.strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        wraps_expression = True
        in_literal = False
        index = 0
        while index < len(normalized):
            character = normalized[index]
            if character == "'":
                if (
                    in_literal
                    and index + 1 < len(normalized)
                    and normalized[index + 1] == "'"
                ):
                    index += 2
                    continue
                in_literal = not in_literal
                index += 1
                continue
            if in_literal:
                index += 1
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(normalized) - 1:
                    wraps_expression = False
                    break
            index += 1
        if not wraps_expression:
            break
        normalized = normalized[1:-1].strip()
    return normalized


def _split_check_sql_boolean(value: str, operator: str) -> List[str]:
    parts: List[str] = []
    start = 0
    depth = 0
    in_literal = False
    index = 0
    operator_length = len(operator)
    while index < len(value):
        character = value[index]
        if character == "'":
            if (
                in_literal
                and index + 1 < len(value)
                and value[index + 1] == "'"
            ):
                index += 2
                continue
            in_literal = not in_literal
            index += 1
            continue
        if in_literal:
            index += 1
            continue
        if character == "(":
            depth += 1
            index += 1
            continue
        if character == ")":
            depth -= 1
            index += 1
            continue
        if depth == 0 and value[index:index + operator_length] == operator:
            previous = value[index - 1] if index else " "
            following_index = index + operator_length
            following = (
                value[following_index]
                if following_index < len(value)
                else " "
            )
            if (
                not (previous.isalnum() or previous == "_")
                and not (following.isalnum() or following == "_")
            ):
                parts.append(value[start:index])
                start = following_index
                index = following_index
                continue
        index += 1
    if not parts:
        return [value]
    parts.append(value[start:])
    return parts


def _canonicalize_check_sql_boolean(value: str) -> str:
    normalized = _strip_check_sql_outer_parentheses(value)
    for operator in ("or", "and"):
        parts = _split_check_sql_boolean(normalized, operator)
        if len(parts) > 1:
            return (
                operator
                + "("
                + ",".join(_canonicalize_check_sql_boolean(part) for part in parts)
                + ")"
            )
    literal_parts = re.split(r"('(?:''|[^'])*')", normalized)
    return "".join(
        part
        if index % 2
        else re.sub(r"\s+", "", part)
        for index, part in enumerate(literal_parts)
    )


def _mysql_check_constraint_sqltexts(
    engine,
    table_name: str,
    *,
    cache: Dict[str, Dict[str, str]] | None = None,
) -> Dict[str, str]:
    if engine.dialect.name not in {"mysql", "mariadb"}:
        return {}
    if cache is not None and table_name in cache:
        return cache[table_name]
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT tc.CONSTRAINT_NAME AS name, cc.CHECK_CLAUSE AS sqltext "
            "FROM information_schema.TABLE_CONSTRAINTS AS tc "
            "INNER JOIN information_schema.CHECK_CONSTRAINTS AS cc "
            "ON tc.CONSTRAINT_SCHEMA = cc.CONSTRAINT_SCHEMA "
            "AND tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME "
            "WHERE tc.CONSTRAINT_SCHEMA = DATABASE() "
            "AND tc.TABLE_NAME = :table_name "
            "AND tc.CONSTRAINT_TYPE = 'CHECK' "
            "ORDER BY tc.CONSTRAINT_NAME"
        ), {"table_name": table_name}).mappings().all()
    result: Dict[str, str] = {}
    for row in rows:
        name = row.get("name")
        sqltext = row.get("sqltext")
        if not isinstance(name, str) or not isinstance(sqltext, str) or not sqltext:
            continue
        if name in result:
            raise RuntimeError(
                "duplicate MySQL CHECK constraint metadata: " + name
            )
        result[name] = sqltext
    if cache is not None:
        cache[table_name] = result
    return result


def _mysql_show_create_check_sqltexts(
    engine,
    table_name: str,
    *,
    cache: Dict[str, Dict[str, str]] | None = None,
) -> Dict[str, str]:
    if engine.dialect.name not in {"mysql", "mariadb"}:
        return {}
    if cache is not None and table_name in cache:
        return cache[table_name]
    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            f"SHOW CREATE TABLE {quote(table_name)}"
        ).mappings().one()
    create_sql = next(
        (
            value
            for key, value in row.items()
            if re.sub(r"[\s_]+", "", str(key)).lower() == "createtable"
            and isinstance(value, str)
        ),
        None,
    )
    if not isinstance(create_sql, str):
        raise RuntimeError("MySQL SHOW CREATE TABLE metadata is invalid")
    pattern = re.compile(
        r"\bCONSTRAINT\s+(?P<name>`(?:``|[^`])+`|[A-Za-z_][A-Za-z0-9_$]*)"
        r"\s+CHECK\s*\(",
        re.IGNORECASE,
    )
    result: Dict[str, str] = {}
    for match in pattern.finditer(create_sql):
        raw_name = match.group("name")
        name = raw_name[1:-1].replace("``", "`") if raw_name.startswith("`") else raw_name
        clause = _extract_sql_parenthesized_expression(create_sql, match.end())
        if clause is None:
            raise RuntimeError("MySQL SHOW CREATE TABLE CHECK metadata is invalid")
        if name in result:
            raise RuntimeError("duplicate MySQL SHOW CREATE CHECK metadata: " + name)
        result[name] = clause
    if cache is not None:
        cache[table_name] = result
    return result


def _extract_sql_parenthesized_expression(value: str, start: int) -> str | None:
    depth = 1
    quote: str | None = None
    index = start
    while index < len(value):
        character = value[index]
        if quote is not None:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return value[start:index]
        index += 1
    return None


def _schema_check_matches(
    inspector,
    spec: Dict[str, Any],
    *,
    engine=None,
    mysql_check_cache: Dict[str, Dict[str, str]] | None = None,
    mysql_show_create_cache: Dict[str, Dict[str, str]] | None = None,
) -> bool:
    table_name = str(spec["table"])
    if table_name not in set(inspector.get_table_names()):
        return False
    expected_name = str(spec["name"])
    expected_sql = _normalize_check_sql(spec["sqltext"])
    if any(
        constraint.get("name") == expected_name
        and _normalize_check_sql(constraint.get("sqltext")) == expected_sql
        for constraint in inspector.get_check_constraints(table_name)
    ):
        return True
    if engine is None or engine.dialect.name not in {"mysql", "mariadb"}:
        return False
    actual_sql = _mysql_check_constraint_sqltexts(
        engine,
        table_name,
        cache=mysql_check_cache,
    ).get(expected_name)
    if (
        isinstance(actual_sql, str)
        and _normalize_check_sql(actual_sql) == expected_sql
    ):
        return True
    actual_sql = _mysql_show_create_check_sqltexts(
        engine,
        table_name,
        cache=mysql_show_create_cache,
    ).get(expected_name)
    return (
        isinstance(actual_sql, str)
        and _normalize_check_sql(actual_sql) == expected_sql
    )


_TXSQL_LEGACY_CHECK_REPLACEMENT_DIGESTS: Dict[tuple[str, str], frozenset[str]] = {
    (
        ActionAttempt.__tablename__,
        "ck_action_attempt_state_facts",
    ): frozenset({
        "03c47c4f681f66277101abe43aea7ec62911f36768757f1cb28cb8350e475ea3"
    }),
    (
        ActionAttempt.__tablename__,
        "ck_action_attempt_status",
    ): frozenset({
        "e01d2d728d450cf2dbde8a33edb141895833f7bc675b311d67fc542a781291bb"
    }),
    (
        ActionIntent.__tablename__,
        "ck_action_intent_status",
    ): frozenset({
        "da24a6538bc1dca0835795a18a2fd54e12cf57669c7f162dd59d5c942419993f"
    }),
    (
        ActionTerminalFact.__tablename__,
        "ck_action_terminal_fact_status_code",
    ): frozenset({
        "5f34c85b8c9a224e835d78c0fd802167ec717bcf89d7654d68f61120ef1a9d2e"
    }),
    (
        SchedulerJobRun.__tablename__,
        "ck_scheduler_job_run_state_facts",
    ): frozenset({
        "9385cd200e90f9d77fe1c38727e6e7bdbe0272d92e253f3b2f29d42d02f940dd"
    }),
    (
        SchedulerJobRun.__tablename__,
        "ck_scheduler_job_run_status",
    ): frozenset({
        "85e9cd9018313ebc656c6cd68bd81d5e225648da33e609596ce9b5c68344cd1a"
    }),
}


def _txsql_check_evidence_digest(value: str) -> str:
    """Keep the pre-remediation TXSQL evidence canonicalization stable."""
    parts = re.split(r"('(?:''|[^'])*')", str(value or ""))
    normalized = "".join(
        part
        if index % 2
        else re.sub(r"\b_utf8mb4\b", "", _normalize_check_sql_code(part))
        for index, part in enumerate(parts)
    ).strip()
    return hashlib.sha256(
        _canonicalize_check_sql_boolean(normalized).encode("utf-8")
    ).hexdigest()


def _is_exact_txsql_legacy_check_replacement(
    engine,
    spec: Dict[str, Any],
    actual_digest: str,
) -> bool:
    expected_digests = _TXSQL_LEGACY_CHECK_REPLACEMENT_DIGESTS.get(
        (str(spec["table"]), str(spec["name"]))
    )
    if (
        not expected_digests
        or not isinstance(actual_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", actual_digest) is None
        or engine.dialect.name not in {"mysql", "mariadb"}
    ):
        return False
    enforcement = inspect_database_check_enforcement(engine)
    return (
        enforcement.get("family") == "txsql"
        and enforcement.get("checkConstraintsEnforced") is True
        and actual_digest in expected_digests
    )


def _schema_check_is_known_legacy(inspector, spec: Dict[str, Any]) -> bool:
    table_name = str(spec["table"])
    constraint_name = str(spec["name"])
    allowed_sql = AIOPS131_LEGACY_CHECK_SQL.get(
        (table_name, constraint_name),
        (),
    )
    if not allowed_sql or table_name not in set(inspector.get_table_names()):
        return False
    normalized_allowed = {
        _normalize_check_sql(sqltext) for sqltext in allowed_sql
    }
    return any(
        constraint.get("name") == constraint_name
        and _normalize_check_sql(constraint.get("sqltext")) in normalized_allowed
        for constraint in inspector.get_check_constraints(table_name)
    )


def inspect_action_fact_constraint_violations(
    engine,
    check_specs: Iterable[Dict[str, Any]] = AIOPS131_CHECK_SPECS,
) -> List[Dict[str, Any]]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    quote = engine.dialect.identifier_preparer.quote
    violations: List[Dict[str, Any]] = []
    with engine.connect() as conn:
        for spec in check_specs:
            table_name = str(spec["table"])
            if table_name not in table_names:
                continue
            actual_columns = {
                str(column["name"])
                for column in inspector.get_columns(table_name)
            }
            if not set(spec.get("columns") or ()).issubset(actual_columns):
                continue
            count = int(conn.execute(text(
                f"SELECT COUNT(*) FROM {quote(table_name)} "
                f"WHERE NOT ({spec['sqltext']})"
            )).scalar_one())
            if count:
                violations.append({
                    "table": table_name,
                    "name": str(spec["name"]),
                    "count": count,
                })
    return violations


def _strict_json_loads(value: Any) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-standard JSON constant: {constant}")

    return json.loads(str(value), parse_constant=reject_constant)


def _fact_json_matches_hash(value: Any, expected_hash: Any) -> bool:
    if not isinstance(expected_hash, str):
        return False
    try:
        payload = _strict_json_loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if isinstance(payload, dict) and payload.get("_truncated") is True:
        return (
            isinstance(payload.get("preview"), str)
            and payload.get("sha256") == expected_hash
        )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest() == expected_hash


def inspect_action_fact_hash_violations(engine) -> List[Dict[str, Any]]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    # ``action_intent.payload_hash`` is the idempotency identity hash when an
    # intent supplies ``idempotency_payload``.  The complete auditable payload
    # is stored in ``payload_json`` and therefore cannot be reconstructed from
    # this column alone.  The fence/readback/terminal fact tables, in contrast,
    # persist their audit payload and fact hash as a matching pair.
    specs = (
        (ActionFenceEvidence.__tablename__, ("evidence_payload",), "fact_hash"),
        (ActionReadback.__tablename__, ("observed_payload",), "fact_hash"),
        (
            ActionTerminalFact.__tablename__,
            ("result_json", "replay_result_json"),
            "fact_hash",
        ),
    )
    violations: List[Dict[str, Any]] = []
    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as conn:
        for table_name, payload_columns, hash_column in specs:
            if table_name not in table_names:
                continue
            actual_columns = {
                str(column["name"])
                for column in inspector.get_columns(table_name)
            }
            required_columns = {*payload_columns, hash_column}
            if not required_columns.issubset(actual_columns):
                continue
            selected_columns = ", ".join(
                quote(column)
                for column in (*payload_columns, hash_column)
            )
            rows = conn.execute(text(
                f"SELECT {selected_columns} FROM {quote(table_name)}"
            )).all()
            count = sum(
                1
                for row in rows
                if not all(
                    _fact_json_matches_hash(row[index], row[-1])
                    for index in range(len(payload_columns))
                )
            )
            if count:
                violations.append({"table": table_name, "count": count})
    return violations


def _terminal_code_matches_status(status: Any, terminal_code: Any) -> bool:
    allowed = {
        "success": {"executor_succeeded", "authoritative_readback_success"},
        "failed": {"executor_failed", "authoritative_readback_failed"},
        "inconclusive": {"readback_unavailable", "readback_failed"},
    }
    return str(terminal_code) in allowed.get(str(status), set())


def inspect_action_terminal_state_violations(engine) -> List[Dict[str, Any]]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    required_tables = {
        ActionIntent.__tablename__,
        ActionAttempt.__tablename__,
        ActionTerminalFact.__tablename__,
    }
    if not required_tables.issubset(table_names):
        return []
    required_columns = {
        ActionIntent.__tablename__: {"id", "status"},
        ActionAttempt.__tablename__: {"intent_id", "attempt_no", "status"},
        ActionTerminalFact.__tablename__: {
            "intent_id",
            "status",
            "terminal_code",
        },
    }
    if any(
        not columns.issubset({
            str(column["name"])
            for column in inspector.get_columns(table_name)
        })
        for table_name, columns in required_columns.items()
    ):
        return []

    quote = engine.dialect.identifier_preparer.quote
    terminal_table = quote(ActionTerminalFact.__tablename__)
    intent_table = quote(ActionIntent.__tablename__)
    attempt_table = quote(ActionAttempt.__tablename__)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT terminal.intent_id, terminal.status AS terminal_status, "
            "terminal.terminal_code, intent.status AS intent_status, "
            "attempt.status AS attempt_status "
            f"FROM {terminal_table} AS terminal "
            f"LEFT JOIN {intent_table} AS intent ON intent.id = terminal.intent_id "
            f"LEFT JOIN {attempt_table} AS attempt "
            "ON attempt.intent_id = terminal.intent_id "
            "AND attempt.attempt_no = ("
            f"SELECT MAX(latest.attempt_no) FROM {attempt_table} AS latest "
            "WHERE latest.intent_id = terminal.intent_id) "
            "ORDER BY terminal.intent_id"
        )).mappings().all()

    violations: List[Dict[str, Any]] = []
    for row in rows:
        if not _terminal_code_matches_status(
            row["terminal_status"],
            row["terminal_code"],
        ):
            reason = "terminal_code"
        elif row["intent_status"] != row["terminal_status"]:
            reason = "intent"
        elif row["attempt_status"] != row["terminal_status"]:
            reason = "attempt"
        else:
            continue
        violations.append({
            "intent_id": int(row["intent_id"]),
            "reason": reason,
        })
    return violations


def _snapshot_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytesHex": value.hex()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def snapshot_action_fact_schema(engine) -> Dict[str, Any]:
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            "action fact schema snapshot unsupported for "
            + str(engine.dialect.name)
        )
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    quote = engine.dialect.identifier_preparer.quote
    snapshot: Dict[str, Any] = {
        "dialect": "sqlite",
        "foreignKeysEnabled": False,
        "tables": {},
    }
    with engine.connect() as conn:
        snapshot["foreignKeysEnabled"] = bool(
            conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
        )
        for model in AIOPS127_ACTION_FACT_MODELS:
            table_name = model.__tablename__
            if table_name not in existing_tables:
                continue
            columns = inspector.get_columns(table_name)
            column_names = [str(column["name"]) for column in columns]
            selected_columns = ", ".join(quote(name) for name in column_names)
            rows = conn.execute(text(
                f"SELECT {selected_columns} FROM {quote(table_name)} "
                f"ORDER BY {quote('id')}"
            )).all()
            row_payload = [
                [_snapshot_value(value) for value in row]
                for row in rows
            ]
            encoded_rows = json.dumps(
                row_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            create_sql = conn.execute(text("""
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = :table_name
            """), {"table_name": table_name}).scalar_one()
            index_rows = conn.execute(text(
                f"PRAGMA index_list({quote(table_name)})"
            )).mappings().all()
            indexes = []
            for index_row in index_rows:
                index_name = str(index_row["name"])
                index_columns = [
                    str(item["name"])
                    for item in conn.execute(text(
                        f"PRAGMA index_info({quote(index_name)})"
                    )).mappings().all()
                ]
                index_sql = conn.execute(text("""
                    SELECT sql FROM sqlite_master
                    WHERE type = 'index' AND name = :index_name
                """), {"index_name": index_name}).scalar_one_or_none()
                indexes.append({
                    "name": index_name,
                    "origin": str(index_row["origin"]),
                    "unique": bool(index_row["unique"]),
                    "partial": bool(index_row["partial"]),
                    "columns": index_columns,
                    "sql": index_sql,
                })
            triggers = [
                {"name": str(row["name"]), "sql": str(row["sql"])}
                for row in conn.execute(text("""
                    SELECT name, sql FROM sqlite_master
                    WHERE type = 'trigger' AND tbl_name = :table_name
                    ORDER BY name
                """), {"table_name": table_name}).mappings().all()
            ]
            foreign_keys = sorted(
                (
                    {
                        "name": foreign_key.get("name"),
                        "columns": list(
                            foreign_key.get("constrained_columns") or ()
                        ),
                        "referredTable": foreign_key.get("referred_table"),
                        "referredColumns": list(
                            foreign_key.get("referred_columns") or ()
                        ),
                        "options": dict(foreign_key.get("options") or {}),
                    }
                    for foreign_key in inspector.get_foreign_keys(table_name)
                ),
                key=lambda item: (
                    str(item["name"] or ""),
                    tuple(item["columns"]),
                ),
            )
            checks = sorted(
                (
                    {
                        "name": constraint.get("name"),
                        "sql": str(constraint.get("sqltext") or ""),
                    }
                    for constraint in inspector.get_check_constraints(table_name)
                ),
                key=lambda item: (
                    str(item["name"] or ""),
                    item["sql"],
                ),
            )
            snapshot["tables"][table_name] = {
                "createSql": str(create_sql),
                "columns": [
                    {
                        "name": str(column["name"]),
                        "type": str(column["type"]),
                        "nullable": bool(column.get("nullable")),
                        "default": (
                            None
                            if column.get("default") is None
                            else str(column.get("default"))
                        ),
                        "primaryKey": int(column.get("primary_key") or 0),
                    }
                    for column in columns
                ],
                "rowCount": len(row_payload),
                "dataHash": hashlib.sha256(
                    encoded_rows.encode("utf-8")
                ).hexdigest(),
                "indexes": sorted(indexes, key=lambda item: item["name"]),
                "triggers": triggers,
                "foreignKeys": foreign_keys,
                "checks": checks,
            }
    return snapshot


def inspect_action_attempt_external_request_id_backfill(engine) -> Dict[str, int]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if not {ActionIntent.__tablename__, ActionAttempt.__tablename__}.issubset(
        table_names
    ):
        return {"backfillable": 0, "unresolved": 0}

    required_columns = {
        ActionIntent.__tablename__: {"id", "idempotency_key"},
        ActionAttempt.__tablename__: {"intent_id", "external_request_id"},
    }
    for table_name, columns in required_columns.items():
        actual_columns = {
            str(column["name"])
            for column in inspector.get_columns(table_name)
        }
        if not columns.issubset(actual_columns):
            return {"backfillable": 0, "unresolved": 0}

    quote = engine.dialect.identifier_preparer.quote
    attempt_table = quote(ActionAttempt.__tablename__)
    intent_table = quote(ActionIntent.__tablename__)
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT "
            "SUM(CASE WHEN intent.id IS NOT NULL "
            "AND intent.idempotency_key IS NOT NULL "
            "AND length(trim(intent.idempotency_key)) > 0 THEN 1 ELSE 0 END) "
            "AS backfillable, "
            "SUM(CASE WHEN intent.id IS NULL "
            "OR intent.idempotency_key IS NULL "
            "OR length(trim(intent.idempotency_key)) = 0 THEN 1 ELSE 0 END) "
            "AS unresolved "
            f"FROM {attempt_table} AS attempt "
            f"LEFT JOIN {intent_table} AS intent ON intent.id = attempt.intent_id "
            "WHERE attempt.external_request_id IS NULL "
            "OR length(trim(attempt.external_request_id)) = 0"
        )).mappings().one()
    return {
        "backfillable": int(result["backfillable"] or 0),
        "unresolved": int(result["unresolved"] or 0),
    }


def ensure_action_attempt_external_request_ids(engine) -> int:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if ActionAttempt.__tablename__ not in table_names:
        return 0
    actual_column = next(
        (
            column
            for column in inspector.get_columns(ActionAttempt.__tablename__)
            if str(column["name"]) == "external_request_id"
        ),
        None,
    )
    if actual_column is None:
        return 0

    plan = inspect_action_attempt_external_request_id_backfill(engine)
    if plan["unresolved"]:
        raise RuntimeError(
            "action_attempt external_request_id backfill unresolved: "
            f"{plan['unresolved']} row(s)"
        )

    quote = engine.dialect.identifier_preparer.quote
    if plan["backfillable"]:
        with engine.begin() as conn:
            result = conn.execute(text(
                f"UPDATE {quote(ActionAttempt.__tablename__)} "
                "SET external_request_id = ("
                f"SELECT intent.idempotency_key FROM {quote(ActionIntent.__tablename__)} "
                "AS intent WHERE intent.id = action_attempt.intent_id"
                ") WHERE external_request_id IS NULL "
                "OR length(trim(external_request_id)) = 0"
            ))
            if result.rowcount not in {-1, plan["backfillable"]}:
                raise RuntimeError(
                    "action_attempt external_request_id backfill count mismatch: "
                    f"expected {plan['backfillable']}, updated {result.rowcount}"
                )

    return plan["backfillable"]


def ensure_action_attempt_external_request_id_not_null(engine) -> None:
    inspector = inspect(engine)
    if ActionAttempt.__tablename__ not in set(inspector.get_table_names()):
        return
    actual_column = next(
        (
            column
            for column in inspector.get_columns(ActionAttempt.__tablename__)
            if str(column["name"]) == "external_request_id"
        ),
        None,
    )
    if actual_column is None or not bool(actual_column.get("nullable")):
        return

    quote = engine.dialect.identifier_preparer.quote
    if engine.dialect.name == "sqlite":
        _rebuild_sqlite_action_fact_tables(
            engine,
            {ActionAttempt.__tablename__},
        )
    elif engine.dialect.name in {"mysql", "mariadb"}:
        referencing_foreign_keys = sorted(
            str(foreign_key.get("name") or f"<unnamed on {table_name}>")
            for table_name in inspector.get_table_names()
            for foreign_key in inspector.get_foreign_keys(table_name)
            if str(foreign_key.get("referred_table"))
            == ActionAttempt.__tablename__
            and "external_request_id"
            in tuple(foreign_key.get("referred_columns") or ())
        )
        if referencing_foreign_keys:
            raise RuntimeError(
                "manual recovery required for "
                "action_attempt.external_request_id: nullable column is "
                "already referenced by foreign key(s): "
                + ", ".join(referencing_foreign_keys)
            )
        definition = ActionAttempt.__table__.c.external_request_id
        compiled_type = str(definition.type.compile(dialect=engine.dialect))
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {quote(ActionAttempt.__tablename__)} "
                f"MODIFY COLUMN {quote('external_request_id')} "
                f"{compiled_type} NOT NULL"
            ))
    else:
        raise RuntimeError(
            "action_attempt external_request_id migration unsupported for "
            + str(engine.dialect.name)
        )


def _build_add_check_constraint_sql(engine, spec: Dict[str, Any]) -> str:
    quote = engine.dialect.identifier_preparer.quote
    return (
        f"ALTER TABLE {quote(str(spec['table']))} "
        f"ADD CONSTRAINT {quote(str(spec['name']))} "
        f"CHECK ({spec['sqltext']})"
    )


def _build_replace_check_constraints_sql(
    engine,
    specs: Sequence[Dict[str, Any]],
) -> str:
    if not specs:
        raise ValueError("check constraint replacement requires specs")
    table_names = {str(spec["table"]) for spec in specs}
    names = [str(spec["name"]) for spec in specs]
    if len(table_names) != 1 or len(names) != len(set(names)):
        raise ValueError("check constraint replacement identity is invalid")
    quote = engine.dialect.identifier_preparer.quote
    clauses: List[str] = []
    for spec in sorted(specs, key=lambda item: str(item["name"])):
        name = quote(str(spec["name"]))
        clauses.extend((
            f"DROP CHECK {name}",
            f"ADD CONSTRAINT {name} CHECK ({spec['sqltext']})",
        ))
    return f"ALTER TABLE {quote(next(iter(table_names)))} " + ", ".join(clauses)


def ensure_action_fact_check_constraints(engine) -> List[str]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    mysql_check_cache: Dict[str, Dict[str, str]] | None = (
        {} if engine.dialect.name in {"mysql", "mariadb"} else None
    )
    mysql_show_create_cache: Dict[str, Dict[str, str]] | None = (
        {} if engine.dialect.name in {"mysql", "mariadb"} else None
    )
    missing: List[Dict[str, Any]] = []
    replacements: List[Dict[str, Any]] = []
    for spec in RELEASE_CHECK_SPECS:
        table_name = str(spec["table"])
        if table_name not in table_names:
            continue
        same_name = next(
            (
                constraint
                for constraint in inspector.get_check_constraints(table_name)
                if constraint.get("name") == spec["name"]
            ),
            None,
        )
        if same_name is None:
            missing.append(spec)
            continue
        if not _schema_check_matches(
            inspector,
            spec,
            engine=engine,
            mysql_check_cache=mysql_check_cache,
            mysql_show_create_cache=mysql_show_create_cache,
        ):
            if _schema_check_is_known_legacy(inspector, spec):
                missing.append(spec)
                continue
            actual_sql = _mysql_show_create_check_sqltexts(
                engine,
                table_name,
                cache=mysql_show_create_cache,
            ).get(str(spec["name"]))
            if (
                isinstance(actual_sql, str)
                and _is_exact_txsql_legacy_check_replacement(
                    engine,
                    spec,
                    _txsql_check_evidence_digest(actual_sql),
                )
            ):
                replacements.append(spec)
                continue
            raise RuntimeError(
                "check constraint mismatch: "
                f"{spec['name']} on {table_name}"
            )

    changed_specs = [*missing, *replacements]
    if not changed_specs:
        return []
    violations = inspect_action_fact_constraint_violations(engine, changed_specs)
    if violations:
        detail = "; ".join(
            f"{item['name']} on {item['table']} has {item['count']} row(s)"
            for item in violations
        )
        raise RuntimeError(f"action fact constraint violations: {detail}")

    if engine.dialect.name == "sqlite":
        _rebuild_sqlite_action_fact_tables(
            engine,
            {str(spec["table"]) for spec in missing},
        )
    elif engine.dialect.name in {"mysql", "mariadb"}:
        with engine.begin() as conn:
            replacements_by_table: Dict[str, List[Dict[str, Any]]] = {}
            for spec in replacements:
                replacements_by_table.setdefault(str(spec["table"]), []).append(spec)
            for table_name in sorted(replacements_by_table):
                conn.execute(text(
                    _build_replace_check_constraints_sql(
                        engine,
                        replacements_by_table[table_name],
                    )
                ))
            for spec in missing:
                conn.execute(text(_build_add_check_constraint_sql(engine, spec)))
        post_inspector = inspect(engine)
        post_mysql_check_cache: Dict[str, Dict[str, str]] = {}
        post_mysql_show_create_cache: Dict[str, Dict[str, str]] = {}
        unresolved = [
            spec
            for spec in replacements
            if not _schema_check_matches(
                post_inspector,
                spec,
                engine=engine,
                mysql_check_cache=post_mysql_check_cache,
                mysql_show_create_cache=post_mysql_show_create_cache,
            )
        ]
        if unresolved:
            raise RuntimeError(
                "TXSQL check constraint replacement readback failed: "
                + ", ".join(
                    f"{spec['name']} on {spec['table']}"
                    for spec in unresolved
                )
            )
    else:
        raise RuntimeError(
            "action fact check migration unsupported for "
            + str(engine.dialect.name)
        )
    return [str(spec["name"]) for spec in changed_specs]


def _rebuild_sqlite_action_fact_tables(engine, table_names: set[str]) -> None:
    models = [
        model
        for model in (
            *AIOPS127_ACTION_FACT_MODELS,
            *AIOPS130B_RUN_MODELS,
            *AIOPS135_NODE_CONFIG_MODELS,
        )
        if model.__tablename__ in table_names
    ]
    if not models:
        return
    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as conn:
        foreign_keys_enabled = bool(conn.exec_driver_sql("PRAGMA foreign_keys").scalar())
        conn.rollback()
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                for model in models:
                    table_name = model.__tablename__
                    temp_table_name = f"__aiops131_{table_name}_new"
                    existing_temp = conn.execute(text("""
                        SELECT 1 FROM sqlite_master
                        WHERE type = 'table' AND name = :table_name
                    """), {"table_name": temp_table_name}).first()
                    if existing_temp is not None:
                        raise RuntimeError(
                            "manual recovery required for AIOPS-131 temp table: "
                            + temp_table_name
                        )
                    index_statements = conn.execute(text("""
                        SELECT sql FROM sqlite_master
                        WHERE type = 'index'
                          AND tbl_name = :table_name
                          AND sql IS NOT NULL
                        ORDER BY name
                    """), {"table_name": table_name}).scalars().all()
                    trigger_statements = conn.execute(text("""
                        SELECT sql FROM sqlite_master
                        WHERE type = 'trigger'
                          AND tbl_name = :table_name
                          AND sql IS NOT NULL
                        ORDER BY name
                    """), {"table_name": table_name}).scalars().all()
                    metadata = MetaData()
                    for action_model in (
                        *AIOPS127_ACTION_FACT_MODELS,
                        *AIOPS130B_RUN_MODELS,
                        *AIOPS135_NODE_CONFIG_MODELS,
                    ):
                        action_model.__table__.to_metadata(metadata)
                    temp_table = model.__table__.to_metadata(
                        metadata,
                        name=temp_table_name,
                    )
                    conn.execute(CreateTable(temp_table))
                    column_expr = ", ".join(
                        quote(column.name) for column in model.__table__.columns
                    )
                    conn.execute(text(
                        f"INSERT INTO {quote(temp_table_name)} ({column_expr}) "
                        f"SELECT {column_expr} FROM {quote(table_name)}"
                    ))
                    conn.execute(text(f"DROP TABLE {quote(table_name)}"))
                    conn.execute(text(
                        f"ALTER TABLE {quote(temp_table_name)} "
                        f"RENAME TO {quote(table_name)}"
                    ))
                    for statement in (*index_statements, *trigger_statements):
                        conn.execute(text(str(statement)))
                violations = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
                if violations:
                    raise RuntimeError(
                        "action fact sqlite rebuild foreign key violations: "
                        + json.dumps(
                            [list(row) for row in violations],
                            ensure_ascii=False,
                        )
                    )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.exec_driver_sql(
                "PRAGMA foreign_keys=" + ("ON" if foreign_keys_enabled else "OFF")
            )
            conn.commit()


def inspect_unique_duplicates(
    engine,
    unique_specs: Iterable[Dict[str, Any]] = RELEASE_UNIQUE_SPECS,
) -> List[Dict[str, Any]]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    duplicates: List[Dict[str, Any]] = []
    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as conn:
        for spec in unique_specs:
            table_name = str(spec["table"])
            columns = tuple(str(column) for column in spec["columns"])
            if table_name not in table_names:
                continue
            existing_columns = {
                str(column["name"]) for column in inspector.get_columns(table_name)
            }
            if not set(columns).issubset(existing_columns):
                continue
            quoted_columns = ", ".join(quote(column) for column in columns)
            non_null_filter = " AND ".join(
                f"{quote(column)} IS NOT NULL" for column in columns
            )
            grouped_duplicates = (
                f"SELECT COUNT(*) AS duplicate_count FROM {quote(table_name)} "
                f"WHERE {non_null_filter} GROUP BY {quoted_columns} HAVING COUNT(*) > 1"
            )
            summary = conn.execute(text(
                "SELECT COUNT(*) AS duplicate_groups, "
                "MAX(duplicate_count) AS max_duplicate_count "
                f"FROM ({grouped_duplicates}) AS duplicate_keys"
            )).mappings().one()
            duplicate_groups = int(summary["duplicate_groups"] or 0)
            if duplicate_groups == 0:
                continue
            duplicates.append({
                "table": table_name,
                "name": str(spec["name"]),
                "columns": columns,
                "groups": duplicate_groups,
                "count": int(summary["max_duplicate_count"] or 0),
            })
    return duplicates


def inspect_foreign_key_data_violations(
    engine,
    foreign_key_specs: Iterable[Dict[str, Any]] = RELEASE_FOREIGN_KEY_SPECS,
) -> List[Dict[str, Any]]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    quote = engine.dialect.identifier_preparer.quote
    violations: List[Dict[str, Any]] = []
    with engine.connect() as conn:
        for spec in foreign_key_specs:
            table_name = str(spec["table"])
            referred_table = str(spec["referred_table"])
            columns = tuple(str(column) for column in spec["columns"])
            referred_columns = tuple(
                str(column) for column in spec["referred_columns"]
            )
            if table_name not in table_names or referred_table not in table_names:
                continue
            child_columns = {
                str(column["name"])
                for column in inspector.get_columns(table_name)
            }
            parent_columns = {
                str(column["name"])
                for column in inspector.get_columns(referred_table)
            }
            if not set(columns).issubset(child_columns) or not set(
                referred_columns
            ).issubset(parent_columns):
                continue
            join_condition = " AND ".join(
                f"child.{quote(column)} = parent.{quote(referred_column)}"
                for column, referred_column in zip(columns, referred_columns)
            )
            non_null_condition = " AND ".join(
                f"child.{quote(column)} IS NOT NULL" for column in columns
            )
            missing_parent_condition = " AND ".join(
                f"parent.{quote(column)} IS NULL" for column in referred_columns
            )
            count = int(conn.execute(text(
                f"SELECT COUNT(*) FROM {quote(table_name)} AS child "
                f"LEFT JOIN {quote(referred_table)} AS parent ON {join_condition} "
                f"WHERE {non_null_condition} AND {missing_parent_condition}"
            )).scalar_one())
            if count:
                violations.append({
                    "table": table_name,
                    "name": str(spec["name"]),
                    "count": count,
                })
    return violations


def mysql_check_enforcement_contract(
    version: Any,
    version_comment: Any,
) -> Dict[str, Any]:
    version_text = str(version or "").strip()
    comment_text = str(version_comment or "").strip()
    combined = f"{version_text} {comment_text}".lower()
    txsql_match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)-txsql", version_text, re.IGNORECASE
    )
    if txsql_match is not None:
        parsed = tuple(int(part) for part in txsql_match.groups())
        family = "txsql"
        enforced = parsed >= (8, 0, 16)
    elif "mariadb" in combined:
        matches = re.findall(r"(\d+)\.(\d+)\.(\d+)", version_text)
        if not matches:
            raise RuntimeError("database CHECK enforcement unknown: invalid MariaDB version")
        parsed = tuple(int(part) for part in matches[-1])
        family = "mariadb"
        enforced = parsed >= (10, 2, 1)
    elif re.search(
        r"\bmysql\s+(?:community|enterprise|server)",
        comment_text,
        re.IGNORECASE,
    ):
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version_text)
        if match is None:
            raise RuntimeError("database CHECK enforcement unknown: invalid MySQL version")
        parsed = tuple(int(part) for part in match.groups())
        family = "mysql"
        enforced = parsed >= (8, 0, 16)
    else:
        raise RuntimeError("database CHECK enforcement unknown: unsupported server family")
    return {
        "family": family,
        "version": version_text,
        "checkConstraintsEnforced": enforced,
    }


def inspect_database_check_enforcement(engine) -> Dict[str, Any]:
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            version = str(conn.exec_driver_sql("SELECT sqlite_version()").scalar())
            return {
                "family": "sqlite",
                "version": version,
                "checkConstraintsEnforced": True,
            }
        if engine.dialect.name in {"mysql", "mariadb"}:
            row = conn.execute(text(
                "SELECT VERSION() AS version, "
                "@@version_comment AS version_comment"
            )).mappings().one()
            return mysql_check_enforcement_contract(
                row["version"],
                row["version_comment"],
            )
    raise RuntimeError(
        "database CHECK enforcement unknown: unsupported dialect "
        + str(engine.dialect.name)
    )


def _release_table_charset_matches(engine, table_name: str, spec: Mapping[str, str]) -> bool:
    if engine.dialect.name not in {"mysql", "mariadb"}:
        return True
    with engine.connect() as conn:
        table_collation = conn.execute(
            text(
                "SELECT TABLE_COLLATION FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name"
            ),
            {"table_name": table_name},
        ).scalar_one_or_none()
        payload_row = conn.execute(
            text(
                "SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name "
                "AND COLUMN_NAME = 'payload'"
            ),
            {"table_name": table_name},
        ).one_or_none()
    return (
        str(table_collation or "").lower() == spec["collation"].lower()
        and payload_row is not None
        and str(payload_row[0] or "").lower() == spec["charset"].lower()
        and str(payload_row[1] or "").lower() == spec["collation"].lower()
    )


def verify_release_schema(engine) -> Dict[str, Any]:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    table_checks = {
        table_name: table_name in table_names
        for table_name in RELEASE_VERIFICATION_TABLES
    }
    table_charset_checks = {
        table_name: table_name in table_names and _release_table_charset_matches(
            engine, table_name, spec
        )
        for table_name, spec in RELEASE_TABLE_CHARSET_SPECS.items()
    }
    primary_key_checks = {
        table_name: table_name in table_names and _primary_key_identity_matches(
            inspector,
            table_name,
            model,
            engine.dialect,
        )
        for table_name, model in RELEASE_IDENTITY_TABLE_MODELS.items()
    }
    column_checks = {}
    for table_name, columns in RELEASE_VERIFICATION_SCHEMA_COLUMNS.items():
        actual_columns = {
            str(column["name"]): column
            for column in inspector.get_columns(table_name)
        } if table_name in table_names else {}
        column_checks[table_name] = {
            column_name: _release_column_contract_check(
                engine,
                table_name,
                column_name,
                actual_columns.get(column_name),
                definition,
            )
            for column_name, definition in columns
        }
    mysql_check_cache: Dict[str, Dict[str, str]] | None = (
        {} if engine.dialect.name in {"mysql", "mariadb"} else None
    )
    mysql_show_create_cache: Dict[str, Dict[str, str]] | None = (
        {} if engine.dialect.name in {"mysql", "mariadb"} else None
    )
    unique_checks = {
        str(spec["name"]): _schema_index_matches(inspector, spec, require_unique=True)
        for spec in RELEASE_UNIQUE_SPECS
    }
    index_checks = {
        str(spec["name"]): _schema_index_matches(inspector, spec, require_unique=False)
        for spec in RELEASE_VERIFICATION_INDEX_SPECS
    }
    foreign_key_checks = {
        str(spec["name"]): _schema_foreign_key_matches(inspector, spec)
        for spec in RELEASE_FOREIGN_KEY_SPECS
    }
    check_constraint_checks = {
        str(spec["name"]): _schema_check_matches(
            inspector,
            spec,
            engine=engine,
            mysql_check_cache=mysql_check_cache,
            mysql_show_create_cache=mysql_show_create_cache,
        )
        for spec in RELEASE_CHECK_SPECS
    }
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            foreign_key_enforcement = bool(
                conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
            )
        elif engine.dialect.name in {"mysql", "mariadb"}:
            foreign_key_enforcement = bool(
                conn.execute(text("SELECT @@FOREIGN_KEY_CHECKS")).scalar()
            )
        else:
            foreign_key_enforcement = False
    foreign_key_violations = inspect_foreign_key_data_violations(engine)
    fact_hash_violations = inspect_action_fact_hash_violations(engine)
    terminal_state_violations = inspect_action_terminal_state_violations(engine)
    check_enforcement = inspect_database_check_enforcement(engine)
    ledger_table = TopQueryBudgetLedger.__tablename__
    ledger_foreign_keys_exact = (
        ledger_table in table_names
        and _ledger_foreign_keys_are_exact(
            inspector.get_foreign_keys(ledger_table)
        )
    )
    ok = (
        all(table_checks.values())
        and all(table_charset_checks.values())
        and all(primary_key_checks.values())
        and all(
            result["ok"]
            for checks in column_checks.values()
            for result in checks.values()
        )
        and all(unique_checks.values())
        and all(index_checks.values())
        and all(foreign_key_checks.values())
        and foreign_key_enforcement
        and not foreign_key_violations
        and not fact_hash_violations
        and not terminal_state_violations
        and check_enforcement["checkConstraintsEnforced"] is True
        and all(check_constraint_checks.values())
        and ledger_foreign_keys_exact
    )
    return {
        "ok": ok,
        "tables": table_checks,
        "tableCharsets": table_charset_checks,
        "primaryKeys": primary_key_checks,
        "columns": column_checks,
        "uniqueConstraints": unique_checks,
        "indexes": index_checks,
        "foreignKeys": foreign_key_checks,
        "foreignKeyEnforcement": foreign_key_enforcement,
        "foreignKeyViolations": foreign_key_violations,
        "factHashViolations": fact_hash_violations,
        "terminalStateViolations": terminal_state_violations,
        "checkConstraintEnforcement": check_enforcement,
        "checkConstraints": check_constraint_checks,
    }


def validate_release_schema_verification(verification: Any) -> Dict[str, Any]:
    top_level_keys = {
        "ok",
        "tables",
        "tableCharsets",
        "primaryKeys",
        "columns",
        "uniqueConstraints",
        "indexes",
        "foreignKeys",
        "foreignKeyEnforcement",
        "foreignKeyViolations",
        "factHashViolations",
        "terminalStateViolations",
        "checkConstraintEnforcement",
        "checkConstraints",
    }
    expected_boolean_keys = {
        "tables": set(RELEASE_VERIFICATION_TABLES),
        "tableCharsets": set(RELEASE_TABLE_CHARSET_SPECS),
        "primaryKeys": set(RELEASE_IDENTITY_TABLE_MODELS),
        "uniqueConstraints": {
            str(spec["name"]) for spec in RELEASE_UNIQUE_SPECS
        },
        "indexes": {
            str(spec["name"]) for spec in RELEASE_VERIFICATION_INDEX_SPECS
        },
        "foreignKeys": {
            str(spec["name"]) for spec in RELEASE_FOREIGN_KEY_SPECS
        },
        "checkConstraints": {
            str(spec["name"]) for spec in RELEASE_CHECK_SPECS
        },
    }
    column_check_keys = {"present", "type", "nullable", "default", "ok"}
    valid = isinstance(verification, dict) and set(verification) == top_level_keys
    valid = valid and verification.get("ok") is True
    valid = valid and verification.get("foreignKeyEnforcement") is True
    valid = valid and verification.get("foreignKeyViolations") == []
    valid = valid and verification.get("factHashViolations") == []
    valid = valid and verification.get("terminalStateViolations") == []
    check_enforcement = verification.get("checkConstraintEnforcement")
    valid = (
        valid
        and isinstance(check_enforcement, dict)
        and set(check_enforcement)
        == {"family", "version", "checkConstraintsEnforced"}
        and bool(check_enforcement.get("family"))
        and bool(check_enforcement.get("version"))
        and check_enforcement.get("checkConstraintsEnforced") is True
    )

    for category, expected_keys in expected_boolean_keys.items():
        checks = verification.get(category) if isinstance(verification, dict) else None
        valid = (
            valid
            and isinstance(checks, dict)
            and bool(expected_keys)
            and set(checks) == expected_keys
            and all(value is True for value in checks.values())
        )

    columns = verification.get("columns") if isinstance(verification, dict) else None
    expected_column_tables = set(RELEASE_VERIFICATION_SCHEMA_COLUMNS)
    valid = (
        valid
        and isinstance(columns, dict)
        and bool(expected_column_tables)
        and set(columns) == expected_column_tables
    )
    if isinstance(columns, dict):
        for table_name, column_specs in RELEASE_VERIFICATION_SCHEMA_COLUMNS.items():
            table_checks = columns.get(table_name)
            expected_columns = {column_name for column_name, _definition in column_specs}
            valid = (
                valid
                and isinstance(table_checks, dict)
                and bool(expected_columns)
                and set(table_checks) == expected_columns
            )
            if isinstance(table_checks, dict):
                for result in table_checks.values():
                    valid = (
                        valid
                        and isinstance(result, dict)
                        and set(result) == column_check_keys
                        and all(value is True for value in result.values())
                    )

    if not valid:
        raise RuntimeError("release schema verification result invalid") from None
    return verification


def _raise_on_release_schema_preflight_blockers(engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    blockers: List[str] = []
    reserved_temp_tables = sorted(
        table_name
        for table_name in table_names
        if table_name.startswith("__aiops131_")
    )
    if reserved_temp_tables:
        blockers.append(
            "manual recovery required for AIOPS-131 temp table: "
            + ", ".join(reserved_temp_tables)
            + "; preserve and inspect source/temp data before explicit cleanup"
        )
    request_id_backfill = inspect_action_attempt_external_request_id_backfill(
        engine
    )
    if engine.dialect.name in {"mysql", "mariadb"}:
        check_enforcement = inspect_database_check_enforcement(engine)
        if check_enforcement["checkConstraintsEnforced"] is not True:
            blockers.append(
                "database CHECK constraints are not enforced: "
                f"{check_enforcement['family']} {check_enforcement['version']}"
            )

    ledger_table = TopQueryBudgetLedger.__tablename__
    blockers.extend(_existing_release_column_contract_blockers(
        inspector,
        table_names,
        engine.dialect,
    ))
    blockers.extend(_action_plan_core_contract_blockers(
        inspector,
        table_names,
        engine.dialect,
    ))
    for table_name, columns in AIOPS127_SCHEMA_COLUMNS.items():
        if table_name not in table_names:
            continue
        actual_columns = {
            str(column["name"]): column
            for column in inspector.get_columns(table_name)
        }
        if (
            engine.dialect.name == "sqlite"
            and table_name in {
                model.__tablename__ for model in AIOPS127_ACTION_FACT_MODELS
            }
        ):
            expected_columns = {column_name for column_name, _definition in columns}
            blockers.extend(
                f"unexpected column: {table_name}.{column_name}"
                for column_name in sorted(set(actual_columns) - expected_columns)
            )
        for column_name, definition in columns:
            if column_name not in actual_columns:
                continue
            result = _release_column_contract_check(
                engine,
                table_name,
                column_name,
                actual_columns[column_name],
                definition,
            )
            allowed_request_id_upgrade = (
                table_name == ActionAttempt.__tablename__
                and column_name == "external_request_id"
                and engine.dialect.name in {"sqlite", "mysql", "mariadb"}
                and result["present"]
                and result["type"]
                and result["default"]
                and not result["nullable"]
                and bool(actual_columns[column_name].get("nullable"))
                and request_id_backfill["unresolved"] == 0
            )
            if not result["ok"] and not allowed_request_id_upgrade:
                blockers.append(f"column:{table_name}.{column_name}")

    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            for model in AIOPS127_ACTION_FACT_MODELS:
                table_name = model.__tablename__
                if table_name not in table_names:
                    continue
                allowed_autoindexes = {
                    (
                        "pk",
                        tuple(
                            column.name
                            for column in model.__table__.primary_key.columns
                        ),
                    ): 1,
                }
                for constraint in model.__table__.constraints:
                    if not isinstance(constraint, UniqueConstraint):
                        continue
                    signature = (
                        "u",
                        tuple(column.name for column in constraint.columns),
                    )
                    allowed_autoindexes[signature] = (
                        allowed_autoindexes.get(signature, 0) + 1
                    )
                blockers.extend(_sqlite_unreplayable_index_blockers(
                    conn,
                    engine,
                    table_name,
                    model_columns={
                        column.name for column in model.__table__.columns
                    },
                    allowed_autoindexes=allowed_autoindexes,
                    blocker_prefix="action fact sqlite rebuild",
                ))

    for table_name, columns in ZHA26_SCHEMA_COLUMNS.items():
        if table_name not in table_names:
            continue
        actual_columns = {
            str(column["name"]): column
            for column in inspector.get_columns(table_name)
        }
        for column_name, definition in columns:
            if column_name not in actual_columns:
                continue
            result = _column_contract_check(
                actual_columns[column_name],
                definition,
                engine.dialect,
            )
            if not result["ok"] and not _is_allowed_release_column_type_upgrade(
                table_name,
                column_name,
                actual_columns[column_name],
                definition,
                engine.dialect,
            ):
                blockers.append(f"column:{table_name}.{column_name}")

    for table_name in ZHA26_REQUIRED_TABLES:
        if table_name not in table_names:
            continue
        primary_key = tuple(
            str(column)
            for column in (
                inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
            )
        )
        if primary_key != ("id",):
            blockers.append(
                f"primary key mismatch: {table_name} expected (id)"
            )

    blockers.extend(_existing_schema_asset_contract_blockers(
        inspector,
        table_names,
        engine=engine,
        unique_specs=RELEASE_UNIQUE_SPECS,
        index_specs=(
            *RELEASE_INDEX_SPECS,
            *ZHA26_QUERY_INDEX_SPECS,
            *AIOPS127_INDEX_SPECS,
            *AIOPS130B_INDEX_SPECS,
            *QUERY_GOVERNANCE_INDEX_SPECS,
        ),
        foreign_key_specs=RELEASE_FOREIGN_KEY_SPECS,
    ))
    mysql_check_cache: Dict[str, Dict[str, str]] | None = (
        {} if engine.dialect.name in {"mysql", "mariadb"} else None
    )
    mysql_show_create_cache: Dict[str, Dict[str, str]] | None = (
        {} if engine.dialect.name in {"mysql", "mariadb"} else None
    )
    for spec in RELEASE_CHECK_SPECS:
        table_name = str(spec["table"])
        if table_name not in table_names:
            continue
        same_name = next(
            (
                constraint
                for constraint in inspector.get_check_constraints(table_name)
                if constraint.get("name") == spec["name"]
            ),
            None,
        )
        if (
            same_name is not None
            and not _schema_check_matches(
                inspector,
                spec,
                engine=engine,
                mysql_check_cache=mysql_check_cache,
                mysql_show_create_cache=mysql_show_create_cache,
            )
            and not _schema_check_is_known_legacy(inspector, spec)
        ):
            blockers.append(
                f"check constraint mismatch: {spec['name']} on {table_name}"
            )

    sqlite_ledger_needs_rebuild = False
    if engine.dialect.name == "sqlite" and ledger_table in table_names:
        ledger_foreign_keys = inspector.get_foreign_keys(ledger_table)
        ledger_specs = tuple(
            spec
            for spec in ZHA26_FOREIGN_KEY_SPECS
            if str(spec["table"]) == ledger_table
        )
        sqlite_ledger_needs_rebuild = any(
            not any(
                _foreign_key_matches(foreign_key, spec)
                for foreign_key in ledger_foreign_keys
            )
            for spec in ledger_specs
        )

    blockers.extend(_unsafe_missing_required_columns(engine, inspector, table_names))

    if (
        engine.dialect.name == "sqlite"
        and ledger_table in table_names
        and sqlite_ledger_needs_rebuild
    ):
        blockers.extend(_sqlite_ledger_rebuild_blockers(engine, inspector))

    duplicates = inspect_unique_duplicates(engine)
    if duplicates:
        detail = "; ".join(
            f"{item['name']} on {item['table']} ({', '.join(item['columns'])}) "
            f"has {item['groups']} duplicate group(s), max {item['count']} rows"
            for item in duplicates
        )
        blockers.append(f"duplicate unique key values detected: {detail}")

    action_fact_violations = inspect_action_fact_constraint_violations(engine)
    if request_id_backfill["unresolved"] == 0:
        action_fact_violations = [
            item
            for item in action_fact_violations
            if item["name"]
            != "ck_action_attempt_external_request_id_nonempty"
        ]
    else:
        blockers.append(
            "action_attempt external_request_id backfill unresolved: "
            f"{request_id_backfill['unresolved']} row(s)"
        )
    if action_fact_violations:
        detail = "; ".join(
            f"{item['name']} on {item['table']} has {item['count']} row(s)"
            for item in action_fact_violations
        )
        blockers.append(f"action fact constraint violations: {detail}")

    fact_hash_violations = inspect_action_fact_hash_violations(engine)
    if fact_hash_violations:
        detail = "; ".join(
            f"{item['table']} has {item['count']} row(s)"
            for item in fact_hash_violations
        )
        blockers.append(f"action fact hash violations: {detail}")

    terminal_state_violations = inspect_action_terminal_state_violations(engine)
    if terminal_state_violations:
        detail = "; ".join(
            f"intent {item['intent_id']} {item['reason']}"
            for item in terminal_state_violations
        )
        blockers.append(f"action terminal state violations: {detail}")

    if not blockers:
        return
    if any(blocker.startswith("manual remediation") for blocker in blockers):
        prefix = "manual remediation required before ZHA-26 migration: "
    elif any(blocker.startswith("foreign key mismatch") for blocker in blockers):
        prefix = "foreign key mismatch: release schema preflight blocked: "
    else:
        prefix = "release schema verification failed: "
    raise RuntimeError(prefix + "; ".join(blockers))


def _unsafe_missing_required_columns(engine, inspector, table_names: set[str]) -> List[str]:
    quote = engine.dialect.identifier_preparer.quote
    blockers: List[str] = []
    with engine.connect() as conn:
        for table_name, columns in (
            *ZHA26_SCHEMA_COLUMNS.items(),
            *AIOPS127_SCHEMA_COLUMNS.items(),
        ):
            if table_name not in table_names:
                continue
            existing_columns = {
                str(column["name"]) for column in inspector.get_columns(table_name)
            }
            missing_required = [
                column_name
                for column_name, definition in columns
                if column_name not in existing_columns
                and not bool(definition.nullable)
                and definition.server_default is None
            ]
            if not missing_required:
                continue
            has_rows = conn.execute(text(
                f"SELECT 1 FROM {quote(table_name)} LIMIT 1"
            )).first() is not None
            if has_rows:
                blockers.extend(
                    f"manual remediation required for {table_name}.{column_name}"
                    for column_name in missing_required
                )
    return blockers


def _action_plan_core_contract_blockers(
    inspector,
    table_names: set[str],
    dialect,
) -> List[str]:
    table_name = TopQueryActionPlan.__tablename__
    if table_name not in table_names:
        return []

    actual_columns = {
        str(column["name"]): column
        for column in inspector.get_columns(table_name)
    }
    extension_columns = set(ZHA26_ACTION_PLAN_EXTENSION_COLUMNS) | {
        column_name
        for column_name, _definition in RELEASE_SCHEMA_COLUMNS[table_name]
    }
    blockers: List[str] = []
    for column_name, definition in ZHA26_VERIFICATION_SCHEMA_COLUMNS[table_name]:
        if column_name in extension_columns:
            continue
        actual = actual_columns.get(column_name)
        if actual is None:
            blockers.append(
                f"manual remediation required for {table_name}.{column_name}"
            )
            continue
        if not _column_contract_check(actual, definition, dialect)["ok"]:
            blockers.append(f"column:{table_name}.{column_name}")

    if not _primary_key_identity_matches(
        inspector,
        table_name,
        TopQueryActionPlan,
        dialect,
    ):
        blockers.append(
            f"primary key mismatch: {table_name} expected auto-increment (id)"
        )
    return blockers


def _existing_release_column_contract_blockers(
    inspector,
    table_names: set[str],
    dialect,
) -> List[str]:
    blockers: List[str] = []
    for table_name, column_specs in RELEASE_SCHEMA_COLUMNS.items():
        if table_name not in table_names:
            continue
        actual_columns = {
            str(column["name"]): column
            for column in inspector.get_columns(table_name)
        }
        expected_columns = dict(
            RELEASE_VERIFICATION_SCHEMA_COLUMNS[table_name]
        )
        for column_name, _definition in column_specs:
            actual = actual_columns.get(column_name)
            if actual is None:
                continue
            if not _column_contract_check(
                actual,
                expected_columns[column_name],
                dialect,
            )["ok"]:
                blockers.append(f"column:{table_name}.{column_name}")
    return blockers


def _existing_schema_asset_contract_blockers(
    inspector,
    table_names: set[str],
    *,
    engine=None,
    unique_specs: Iterable[Dict[str, Any]],
    index_specs: Iterable[Dict[str, Any]],
    foreign_key_specs: Iterable[Dict[str, Any]],
) -> List[str]:
    blockers: List[str] = []
    for asset_type, specs, require_unique in (
        ("unique", unique_specs, True),
        ("index", index_specs, False),
    ):
        for spec in specs:
            table_name = str(spec["table"])
            if table_name not in table_names:
                continue
            asset_name = str(spec["name"])
            asset_name_exists = _schema_asset_name_exists(
                inspector,
                table_name,
                asset_name,
            )
            if not asset_name_exists and (
                engine is None
                or engine.dialect.name != "sqlite"
                or not _sqlite_schema_asset_name_exists(
                    engine,
                    table_name,
                    asset_name,
                )
            ):
                continue
            if not _schema_index_matches(
                inspector,
                spec,
                require_unique=require_unique,
            ):
                blockers.append(
                    f"{asset_type} mismatch: {asset_name} on {table_name}"
                )

    specs_by_table: Dict[str, List[Dict[str, Any]]] = {}
    for spec in foreign_key_specs:
        specs_by_table.setdefault(str(spec["table"]), []).append(spec)
    for table_name, table_specs in specs_by_table.items():
        if table_name not in table_names:
            continue
        unmatched_specs = list(table_specs)
        for foreign_key in inspector.get_foreign_keys(table_name):
            named_spec = next(
                (
                    spec
                    for spec in table_specs
                    if foreign_key.get("name") == spec["name"]
                ),
                None,
            )
            related_specs = [named_spec] if named_spec is not None else [
                spec
                for spec in table_specs
                if tuple(foreign_key.get("constrained_columns") or ())
                == tuple(spec["columns"])
            ]
            if not related_specs:
                blockers.append(
                    "foreign key mismatch: unexpected foreign key on "
                    f"{table_name} ({', '.join(foreign_key.get('constrained_columns') or ())})"
                )
                continue
            match_index = next(
                (
                    index
                    for index, spec in enumerate(unmatched_specs)
                    if spec in related_specs
                    and _foreign_key_matches(foreign_key, spec)
                ),
                None,
            )
            if match_index is not None:
                unmatched_specs.pop(match_index)
                continue
            asset_name = str(related_specs[0]["name"])
            blockers.append(
                f"foreign key mismatch: {asset_name} on {table_name}"
            )
    return blockers


def _sqlite_schema_asset_name_exists(
    engine,
    table_name: str,
    asset_name: str,
) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT 1
            FROM sqlite_master
            WHERE type = 'index'
              AND tbl_name = :table_name
              AND name = :asset_name
            LIMIT 1
        """), {
            "table_name": table_name,
            "asset_name": asset_name,
        }).first() is not None


def _schema_asset_name_exists(inspector, table_name: str, asset_name: str) -> bool:
    indexes, unique_constraints = _reflected_indexes_and_unique_constraints(
        inspector,
        table_name,
    )
    return any(
        asset.get("name") == asset_name
        for asset in (
            *indexes,
            *unique_constraints,
        )
    )


def _reflected_indexes_and_unique_constraints(inspector, table_name: str):
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Skipped unsupported reflection of expression-based index .*",
            category=SAWarning,
        )
        return (
            inspector.get_indexes(table_name),
            inspector.get_unique_constraints(table_name),
        )


def _ledger_foreign_keys_are_exact(foreign_keys: Sequence[Dict[str, Any]]) -> bool:
    if len(foreign_keys) != len(ZHA26_FOREIGN_KEY_SPECS):
        return False
    unmatched = list(foreign_keys)
    for spec in ZHA26_FOREIGN_KEY_SPECS:
        match_index = next(
            (
                index
                for index, foreign_key in enumerate(unmatched)
                if _foreign_key_matches(foreign_key, spec)
            ),
            None,
        )
        if match_index is None:
            return False
        unmatched.pop(match_index)
    return not unmatched


def _sqlite_ledger_rebuild_blockers(engine, inspector) -> List[str]:
    table_name = TopQueryBudgetLedger.__tablename__
    model_columns = {column.name for column in TopQueryBudgetLedger.__table__.columns}
    actual_columns = {
        str(column["name"]) for column in inspector.get_columns(table_name)
    }
    blockers = [
        f"sqlite ledger rebuild has unexpected column: {column_name}"
        for column_name in sorted(actual_columns - model_columns)
    ]
    ledger_specs = tuple(
        spec
        for spec in ZHA26_FOREIGN_KEY_SPECS
        if str(spec["table"]) == table_name
    )
    if any(
        not any(
            foreign_key.get("name") == spec["name"]
            or tuple(foreign_key.get("constrained_columns") or ())
            == tuple(spec["columns"])
            for spec in ledger_specs
        )
        for foreign_key in inspector.get_foreign_keys(table_name)
    ):
        blockers.append(
            "foreign key mismatch: sqlite ledger rebuild cannot preserve unrelated foreign key"
        )
    with engine.connect() as conn:
        triggers = conn.execute(text("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'trigger' AND tbl_name = :table_name
            ORDER BY name
        """), {"table_name": table_name}).scalars().all()
        blockers.extend(
            f"sqlite ledger rebuild cannot preserve trigger: {trigger_name}"
            for trigger_name in triggers
        )
        blockers.extend(_sqlite_unreplayable_index_blockers(conn, engine, table_name))

    check_constraints = inspector.get_check_constraints(table_name)
    blockers.extend(
        "sqlite ledger rebuild cannot preserve check constraint: "
        + str(constraint.get("name") or "<unnamed>")
        for constraint in check_constraints
    )
    return blockers


def _sqlite_unreplayable_index_blockers(
    conn,
    engine,
    table_name: str,
    *,
    model_columns: set[str] | None = None,
    allowed_autoindexes: Dict[tuple[str, tuple[str, ...]], int] | None = None,
    blocker_prefix: str = "sqlite ledger rebuild",
) -> List[str]:
    quote = engine.dialect.identifier_preparer.quote
    index_rows = conn.execute(text(
        f"PRAGMA index_list({quote(table_name)})"
    )).mappings().all()
    sql_by_name = {
        str(row["name"]): row["sql"]
        for row in conn.execute(text("""
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'index' AND tbl_name = :table_name
        """), {"table_name": table_name}).mappings()
    }
    if model_columns is None:
        model_columns = {
            column.name for column in TopQueryBudgetLedger.__table__.columns
        }
    if allowed_autoindexes is None:
        allowed_autoindexes = {
            ("u", ("idempotency_key",)): 1,
            ("pk", ("id",)): 1,
        }
    remaining_autoindexes = dict(allowed_autoindexes)
    blockers: List[str] = []
    for row in index_rows:
        index_name = str(row["name"])
        index_sql = sql_by_name.get(index_name)
        index_columns = tuple(
            str(index_row["name"])
            for index_row in conn.execute(text(
                f"PRAGMA index_info({quote(index_name)})"
            )).mappings().all()
            if index_row.get("name") is not None
        )
        if index_sql is None:
            signature = (str(row.get("origin") or ""), index_columns)
            remaining = remaining_autoindexes.get(signature, 0)
            if remaining:
                remaining_autoindexes[signature] = remaining - 1
            else:
                blockers.append(
                    f"{blocker_prefix} cannot replay autoindex: {index_name}"
                )
            continue
        if (
            int(row.get("partial") or 0) != 0
            or not index_columns
            or any(column not in model_columns for column in index_columns)
            or re.match(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+", index_sql, re.IGNORECASE) is None
        ):
            blockers.append(
                f"{blocker_prefix} cannot replay index: {index_name}"
            )
    return blockers


def _raise_on_column_contract_mismatches(
    engine,
    *,
    allow_action_attempt_request_id_nullable: bool = False,
) -> None:
    inspector = inspect(engine)
    mismatches: List[str] = []
    for table_name, columns in RELEASE_VERIFICATION_SCHEMA_COLUMNS.items():
        actual_columns = {
            str(column["name"]): column
            for column in inspector.get_columns(table_name)
        }
        for column_name, definition in columns:
            result = _release_column_contract_check(
                engine,
                table_name,
                column_name,
                actual_columns.get(column_name),
                definition,
            )
            allowed_request_id_upgrade = (
                allow_action_attempt_request_id_nullable
                and table_name == ActionAttempt.__tablename__
                and column_name == "external_request_id"
                and result["present"]
                and result["type"]
                and result["default"]
                and not result["nullable"]
                and bool(actual_columns[column_name].get("nullable"))
            )
            if not result["ok"] and not allowed_request_id_upgrade:
                mismatches.append(f"column:{table_name}.{column_name}")
    if mismatches:
        raise RuntimeError(
            "release schema verification failed: " + ", ".join(mismatches)
        )


def _schema_index_matches(inspector, spec: Dict[str, Any], *, require_unique: bool) -> bool:
    table_name = str(spec["table"])
    if table_name not in set(inspector.get_table_names()):
        return False
    expected_name = str(spec["name"])
    expected_columns = tuple(str(column) for column in spec["columns"])
    indexes, unique_constraints = _reflected_indexes_and_unique_constraints(
        inspector,
        table_name,
    )
    for index in indexes:
        if index.get("name") != expected_name:
            continue
        return (
            tuple(str(column) for column in (index.get("column_names") or ()))
            == expected_columns
            and bool(index.get("unique")) is require_unique
        )
    if not require_unique:
        return False
    for constraint in unique_constraints:
        if constraint.get("name") != expected_name:
            continue
        return (
            tuple(str(column) for column in (constraint.get("column_names") or ()))
            == expected_columns
        )
    return False


def _release_column_contract_check(
    engine,
    table_name: str,
    column_name: str,
    actual: Dict[str, Any] | None,
    expected,
) -> Dict[str, bool]:
    allow_type_qualifier_mismatch = False
    if (
        (table_name, column_name)
        in AIOPS127_TXSQL_QUALIFIER_COMPATIBLE_COLUMNS
        and engine.dialect.name in {"mysql", "mariadb"}
    ):
        enforcement = inspect_database_check_enforcement(engine)
        allow_type_qualifier_mismatch = enforcement.get("family") == "txsql"
    result = _column_contract_check(
        actual,
        expected,
        engine.dialect,
        allow_type_qualifier_mismatch=allow_type_qualifier_mismatch,
    )
    if (
        table_name == QueryGovernanceContextSnapshot.__tablename__
        and actual is not None
        and engine.dialect.name in {"mysql", "mariadb"}
    ):
        # Existing MySQL tables may reflect SQLAlchemy Text as MEDIUMTEXT and
        # include the table's explicit COLLATE qualifier. Both are compatible
        # with the utf8mb4 table contract and preserve the stored data.
        actual_signature = _column_type_signature(actual["type"], engine.dialect)
        expected_signature = _column_type_signature(expected.type, engine.dialect)
        compatible_text = (
            column_name == "payload"
            and
            actual_signature[0] in {"TEXT", "MEDIUMTEXT"}
            and expected_signature[0] in {"TEXT", "MEDIUMTEXT"}
        )
        compatible_qualified_type = (
            (actual_signature[:2] == expected_signature[:2] or compatible_text)
            and actual_signature[2] in {"", "COLLATE UTF8MB4_UNICODE_CI"}
        )
        if compatible_qualified_type:
            result["type"] = True
            result["ok"] = result["present"] and result["nullable"] and result["default"]
    return result


def _column_contract_check(
    actual: Dict[str, Any] | None,
    expected,
    dialect,
    *,
    allow_type_qualifier_mismatch: bool = False,
) -> Dict[str, bool]:
    present = actual is not None
    actual_type_signature = (
        _column_type_signature(actual["type"], dialect)
        if present
        else None
    )
    expected_type_signature = _column_type_signature(expected.type, dialect)
    type_matches = actual_type_signature == expected_type_signature
    if (
        present
        and not type_matches
        and allow_type_qualifier_mismatch
        and actual_type_signature is not None
        and actual_type_signature[:2] == expected_type_signature[:2]
    ):
        type_matches = True
    nullable_matches = present and bool(actual.get("nullable")) == bool(expected.nullable)
    default_matches = present and _column_defaults_match(
        actual.get("default"),
        expected.server_default.arg if expected.server_default is not None else None,
        expected.type,
    )
    return {
        "present": present,
        "type": type_matches,
        "nullable": nullable_matches,
        "default": default_matches,
        "ok": present and type_matches and nullable_matches and default_matches,
    }


def _primary_key_identity_matches(inspector, table_name: str, model, dialect) -> bool:
    primary_key = tuple(
        str(column)
        for column in (
            inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
        )
    )
    if primary_key != ("id",):
        return False

    actual_id = next(
        (
            column
            for column in inspector.get_columns(table_name)
            if str(column.get("name")) == "id"
        ),
        None,
    )
    if actual_id is None:
        return False
    expected_id = model.__table__.c.id
    if _column_type_signature(
        actual_id["type"], dialect
    ) != _column_type_signature(expected_id.type, dialect):
        return False
    if dialect.name == "sqlite":
        return isinstance(actual_id["type"], Integer)
    if dialect.name in {"mysql", "mariadb"}:
        return actual_id.get("autoincrement") is True
    return (
        actual_id.get("autoincrement") is True
        or actual_id.get("identity") is not None
    )


def _column_type_signature(column_type, dialect) -> tuple[Any, ...]:
    compiled = " ".join(
        str(column_type.compile(dialect=dialect)).upper().split()
    )
    match = re.fullmatch(
        r"([A-Z0-9_]+)(?:\s*\(([^)]*)\))?(?:\s+(.*))?",
        compiled,
    )
    if not match:
        return (compiled,)
    base_type, raw_parameters, raw_qualifiers = match.groups()
    base_type = {"INT": "INTEGER"}.get(base_type, base_type)
    parameters = (
        tuple(parameter.strip() for parameter in raw_parameters.split(","))
        if raw_parameters is not None
        else ()
    )
    if base_type in {"INTEGER", "BIGINT", "SMALLINT", "TINYINT"} and all(
        parameter.isdigit() for parameter in parameters
    ):
        parameters = ()
    qualifiers = raw_qualifiers.strip() if raw_qualifiers else ""
    return (base_type, parameters, qualifiers)


def _column_defaults_match(actual: Any, expected: Any, expected_type) -> bool:
    return _normalize_column_default(
        actual,
        expected_type=expected_type,
    ) == _normalize_column_default(
        expected,
        expected_type=expected_type,
    )


def _normalize_column_default(
    value: Any,
    *,
    expected_type=None,
) -> tuple[str, Any] | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {"'", '"'}
    ):
        quote = normalized[0]
        literal = normalized[1:-1].replace(quote * 2, quote)
        numeric_literal = _normalize_numeric_literal(literal)
        if _is_numeric_default_type(expected_type) and numeric_literal is not None:
            return ("number", numeric_literal)
        return ("string", literal)
    numeric_literal = _normalize_numeric_literal(normalized)
    if numeric_literal is not None:
        return ("number", numeric_literal)
    sql_expression = re.sub(r"\(\)$", "", normalized).lower()
    return ("sql", sql_expression)


def _normalize_numeric_literal(value: str) -> Decimal | None:
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value) is None:
        return None
    try:
        return Decimal(value).normalize()
    except InvalidOperation:
        return None


def _is_numeric_default_type(column_type) -> bool:
    return isinstance(column_type, (Boolean, Float, Integer, Numeric))


def _schema_foreign_key_matches(inspector, spec: Dict[str, Any]) -> bool:
    table_name = str(spec["table"])
    if table_name not in set(inspector.get_table_names()):
        return False
    return any(
        _foreign_key_matches(foreign_key, spec)
        for foreign_key in inspector.get_foreign_keys(table_name)
    )


def _foreign_key_matches(foreign_key: Dict[str, Any], spec: Dict[str, Any]) -> bool:
    actual_options = {
        str(key).lower(): _normalize_foreign_key_action(value)
        for key, value in (foreign_key.get("options") or {}).items()
        if value is not None
        and str(key).lower() in {"ondelete", "onupdate"}
    }
    expected_options = {
        key: _normalize_foreign_key_action(spec.get(key))
        for key in ("ondelete", "onupdate")
        if spec.get(key) is not None
    }
    return (
        tuple(foreign_key.get("constrained_columns") or ()) == tuple(spec["columns"])
        and foreign_key.get("referred_table") == spec["referred_table"]
        and tuple(foreign_key.get("referred_columns") or ())
        == tuple(spec["referred_columns"])
        and actual_options == expected_options
    )


def _normalize_foreign_key_action(value: Any) -> str:
    return " ".join(str(value).strip().upper().split())


def _rebuild_sqlite_table_from_model(engine, table_name: str) -> None:
    model = RELEASE_FOREIGN_KEY_TABLE_MODELS[table_name]
    inspector = inspect(engine)
    existing_columns = {
        str(column["name"]) for column in inspector.get_columns(table_name)
    }
    model_columns = {column.name for column in model.__table__.columns}
    extra_columns = existing_columns - model_columns
    if extra_columns:
        raise RuntimeError(
            f"release schema verification failed: unexpected columns on {table_name}: "
            + ", ".join(sorted(extra_columns))
        )

    temp_table_name = f"__zha26_{table_name}_new"
    metadata = MetaData()
    for referred_table in dict.fromkeys(
        str(spec["referred_table"])
        for spec in RELEASE_FOREIGN_KEY_SPECS
    ):
        if referred_table == table_name:
            continue
        RELEASE_FOREIGN_KEY_TABLE_MODELS[referred_table].__table__.to_metadata(
            metadata
        )
    temp_table = model.__table__.to_metadata(metadata, name=temp_table_name)
    quote = engine.dialect.identifier_preparer.quote
    column_expr = ", ".join(quote(column.name) for column in model.__table__.columns)
    with engine.begin() as conn:
        index_statements = conn.execute(text("""
            SELECT sql
            FROM sqlite_master
            WHERE type = 'index'
              AND tbl_name = :table_name
              AND sql IS NOT NULL
            ORDER BY name
        """), {"table_name": table_name}).scalars().all()
        conn.execute(CreateTable(temp_table))
        conn.execute(text(
            f"INSERT INTO {quote(temp_table_name)} ({column_expr}) "
            f"SELECT {column_expr} FROM {quote(table_name)}"
        ))
        conn.execute(text(f"DROP TABLE {quote(table_name)}"))
        conn.execute(text(
            f"ALTER TABLE {quote(temp_table_name)} RENAME TO {quote(table_name)}"
        ))
        for statement in index_statements:
            conn.execute(text(str(statement)))


def _failed_release_schema_checks(verification: Dict[str, Any]) -> List[str]:
    failed = [
        f"table:{name}"
        for name, passed in verification["tables"].items()
        if not passed
    ]
    failed.extend(
        f"table_charset:{name}"
        for name, passed in verification["tableCharsets"].items()
        if not passed
    )
    failed.extend(
        f"primary_key:{name}"
        for name, passed in verification["primaryKeys"].items()
        if not passed
    )
    failed.extend(
        f"column:{table_name}.{column_name}"
        for table_name, columns in verification["columns"].items()
        for column_name, result in columns.items()
        if not result["ok"]
    )
    failed.extend(
        f"unique:{name}"
        for name, passed in verification["uniqueConstraints"].items()
        if not passed
    )
    failed.extend(
        f"index:{name}"
        for name, passed in verification["indexes"].items()
        if not passed
    )
    failed.extend(
        f"foreign_key:{name}"
        for name, passed in verification["foreignKeys"].items()
        if not passed
    )
    if verification["foreignKeyEnforcement"] is not True:
        failed.append("foreign_key_enforcement")
    failed.extend(
        f"check_constraint:{name}"
        for name, passed in verification["checkConstraints"].items()
        if not passed
    )
    failed.extend(
        f"fact_hash:{item['table']}"
        for item in verification["factHashViolations"]
    )
    failed.extend(
        f"terminal_state:{item['intent_id']}:{item['reason']}"
        for item in verification["terminalStateViolations"]
    )
    if verification["checkConstraintEnforcement"]["checkConstraintsEnforced"] is not True:
        failed.append("check_constraint_enforcement")
    return failed


def apply_assistant_bootstrap(SessionLocal, bootstrap: Dict[str, Any], log: Logger = print) -> Dict[str, int]:
    summary = {
        "brand_spaces_created": 0,
        "brand_spaces_updated": 0,
        "bindings_created": 0,
        "bindings_updated": 0,
        "admin_bindings_created": 0,
    }
    session = SessionLocal()
    try:
        admin_users = session.query(User).filter(User.username.in_(bootstrap["adminUsernames"])).all()
        admin_by_name = {user.username: user for user in admin_users}
        missing_admins = [username for username in bootstrap["adminUsernames"] if username not in admin_by_name]
        if missing_admins:
            raise ValueError(f"admin users missing for bootstrap: {', '.join(missing_admins)}")

        for item in bootstrap["brandSpaces"]:
            brand_space = session.query(AssistantBrandSpace).filter(
                AssistantBrandSpace.brand == item["brand"]
            ).first()
            created_space = False
            if not brand_space:
                brand_space = AssistantBrandSpace(
                    brand=item["brand"],
                    display_name=item["displayName"],
                    enabled=1,
                )
                session.add(brand_space)
                session.flush()
                summary["brand_spaces_created"] += 1
                created_space = True
            else:
                brand_space.display_name = item["displayName"]
                brand_space.enabled = 1
                summary["brand_spaces_updated"] += 1

            binding_payload = item["defaultBinding"]
            binding = _find_existing_binding(session, brand_space.id, binding_payload)
            if not binding:
                binding = AssistantAgentBinding(
                    brand_space_id=brand_space.id,
                    display_name=binding_payload["displayName"],
                    model_display_name=binding_payload["modelDisplayName"],
                    model_name=binding_payload["modelName"],
                    available_models_json=json.dumps(binding_payload["availableModels"], ensure_ascii=False),
                    supports_model_switch=1 if binding_payload["supportsModelSwitch"] else 0,
                    binding_type=binding_payload["bindingType"],
                    enabled=1,
                )
                session.add(binding)
                session.flush()
                summary["bindings_created"] += 1
            else:
                binding.display_name = binding_payload["displayName"]
                binding.model_display_name = binding_payload["modelDisplayName"]
                binding.model_name = binding_payload["modelName"]
                binding.available_models_json = json.dumps(binding_payload["availableModels"], ensure_ascii=False)
                binding.supports_model_switch = 1 if binding_payload["supportsModelSwitch"] else 0
                binding.binding_type = binding_payload["bindingType"]
                binding.enabled = 1
                summary["bindings_updated"] += 1

            brand_space.default_agent_binding_id = binding.id
            if created_space:
                session.flush()

            for admin_user in admin_users:
                existing = session.query(AssistantUserBinding).filter(
                    AssistantUserBinding.user_id == admin_user.id,
                    AssistantUserBinding.brand_space_id == brand_space.id,
                    AssistantUserBinding.agent_binding_id == binding.id,
                ).first()
                if existing:
                    continue
                session.add(AssistantUserBinding(
                    user_id=admin_user.id,
                    brand_space_id=brand_space.id,
                    agent_binding_id=binding.id,
                ))
                summary["admin_bindings_created"] += 1

        session.commit()
        log(f"ASSISTANT_BOOTSTRAP_SUMMARY: {json.dumps(summary, ensure_ascii=False)}")
        return summary
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _find_existing_binding(session, brand_space_id: int, payload: Dict[str, Any]) -> AssistantAgentBinding | None:
    candidates = session.query(AssistantAgentBinding).filter(
        AssistantAgentBinding.brand_space_id == brand_space_id
    ).all()
    for candidate in candidates:
        if candidate.display_name == payload["displayName"]:
            return candidate
    return None
