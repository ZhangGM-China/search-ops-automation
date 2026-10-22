"""Scheduler-facing entrypoint for the deterministic Query mainline.

The scheduler owns only fan-out and batch selection.  Query execution remains
inside :class:`QueryGovernanceWorkflow`; active A/B experiments are routed to
the same workflow with their fixed treatment/control samples.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import hashlib
import os
import time
from pathlib import Path
from dataclasses import replace
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import String, and_, case, cast, func, or_, select
from src.runtime.process_guard import ProcessLockError, acquire_process_lock

from src.config import BRAND_CONFIGS, get_brand_business_config, is_brand_enabled, reload_config, _raw_config
from src.integrations.query_extractor import QueryExtractor
from src.persistence import SessionLocal

from .experiments import QueryGovernanceExperimentScheduler, QueryGovernanceExperimentService
from .image_integrity import asset_records_mismatch, image_health_report, render_alert_lines
from .metric_snapshots import materialize_candidate_metrics
from .models import (
    QueryGovernanceBatchSummary,
    QueryGovernanceImageAsset,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
    QueryGovernanceStepStatus,
)
from .read_api import QueryGovernanceReadService, quality_metrics_payload_marker
from .selection import (
    QuerySelectionConfig,
    QuerySelectionService,
    QuerySourceSpec,
    SqlAlchemyQuerySelectionStore,
    SqlQueryCandidateSource,
)
from .visibility import check_modes_for_date


_BRAND_TYPES = {
    "riviera": "rl",
    "summit": "rds",
}
_LOGGER = logging.getLogger(__name__)
QUERY_GOVERNANCE_RECOVERY_STALE_MINUTES = 30
QUERY_GOVERNANCE_MAX_NODE_STARTS = 100
QUERY_GOVERNANCE_MAX_EXECUTION_SECONDS = 240
# 品牌锁被断码巡检等阻塞持有者占用时，主治理任务最多等待多久再放弃当天该品牌。
QUERY_GOVERNANCE_BRAND_LOCK_WAIT_SECONDS = int(os.getenv("QUERY_GOVERNANCE_BRAND_LOCK_WAIT_SECONDS", "1800") or 1800)
QUERY_GOVERNANCE_BRAND_LOCK_POLL_SECONDS = int(os.getenv("QUERY_GOVERNANCE_BRAND_LOCK_POLL_SECONDS", "30") or 30)


def _acquire_brand_scope_lock(brand: str, lock_dir: Path, wait_seconds: int) -> Any:
    """Acquire the per-brand governance lock, waiting up to ``wait_seconds``.

    The inventory monitor holds this lock in blocking mode while executing each
    demotion/recovery action, so an instant non-blocking acquire can silently
    drop the brand for the whole day.  Waiting is deterministic code-side retry;
    ``wait_seconds <= 0`` keeps the legacy fail-fast behaviour for the recovery
    job, which must never queue behind an active mainline brand.
    """
    name = "brand-governance-" + hashlib.sha256(brand.encode()).hexdigest()
    if wait_seconds <= 0:
        return acquire_process_lock(name, lock_dir=lock_dir)
    deadline = time.monotonic() + wait_seconds
    logged_waiting = False
    while True:
        try:
            return acquire_process_lock(name, lock_dir=lock_dir)
        except ProcessLockError:
            if not logged_waiting:
                logged_waiting = True
                _LOGGER.warning(
                    "query governance brand lock busy, waiting up to %ss (likely inventory monitor holding it) brand=%s",
                    wait_seconds,
                    brand,
                )
            if time.monotonic() + QUERY_GOVERNANCE_BRAND_LOCK_POLL_SECONDS >= deadline:
                _LOGGER.warning(
                    "query governance brand lock still busy after waiting %ss, giving up brand=%s",
                    wait_seconds,
                    brand,
                )
                raise
            time.sleep(QUERY_GOVERNANCE_BRAND_LOCK_POLL_SECONDS)


def _workflow_progress(session_factory: Callable, batch_id: str) -> tuple[str | None, int]:
    with session_factory() as session:
        status = session.scalar(select(QueryGovernanceWorkflowRun.status).where(
            QueryGovernanceWorkflowRun.batch_id == batch_id))
        completed = session.scalar(select(func.count(QueryGovernanceStepStatus.id))
            .join(QueryGovernanceTask, QueryGovernanceTask.id == QueryGovernanceStepStatus.task_id)
            .where(QueryGovernanceTask.batch_id == batch_id,
                   QueryGovernanceStepStatus.status.in_(("executed", "skipped", "error"))))
    return status, int(completed or 0)


def _query_source_spec(brand: str) -> QuerySourceSpec:
    """Build the read-only candidate SQL mapping from the existing brand facts."""

    if brand == "riviera":
        return QuerySourceSpec(
            table_name="rl_keyword_daily",
            query_column="search_term",
            search_pv_column="search_uv",
            search_uv_column="search_uv",
            click_uv_column="click_uv",
            purchase_uv_column="purchase_uv",
        )
    if brand == "harbor":
        return QuerySourceSpec(
            table_name="dwd_harbor_query_behavior_daily",
            query_column="query",
            search_pv_column="query_search_pv",
            search_uv_column="query_search_uv",
            click_uv_column="query_search_click_uv",
            purchase_uv_column="query_search_pay_uv",
        )
    if brand == "summit":
        return QuerySourceSpec(
            table_name="dwd_summit_query_behavior_daily",
            query_column="query",
            search_pv_column="query_search_pv",
            search_uv_column="query_search_uv",
            click_uv_column="query_search_click_uv",
            purchase_uv_column="query_search_pay_uv",
        )
    if brand == "meadow":
        return QuerySourceSpec(
            table_name="dwd_meadow_query_behavior_daily",
            query_column="query",
            search_pv_column="query_search_pv",
            search_uv_column="query_search_uv",
            click_uv_column="query_search_click_uv",
            purchase_uv_column="query_search_pay_uv",
        )
    return QuerySourceSpec(
        table_name="dwd_query_behavior_daily",
        query_column="query",
        search_pv_column="query_search_pv",
        search_uv_column="query_search_uv",
        click_uv_column="query_search_click_uv",
        purchase_uv_column="query_search_pay_uv",
    )


class QueryGovernanceCandidateSource:
    """Create one read-only SQL candidate source per brand on demand."""

    def __init__(self) -> None:
        self._sources: dict[str, SqlQueryCandidateSource] = {}
        self._extractors: dict[str, QueryExtractor] = {}

    def _source(self, brand: str) -> SqlQueryCandidateSource:
        source = self._sources.get(brand)
        if source is not None:
            return source
        extractor = QueryExtractor(brand_type=_BRAND_TYPES.get(brand, "rds"))
        self._extractors[brand] = extractor
        source = SqlQueryCandidateSource(
            extractor.engine,
            {brand: _query_source_spec(brand)},
        )
        self._sources[brand] = source
        return source

    def fetch(self, brand: str, data_date: dt.date, config: QuerySelectionConfig):
        return self._source(brand).fetch(brand, data_date, config)


def build_query_selection_service(
    brand: str,
    *,
    session_factory: Callable[[], Any] = SessionLocal,
    candidate_source: Any | None = None,
) -> QuerySelectionService:
    source = candidate_source or QueryGovernanceCandidateSource()
    return QuerySelectionService(
        source=source,
        store=SqlAlchemyQuerySelectionStore(session_factory),
    )


def _materialize_tracked_query_metrics(
    brand: str,
    execution_date: dt.date,
    session_factory: Callable[[], Any],
    *,
    metric_provider: Callable[[str, str, dt.date], Mapping[str, Any]] | None = None,
) -> int:
    provider = metric_provider
    batch_provider = None
    if provider is None:
        extractor = QueryExtractor(brand_type=_BRAND_TYPES.get(brand, "rds"))
        provider = extractor.get_query_performance
        batch_provider = extractor.get_queries_performance_batch
    with session_factory() as session:
        return materialize_candidate_metrics(
            session,
            brand=brand,
            data_date=execution_date,
            metric_provider=provider,
            metric_batch_provider=batch_provider,
            as_of_date=execution_date,
        )


def _materialize_tracked_query_metrics_best_effort(
    brand: str,
    execution_date: dt.date,
    session_factory: Callable[[], Any],
    *,
    metric_provider: Callable[[str, str, dt.date], Mapping[str, Any]] | None = None,
) -> int:
    """Materialize the metric cache without failing the governance mainline.

    A silently dead MySQL connection (proxy/NAT drop) raises here after the
    read timeout.  The snapshot cache is a read optimization, so one retry
    followed by a logged skip must never drop the brand from continuation.
    """
    for attempt in (1, 2):
        try:
            return _materialize_tracked_query_metrics(
                brand, execution_date, session_factory, metric_provider=metric_provider
            )
        except Exception:
            _LOGGER.exception(
                "query governance metric materialization failed brand=%s data_date=%s attempt=%s",
                brand,
                execution_date.isoformat(),
                attempt,
            )
    return 0


def query_governance_mainline_job(
    brand_filter: str | None = None,
    target_date: dt.date | None = None,
    *,
    workflow_factory: Callable[[str], Any] | None = None,
    session_factory: Callable[[], Any] = SessionLocal,
    experiment_service_factory: Callable[[], QueryGovernanceExperimentService] | None = None,
    selection_service_factory: Callable[[str], QuerySelectionService] | None = None,
    candidate_source: Any | None = None,
    metric_provider_factory: Callable[[str], Callable[[str, str, dt.date], Mapping[str, Any]]] | None = None,
    overview_summary_materializer: Callable[[str, dt.date], Any] | None = None,
    brand_lock_wait_seconds: int | None = None,
) -> dict[str, Any]:
    """Run the daily dynamic Query selection or the active A/B experiment.

    One brand failure is returned in the summary and does not prevent other
    enabled brands from running.  The batch ID is deterministic for a brand
    and data date, so selection and checkpoint recovery are idempotent.
    """

    if workflow_factory is None:
        raise RuntimeError("QUERY_GOVERNANCE_WORKFLOW_FACTORY_REQUIRED")
    reload_config()
    lock_wait_seconds = (
        QUERY_GOVERNANCE_BRAND_LOCK_WAIT_SECONDS if brand_lock_wait_seconds is None else brand_lock_wait_seconds
    )
    execution_date = target_date or (dt.date.today() - dt.timedelta(days=1))
    workflows: dict[str, Any] = {}
    def dated_workflow_factory(current_brand: str) -> Any:
        workflow = workflow_factory(current_brand)
        workflows[current_brand] = workflow
        if hasattr(workflow, "max_node_starts") and getattr(workflow, "checkpoint", None) is not None:
            workflow.max_node_starts = QUERY_GOVERNANCE_MAX_NODE_STARTS
            workflow.max_execution_seconds = QUERY_GOVERNANCE_MAX_EXECUTION_SECONDS
        check_modes = check_modes_for_date(_raw_config, current_brand, execution_date) or {}
        for attribute, key in (("recall_enabled", "recall"), ("reorder_enabled", "open_factor"), ("relevance_enabled", "relevance")):
            if hasattr(workflow, attribute):
                enabled = bool(check_modes.get(key, True))
                if key == "open_factor":
                    tool_modes = (get_brand_business_config(current_brand) or {}).get("top_query_tool_modes") or {}
                    enabled = enabled and tool_modes.get("query_reorder") != "disabled"
                setattr(workflow, attribute, enabled)
        return workflow
    brands = [brand_filter] if brand_filter else list(BRAND_CONFIGS.keys())
    experiment_service = (
        experiment_service_factory() if experiment_service_factory else QueryGovernanceExperimentService(session_factory)
    )
    experiment_scheduler = QueryGovernanceExperimentScheduler(
        session_factory,
        dated_workflow_factory,
    )
    results: dict[str, Any] = {}
    continuations: dict[str, Any] = {}
    held_scope_locks: dict[str, Any] = {}
    materialize_summary = overview_summary_materializer
    if materialize_summary is None:
        read_service = QueryGovernanceReadService(session_factory)
        materialize_summary = lambda brand, data_date: read_service.materialize_overview_summary(
            brand, data_date=data_date
        )

    def persist_overview_summary(brand: str) -> None:
        try:
            materialize_summary(brand, execution_date)
        except Exception:
            # Summary persistence is an asynchronous read optimization.  It
            # must never change the governance outcome or block other brands.
            _LOGGER.exception(
                "query governance overview summary materialization failed brand=%s data_date=%s",
                brand,
                execution_date.isoformat(),
            )

    try:
        for brand in brands:
            batch_id = f"query-governance:{brand}:{execution_date.isoformat()}"
            if not brand or not is_brand_enabled(brand):
                if brand:
                    results[brand] = {"brand": brand, "status": "skipped", "reason": "brand_disabled",
                                      "batchId": batch_id, "dataDate": execution_date.isoformat()}
                continue
            business_config = get_brand_business_config(brand)
            if business_config.get("top_query_governance_enabled") is False:
                results[brand] = {"brand": brand, "status": "skipped", "reason": "governance_disabled",
                                  "batchId": batch_id, "dataDate": execution_date.isoformat()}
                continue
            scope_lock = None
            try:
                lock_dir = Path(os.getenv("TOP_QUERY_TASK_LOCK_DIR", "data/runtime/top_query_task_locks"))
                if not lock_dir.is_absolute():
                    lock_dir = Path(__file__).resolve().parents[2] / lock_dir
                scope_lock = _acquire_brand_scope_lock(brand, lock_dir, lock_wait_seconds)
                active = experiment_service.get_active_experiment_for_brand(
                    brand,
                    execution_date=execution_date,
                )
                if active is not None:
                    results[brand] = experiment_scheduler.run_brand(
                        brand,
                        data_date=execution_date,
                        batch_id=batch_id,
                    )
                    if results[brand].get("status") in {"completed", "skipped", "error"}:
                        persist_overview_summary(brand)
                    workflow = workflows.get(brand)
                    if (results[brand].get("status") == "running"
                            and getattr(workflow, "invocation_yielded", False) is True):
                        continuations[brand] = workflow
                    continue

                selection_service = (
                    selection_service_factory(brand)
                    if selection_service_factory
                    else build_query_selection_service(
                        brand,
                        session_factory=session_factory,
                        candidate_source=candidate_source,
                    )
                )
                check_modes = check_modes_for_date(_raw_config, brand, execution_date) or {}
                selection_config = replace(
                    QuerySelectionConfig.from_mapping(business_config),
                    reorder_enabled=bool(check_modes.get("open_factor", True)),
                    check_modes=check_modes,
                )
                workflow = dated_workflow_factory(brand)
                should_continue = getattr(workflow, "should_continue", None)
                if callable(should_continue) and not should_continue():
                    _LOGGER.warning(
                        "query governance brand skipped mid-run, scheduler stopping brand=%s batch=%s",
                        brand,
                        batch_id,
                    )
                    results[brand] = {"brand": brand, "status": "skipped", "reason": "scheduler_stopping",
                                      "batchId": batch_id, "dataDate": execution_date.isoformat()}
                    continue
                batch, runs = selection_service.select_and_start(
                    batch_id=batch_id,
                    brand=brand,
                    data_date=execution_date,
                    config=selection_config,
                    workflow=workflow,
                )
                provider = metric_provider_factory(brand) if metric_provider_factory else None
                should_materialize = (
                    metric_provider_factory is not None
                    or (selection_service_factory is None and candidate_source is None)
                )
                materialized_count = (
                    _materialize_tracked_query_metrics_best_effort(
                        brand,
                        execution_date,
                        session_factory,
                        metric_provider=provider,
                    )
                    if should_materialize else 0
                )
                error_count = sum(
                    getattr(run, "final_status", None) == "error"
                    for run in (runs or {}).values()
                )
                checkpoint_store = getattr(workflow, "checkpoint", None)
                persisted_status = (checkpoint_store.get_checkpoint(batch.batch_id).status
                                    if checkpoint_store is not None and batch.selected else None)
                results[brand] = {
                    "brand": brand,
                    "status": ("running" if persisted_status == "running"
                               else "error" if error_count else ("completed" if batch.selected else "skipped")),
                    "reason": "no_candidates" if not batch.selected and not error_count else None,
                    "batchId": batch.batch_id,
                    "dataDate": execution_date.isoformat(),
                    "selectedCount": len(batch.selected),
                    "candidateCount": len(batch.candidates),
                    "errorCount": error_count,
                    "materializedMetricCount": materialized_count,
                }
                if results[brand]["status"] in {"completed", "skipped", "error"}:
                    persist_overview_summary(brand)
                elif getattr(workflow, "invocation_yielded", False) is True:
                    continuations[brand] = workflow
            except ProcessLockError:
                _LOGGER.warning(
                    "query governance brand skipped for the day, brand lock busy brand=%s batch=%s",
                    brand,
                    batch_id,
                )
                results[brand] = {"brand": brand, "status": "skipped", "reason": "batch_busy",
                                  "batchId": batch_id, "dataDate": execution_date.isoformat()}
            except Exception as exc:
                _LOGGER.exception(
                    "query governance mainline brand failed brand=%s batch=%s",
                    brand,
                    batch_id,
                )
                results[brand] = {
                    "brand": brand,
                    "status": "error",
                    "errorCode": "QUERY_GOVERNANCE_MAINLINE_FAILED",
                    "errorMessage": str(exc) or exc.__class__.__name__,
                }
            finally:
                if scope_lock is not None:
                    if brand in continuations:
                        held_scope_locks[brand] = scope_lock
                    else:
                        scope_lock.release()

        # One slice per brand in each round keeps a large batch from delaying
        # every other brand.  Selection, controls and tracked metrics were already
        # captured above; continuing never calls their daily preparation again.
        while continuations:
            reload_config()
            next_round: dict[str, Any] = {}
            for brand, workflow in continuations.items():
                outcome = results[brand]
                batch_id = outcome["batchId"]
                if not is_brand_enabled(brand) or get_brand_business_config(brand).get("top_query_governance_enabled") is False:
                    outcome.update(status="skipped", reason=("brand_disabled" if not is_brand_enabled(brand) else "governance_disabled"))
                    held_scope_locks.pop(brand).release()
                    continue
                should_continue = getattr(workflow, "should_continue", None)
                if callable(should_continue) and not should_continue():
                    held_scope_locks.pop(brand).release()
                    continue
                try:
                    before_status, before_completed = _workflow_progress(session_factory, batch_id)
                    runs = workflow.resume_yielded_batch()
                    after_status, after_completed = _workflow_progress(session_factory, batch_id)
                    error_count = sum(getattr(run, "final_status", None) == "error" for run in runs.values())
                    outcome.update(status=("running" if after_status == "running" else "error" if error_count else "completed"),
                                   errorCount=error_count)
                    if outcome["status"] in {"completed", "error"}:
                        persist_overview_summary(brand)
                        lock = held_scope_locks.pop(brand, None)
                        if lock is not None:
                            lock.release()
                    elif (getattr(workflow, "invocation_yielded", False) is True
                            and (after_completed > before_completed or after_status != before_status)):
                        next_round[brand] = workflow
                    else:
                        _LOGGER.info("query governance continuation stopped without node progress brand=%s batch=%s", brand, batch_id)
                        held_scope_locks.pop(brand).release()
                except ProcessLockError:
                    _LOGGER.warning(
                        "query governance continuation brand skipped, brand lock busy brand=%s batch=%s",
                        brand,
                        batch_id,
                    )
                    outcome.update(status="skipped", reason="batch_busy")
                except Exception as exc:
                    _LOGGER.exception(
                        "query governance continuation brand failed brand=%s batch=%s",
                        brand,
                        batch_id,
                    )
                    outcome.update(status="error", errorCode="QUERY_GOVERNANCE_MAINLINE_FAILED",
                                   errorMessage=str(exc) or exc.__class__.__name__)
                finally:
                    if outcome.get("status") in {"error", "skipped"}:
                        lock = held_scope_locks.pop(brand, None)
                        if lock is not None:
                            lock.release()
            continuations = next_round
    finally:
        for lock in held_scope_locks.values():
            lock.release()
    has_errors = any(item.get("status") == "error" for item in results.values())
    failed_brands = [
        f"{item.get('brand')}: {item.get('errorCode') or 'UNKNOWN'} {item.get('errorMessage') or ''}".strip()
        for item in results.values()
        if item.get("status") == "error"
    ]
    # brand_disabled 是长期配置性禁用，排除掉避免每天噪音；其余跳过原因
    # （batch_busy/scheduler_stopping/no_candidates/governance_disabled）值得留痕。
    skipped_brands = [
        f"{item.get('brand')}:{item.get('reason')}"
        for item in results.values()
        if item.get("status") == "skipped" and item.get("reason") not in (None, "brand_disabled")
    ]
    return {
        "dataDate": execution_date.isoformat(),
        "status": "error" if has_errors else "success",
        # The scheduler executor copies these into the run's terminal summary;
        # without them a brand failure shows up as a bare "job failed".
        "error_code": "QUERY_GOVERNANCE_MAINLINE_FAILED" if has_errors else "",
        "error_message": "; ".join(failed_brands)[:1000],
        "skippedBrands": skipped_brands,
        "brands": results,
    }


def query_governance_recovery_job(
    *,
    workflow_factory: Callable[[str], Any],
    session_factory: Callable[[], Any] = SessionLocal,
    now: dt.datetime | None = None,
    stale_after_minutes: int = QUERY_GOVERNANCE_RECOVERY_STALE_MINUTES,
    limit: int = 10,
) -> dict[str, Any]:
    """Resume checkpointed governance batches abandoned by a worker restart.

    A workflow heartbeat is updated when a node begins or reaches a terminal
    state.  Only runs with an old heartbeat are eligible, so a healthy long
    running model call is not duplicated by this bounded recovery pass.
    """

    if stale_after_minutes < 1:
        raise ValueError("stale_after_minutes must be positive")
    if limit < 1:
        raise ValueError("limit must be positive")
    observed_at = now or dt.datetime.now(dt.timezone.utc)
    cutoff = observed_at.replace(tzinfo=None) - dt.timedelta(minutes=stale_after_minutes)
    workflow_run = QueryGovernanceWorkflowRun
    task = QueryGovernanceTask
    query = (
        select(workflow_run.batch_id, task.brand, task.data_date)
        .select_from(workflow_run)
        .join(task, task.batch_id == workflow_run.batch_id)
        .where(
            workflow_run.status == "running",
            or_(
                workflow_run.heartbeat_at < cutoff,
                cast(case((func.json_valid(workflow_run.checkpoint_payload) == 1,
                      func.json_extract(workflow_run.checkpoint_payload, "$.invocation_yielded")),
                     else_=0), String).in_(("true", "1")),
                and_(
                    workflow_run.heartbeat_at.is_(None),
                    workflow_run.started_at < cutoff,
                ),
            ),
        )
        .group_by(
            workflow_run.id,
            workflow_run.batch_id,
            task.brand,
            task.data_date,
        )
        .order_by(func.min(workflow_run.heartbeat_at).asc(), workflow_run.id.asc())
        .limit(limit)
    )
    with session_factory() as session:
        candidates = [
            (str(batch_id), str(brand), data_date)
            for batch_id, brand, data_date in session.execute(query).all()
        ]

    failures: list[dict[str, Any]] = []
    resumed_count = 0
    skips: list[dict[str, Any]] = []
    no_progress: list[dict[str, Any]] = []
    for batch_id, brand, data_date in candidates:
        try:
            before_status, before_completed = _workflow_progress(session_factory, batch_id)
            result = query_governance_mainline_job(
                brand_filter=brand,
                target_date=data_date,
                workflow_factory=workflow_factory,
                session_factory=session_factory,
                brand_lock_wait_seconds=0,
            )
            outcome = result.get("brands", {}).get(brand, {}) if isinstance(result, Mapping) else {}
            scope = {"batchId": batch_id, "brand": brand, "dataDate": data_date.isoformat()}
            if outcome.get("batchId") == batch_id and outcome.get("status") == "skipped":
                skips.append({**scope, "reason": outcome.get("reason") or "skipped"})
                continue
            if not isinstance(result, Mapping) or outcome.get("batchId") != batch_id or outcome.get("status") == "error":
                failures.append({
                    "batchId": batch_id,
                    "brand": brand,
                    "dataDate": data_date.isoformat(),
                    "errorCode": "QUERY_GOVERNANCE_RECOVERY_FAILED",
                })
                continue
            after_status, after_completed = _workflow_progress(session_factory, batch_id)
            if not (after_completed > before_completed or (
                    before_status == "running" and after_status in {"completed", "error"})):
                no_progress.append({**scope, "reason": "checkpoint_not_advanced"})
                continue
            resumed_count += 1
        except Exception as exc:
            _LOGGER.exception(
                "query governance recovery failed batch=%s brand=%s data_date=%s",
                batch_id,
                brand,
                data_date,
            )
            failures.append({
                "batchId": batch_id,
                "brand": brand,
                "dataDate": data_date.isoformat(),
                "errorCode": "QUERY_GOVERNANCE_RECOVERY_FAILED",
                "errorMessage": str(exc) or exc.__class__.__name__,
            })

    return {
        "status": "partial_failed" if failures else "success",
        "candidateCount": len(candidates),
        "resumedCount": resumed_count,
        "failedCount": len(failures),
        "failures": failures,
        "skippedCount": len(skips),
        "skips": skips,
        "noProgressCount": len(no_progress),
        "noProgress": no_progress,
    }


def query_governance_remediation_job(
    brand_filter: str | None = None,
    target_date: dt.date | None = None,
    *,
    workflow_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Advance async root-cause plans, daily retag, and delayed verification.

    This job is safe to run repeatedly.  Async tasks, action plans, the retag
    write, and the verification summary all have persisted idempotency facts.
    """
    if workflow_factory is None:
        raise RuntimeError("QUERY_GOVERNANCE_WORKFLOW_FACTORY_REQUIRED")
    reload_config()
    execution_date = target_date or (dt.date.today() - dt.timedelta(days=1))
    brands = [brand_filter] if brand_filter else list(BRAND_CONFIGS.keys())
    results: dict[str, Any] = {}
    for brand in brands:
        if not brand or not is_brand_enabled(brand):
            continue
        business_config = get_brand_business_config(brand)
        if business_config.get("top_query_governance_enabled") is False:
            results[brand] = {"status": "skipped", "reason": "disabled"}
            continue
        try:
            workflow = workflow_factory(brand)
            worker = getattr(workflow, "kg_worker", None)
            kg_results = worker.process_pending(limit=100) if worker is not None else []
            retag_service = getattr(workflow, "retag_verification", None)
            if retag_service is None:
                raise RuntimeError("QUERY_GOVERNANCE_RETAG_SERVICE_REQUIRED")
            retag = retag_service.trigger_if_ready(brand, execution_date)
            verification = retag_service.verify_if_due(brand, execution_date)
            results[brand] = {
                "status": "completed",
                "kgTaskCount": len(kg_results),
                "retag": retag,
                "verification": verification,
            }
        except Exception as exc:
            results[brand] = {
                "status": "error",
                "errorCode": "QUERY_GOVERNANCE_REMEDIATION_FAILED",
                "errorMessage": str(exc) or exc.__class__.__name__,
            }
    return {
        "dataDate": execution_date.isoformat(),
        "status": "error" if any(item.get("status") == "error" for item in results.values()) else "success",
        "brands": results,
    }


