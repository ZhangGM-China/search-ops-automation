"""Local database rebuild and migration rehearsal for the Query rewrite.

This module is intentionally local-only.  Production migration is a separate
release operation and is not reachable through this helper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine

from src.persistence.models import Base
from src.persistence import models as legacy_models

from . import models as query_models


class QueryGovernanceDatabaseError(RuntimeError):
    """Raised when a local rebuild cannot prove the requested table scope."""


@dataclass(frozen=True)
class QueryGovernanceRebuildReport:
    created_tables: tuple[str, ...] = ()
    deleted_tables: tuple[str, ...] = ()
    preserved_tables: tuple[str, ...] = ()
    migrated_rows: dict[str, int] = field(default_factory=dict)


PRESERVED_MODEL_NAMES = (
    "User",
    "InventoryRecoveryTask",
    "DailyAbnormalQuery",
    "ActionLog",
    "OperationLog",
    "ActionIntent",
    "ActionAttempt",
    "ActionFenceEvidence",
    "ActionReadback",
    "ActionTerminalFact",
    "SchedulerJobRun",
    "SchedulerLeaderLease",
    "TaskRunSession",
    "NodeModelConfigVersion",
    "NodeModelConfigEntry",
    "NoResultGovernanceBatch",
    "NoResultQuery",
    "NoResultGovernanceAction",
    "NoResultAgentEvent",
    "NoResultRetagTask",
    "NoResultIgnoreRecord",
    "BrandDailyMetric",
    "AssistantBrandSpace",
    "AssistantAgentBinding",
    "AssistantUserBinding",
    "AssistantSessionMeta",
    "AssistantActionRequest",
)

NEW_MODEL_NAMES = (
    "QueryGovernanceSelectionBatch",
    "QueryGovernanceCandidate",
    "QueryGovernanceBatchSummary",
    "QueryGovernanceTask",
    "QueryGovernanceStepStatus",
    "QueryGovernanceContextSnapshot",
    "QueryGovernanceAgentEvent",
    "QueryGovernanceActionPlan",
    "QueryGovernanceWorkflowRun",
    "QueryGovernanceAsyncTask",
    "QueryGovernanceManualRelevanceCheck",
    "QueryGovernanceExperiment",
    "QueryGovernanceExperimentSample",
    "QueryGovernanceExperimentDailyMetric",
    "QueryGovernanceRelevanceRecord",
    "QueryGovernanceImageAsset",
    "QueryGovernanceRelevanceGuidance",
    "QueryGovernanceRelevanceGuidanceReason",
    "QueryMetricSnapshot",
)

LEGACY_QUERY_TABLES = frozenset(
    {
        "agent_execution_event",
        "top_query_task",
        "top_query_step_status",
        "top_query_context_snapshot",
        "top_query_agent_event",
        "top_query_action_plan",
        "top_query_workflow_run",
        "top_query_budget_account",
        "top_query_budget_ledger",
        "top_query_score",
        "top_query_experiment",
        "top_query_experiment_sample",
        "top_query_experiment_daily_metric",
        "task_run_session",
        "ops_team_run",
        "ops_team_message",
        "ops_expert_ranking",
        "ops_agent_prompt_version",
        "ops_agent_decision_audit",
        "ops_intelligence_snapshot",
        "ops_visual_review_cache",
        "agent_experience_entry",
        "ops_notification_record",
        # Residual tables created by rewrite prototypes before the current
        # query_governance_* schema was finalized.
        "query_governance_stage",
        "top_query_governance_action",
        "top_query_governance_score",
        "top_query_governance_task",
        "top_query_status_event",
    }
)


def _table_for(model_module, name: str):
    return getattr(model_module, name).__table__


def _planned_tables() -> dict[str, object]:
    tables = {_table_for(legacy_models, name).name: _table_for(legacy_models, name) for name in PRESERVED_MODEL_NAMES}
    tables.update({_table_for(query_models, name).name: _table_for(query_models, name) for name in NEW_MODEL_NAMES})
    return tables


def active_schema_tables() -> tuple[object, ...]:
    """Return the tables owned by the active local application schema."""

    return tuple(_planned_tables().values())


def _unknown_legacy_tables(existing: Iterable[str]) -> list[str]:
    planned = set(_planned_tables()) | set(LEGACY_QUERY_TABLES)
    candidates = []
    for table_name in existing:
        if table_name in planned:
            continue
        if table_name.startswith("top_query") or table_name.startswith("ops_") or table_name == "agent_experience_entry":
            candidates.append(table_name)
    return sorted(candidates)


def _copy_rows(source: Engine, target: Engine, table_names: Iterable[str]) -> dict[str, int]:
    source_inspector = inspect(source)
    target_inspector = inspect(target)
    migrated: dict[str, int] = {}
    for table_name in table_names:
        if table_name not in source_inspector.get_table_names() or table_name not in target_inspector.get_table_names():
            migrated[table_name] = 0
            continue
        source_table = _planned_tables()[table_name]
        target_table = _planned_tables()[table_name]
        with source.connect() as source_connection:
            rows = source_connection.execute(select(source_table)).mappings().all()
        if not rows:
            migrated[table_name] = 0
            continue
        target_columns = {column.name for column in target_table.columns}
        values = [{key: value for key, value in row.items() if key in target_columns} for row in rows]
        with target.begin() as conn:
            # The rehearsal may be run repeatedly.  SQLite's OR IGNORE keeps
            # the first copied row and avoids duplicate primary/unique keys.
            statement = target_table.insert().prefix_with("OR IGNORE")
            conn.execute(statement, values)
        migrated[table_name] = len(values)
    return migrated


def rebuild_query_governance_schema(
    engine: Engine,
    *,
    source_engine: Engine | None = None,
) -> QueryGovernanceRebuildReport:
    """Rebuild the new Query schema on a local SQLite database.

    Existing users and inventory rows are retained when rebuilding in place.
    When ``source_engine`` is supplied, the two migration tables are copied to
    the target and their row counts are returned for readback verification.
    """

    if engine.dialect.name != "sqlite" or (source_engine is not None and source_engine.dialect.name != "sqlite"):
        raise QueryGovernanceDatabaseError("本地数据库重构仅允许使用 SQLite，不执行正式环境迁移")

    existing = set(inspect(engine).get_table_names())
    unknown = _unknown_legacy_tables(existing)
    if unknown:
        raise QueryGovernanceDatabaseError("发现未列入计划的旧 Query 表: " + ", ".join(unknown))

    planned = _planned_tables()
    new_table_names = {_table_for(query_models, name).name for name in NEW_MODEL_NAMES}
    retired = sorted((existing & LEGACY_QUERY_TABLES) | (existing & new_table_names))
    with engine.begin() as conn:
        # Foreign keys are disabled only for this local DDL operation so child
        # legacy tables can be removed regardless of their creation order.
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table_name in retired:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        conn.execute(text("PRAGMA foreign_keys=ON"))

    desired_tables = [planned[name] for name in planned]
    Base.metadata.create_all(bind=engine, tables=desired_tables, checkfirst=True)

    migrated_rows: dict[str, int] = {}
    if source_engine is not None and source_engine is not engine:
        migrated_rows = _copy_rows(source_engine, engine, ("users", "inventory_recovery_task"))

    actual = set(inspect(engine).get_table_names())
    return QueryGovernanceRebuildReport(
        created_tables=tuple(sorted(actual & set(planned))),
        deleted_tables=tuple(retired),
        preserved_tables=tuple(sorted(actual & { _table_for(legacy_models, name).name for name in PRESERVED_MODEL_NAMES })),
        migrated_rows=migrated_rows,
    )


__all__ = [
    "active_schema_tables",
    "QueryGovernanceDatabaseError",
    "QueryGovernanceRebuildReport",
    "rebuild_query_governance_schema",
]