def query_governance_summary_reconciliation_job(
    brand_filter: str | None = None,
    *,
    session_factory: Callable[[], Any] = SessionLocal,
    summary_materializer: Callable[[str, dt.date], Any] | None = None,
    lookback_days: int = 180,
    limit: int = 20,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Backfill summaries for finished batches whose summary is missing or stale.

    Workflow completion and summary materialization are separate durable facts.
    The scheduler may therefore finish the batch while a pod restart or lost
    heartbeat prevents the asynchronous summary write.  Summary payloads from
    before the Query rail filter index was added, or from an older Query quality
    metrics definition, are also stale and need one idempotent rebuild.  The
    window covers the metric-trend range (180 days) because the operations
    drawer reads daily snapshots for pass rate and loss-rate trends.  This job
    skips summaries that already carry the current index and metrics version,
    and invokes the same read-service materializer as the mainline job.  It
    deliberately excludes running and ``execution_unknown`` workflow runs.
    """
    if lookback_days < 1:
        raise ValueError("summary reconciliation lookback_days must be positive")
    if limit < 1:
        raise ValueError("summary reconciliation limit must be positive")
    cutoff = (now or dt.datetime.now(dt.timezone.utc)).replace(tzinfo=None) - dt.timedelta(days=lookback_days)
    summary_model = QueryGovernanceBatchSummary
    query = (
        select(
            QueryGovernanceWorkflowRun.batch_id,
            QueryGovernanceTask.brand,
            QueryGovernanceTask.data_date,
            QueryGovernanceWorkflowRun.finished_at,
            QueryGovernanceWorkflowRun.id,
        )
        .select_from(QueryGovernanceWorkflowRun)
        .join(
            QueryGovernanceTask,
            QueryGovernanceTask.batch_id == QueryGovernanceWorkflowRun.batch_id,
        )
        .outerjoin(
            summary_model,
            and_(
                summary_model.batch_id == QueryGovernanceWorkflowRun.batch_id,
                summary_model.brand == QueryGovernanceTask.brand,
            ),
        )
        .where(
            QueryGovernanceWorkflowRun.status.in_(("completed", "error")),
            QueryGovernanceWorkflowRun.finished_at.is_not(None),
            QueryGovernanceWorkflowRun.finished_at >= cutoff,
            or_(
                summary_model.id.is_(None),
                summary_model.payload_json.is_(None),
                ~summary_model.payload_json.like('%"filterTaskIds"%'),
                ~summary_model.payload_json.like(quality_metrics_payload_marker()),
            ),
        )
        .distinct()
        .order_by(
            QueryGovernanceWorkflowRun.finished_at.desc(),
            QueryGovernanceWorkflowRun.id.desc(),
            QueryGovernanceTask.brand.asc(),
        )
        .limit(limit)
    )
    if brand_filter:
        query = query.where(QueryGovernanceTask.brand == brand_filter)

    with session_factory() as session:
        candidates = [
            (str(batch_id), str(brand), data_date)
            for batch_id, brand, data_date, _finished_at, _run_id in session.execute(query).all()
        ]

    materialize = summary_materializer
    if materialize is None:
        service = QueryGovernanceReadService(session_factory)
        materialize = lambda brand, data_date, batch_id: service.materialize_overview_summary(
            brand,
            data_date=data_date,
            batch_id=batch_id,
        )

    failures: list[dict[str, Any]] = []
    repaired_count = 0
    for batch_id, brand, data_date in candidates:
        try:
            if summary_materializer is None:
                materialize(brand, data_date, batch_id)
            else:
                materialize(brand, data_date)
            repaired_count += 1
        except Exception as exc:
            _LOGGER.exception(
                "query governance summary reconciliation failed batch=%s brand=%s data_date=%s",
                batch_id,
                brand,
                data_date,
            )
            failures.append({
                "batchId": batch_id,
                "brand": brand,
                "dataDate": data_date.isoformat(),
                "errorType": exc.__class__.__name__,
            })

    return {
        "status": "partial_failed" if failures else "success",
        "lookbackDays": lookback_days,
        "candidateCount": len(candidates),
        "repairedCount": repaired_count,
        "failedCount": len(failures),
        "failures": failures,
    }


def _snapshot_image_observations(snapshot_text: Any) -> list[dict[str, Any]]:
    """Flatten one S0 snapshot into image-audit observation rows."""
    if isinstance(snapshot_text, str):
        try:
            snapshot = json.loads(snapshot_text)
        except (TypeError, ValueError):
            return []
    else:
        snapshot = snapshot_text
    if not isinstance(snapshot, Mapping):
        return []
    products = snapshot.get("products") or []
    return [
        {
            "source": "snapshot",
            "product_id": item.get("product_id") or item.get("productId"),
            "image_url": item.get("image_url") or item.get("imageUrl"),
        }
        for item in products
        if isinstance(item, Mapping)
    ]


def query_governance_image_integrity_job(
    brand_filter: str | None = None,
    *,
    session_factory: Callable[[], Any] = SessionLocal,
    notifier: Callable[[str, str], Any] | None = None,
    lookback_days: int = 3,
    task_limit: int = 200,
    shared_alert_min_products: int = 3,
) -> dict[str, Any]:
    """Daily read-only image integrity audit with Feishu alerting.

    错误 mainImage 的写入源头在搜索索引侧，无法阻止再次发生；本任务只做
    发现：共享图分组、缺图比例、资产表自洽性全部只读，命中阈值即推送告警。
    """
    reload_config()
    brands = [brand_filter] if brand_filter else list(BRAND_CONFIGS.keys())
    since = dt.date.today() - dt.timedelta(days=max(1, lookback_days))
    results: dict[str, Any] = {}
    for brand in brands:
        if not brand or not is_brand_enabled(brand):
            continue
        business_config = get_brand_business_config(brand)
        if business_config.get("top_query_governance_enabled") is False:
            results[brand] = {"status": "skipped", "reason": "disabled"}
            continue
        try:
            with session_factory() as session:
                task_rows = session.scalars(
                    select(QueryGovernanceTask)
                    .where(
                        QueryGovernanceTask.brand == brand,
                        QueryGovernanceTask.data_date >= since,
                    )
                    .order_by(QueryGovernanceTask.id.desc())
                    .limit(task_limit)
                ).all()
                asset_rows = session.scalars(
                    select(QueryGovernanceImageAsset).where(QueryGovernanceImageAsset.brand == brand)
                ).all()
            observations: list[dict[str, Any]] = []
            for task_row in task_rows:
                observations.extend(_snapshot_image_observations(task_row.s0_snapshot))
            asset_mismatches = asset_records_mismatch(
                [
                    {
                        "product_id": row.product_id,
                        "source_url": row.source_url,
                        "storage_key": row.storage_key,
                        "cached_url": row.cached_url,
                    }
                    for row in asset_rows
                ]
            )
            report = image_health_report(
                observations,
                asset_mismatches=asset_mismatches,
                shared_alert_min_products=shared_alert_min_products,
            )
            results[brand] = {
                "status": report["verdict"],
                "productCount": report["product_count"],
                "sharedGroupCount": report["shared_group_count"],
                "sharedProductCount": report["shared_product_count"],
                "missingImageRatio": report["missing_image_ratio"],
                "assetMismatchCount": report["asset_mismatch_count"],
                "alerts": report["alerts"],
            }
            if report["alerts"]:
                if notifier is None:
                    from src.runtime.notifier import send_feishu_notification as notifier
                notifier(
                    f"[{brand}] " + "\n".join(render_alert_lines(report)),
                    "Query治理商品图完整性告警",
                )
        except Exception as exc:
            results[brand] = {
                "status": "error",
                "errorCode": "QUERY_GOVERNANCE_IMAGE_INTEGRITY_FAILED",
                "errorMessage": str(exc) or exc.__class__.__name__,
            }
    brand_statuses = {str(item.get("status")) for item in results.values()}
    overall = "error" if "error" in brand_statuses else ("alert" if "ALERT" in brand_statuses else "success")
    return {
        "since": since.isoformat(),
        "lookbackDays": lookback_days,
        "status": overall,
        "brands": results,
    }


__all__ = [
    "QueryGovernanceCandidateSource",
    "build_query_selection_service",
    "query_governance_remediation_job",
    "query_governance_mainline_job",
    "query_governance_recovery_job",
    "query_governance_summary_reconciliation_job",
    "query_governance_image_integrity_job",
]
