"""Retained compatibility handlers for Query Governance operations and experiments."""

from __future__ import annotations

from fastapi import Query, Body, HTTPException, Depends
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from typing import Optional, Dict, Any
import datetime
import json
from sqlalchemy.exc import DatabaseError
from src.persistence import SessionLocal, User, TopQueryActionPlan, TopQueryExperiment, TopQueryTask, ACTION_EXECUTE_GOVERNANCE_ACTIONS, ACTION_MANAGE_BRAND_CONFIG
from src.governance.action_gateway import ActionAuthorizationError, ActionConflictError, ActionExecutionFailedError, ActionGateway, ActionInProgressError, ActionIntent, ExecutionContext
from src.runtime.release_info import get_release_info
from src.config import get_brand_business_config, get_env
from src.top_query.service import ActionExecutionError, ActionStateConflictError, ActionValidationError, LegacyQueryDemotionContractError, TopQueryGovernanceService, _bounded_action_response_payload, sanitize_action_error
from src.top_query.query_demotion_contract import LEGACY_QUERY_DEMOTION_BLOCK_REASON, should_block_legacy_query_demotion, uses_authoritative_query_demotion
from src.top_query.task_lock import TopQueryTaskLockBusy, top_query_brand_governance_lock, top_query_task_lock
from src.governance.query_policy import ExperimentValidationError, assert_query_governance_allowed
from src.top_query.experiment_service import TopQueryExperimentService
from src.top_query.experiment_analytics import ExperimentAnalyticsService
from src.top_query.experiment_export import ExperimentExportService
from src.top_query.experiment_report_snapshot import ReportSnapshotTokenError, decode_report_snapshot, encode_report_snapshot
from fastapi import APIRouter
import logging
from ..dependencies import *  # noqa: F401,F403
from ..shared import *  # noqa: F401,F403
from .actions import *  # noqa: F401,F403
from .query_governance_support import *  # noqa: F401,F403
from .query_governance_support import _top_query_event_payload

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/api/dashboard/top-query/overview")
def get_top_query_overview(
    date: Optional[datetime.date] = Query(None),
    user: User = Depends(get_current_user),
):
    brands = _user_brand_permissions(user)
    data_date = date or _top_query_data_date_for_monitoring_date(date)
    db = SessionLocal()
    try:
        metrics_by_brand = _query_brand_metrics_by_brand(db, brands, data_date)
        tasks_by_brand = _latest_top_query_tasks_by_brand(
            db,
            brands,
            metrics_by_brand,
            data_date=data_date,
        )
        all_tasks = [task for brand in brands for task in tasks_by_brand.get(brand, [])]
        all_task_ids = [task.id for task in all_tasks]
        steps_by_task = _safe_latest_steps_by_task(
            db,
            all_task_ids,
            context=f"overview:all:{data_date.isoformat() if data_date else 'latest'}",
        )
        actions_by_task = _safe_top_query_actions_by_task(
            db,
            all_task_ids,
            context=f"overview-actions:all:{data_date.isoformat() if data_date else 'latest'}",
        )
        event_aggregates_by_task = _safe_top_query_event_aggregates_by_task(
            db,
            all_task_ids,
            context=f"overview-events:all:{data_date.isoformat() if data_date else 'latest'}",
        )
        metric_7d_by_brand = _brand_metric_7d_payloads_by_brand(db, brands, metrics_by_brand)
        items = []
        for brand in brands:
            latest_metric = metrics_by_brand.get(brand)
            tasks = tasks_by_brand.get(brand, [])
            active_experiment = _active_top_query_experiment_summary(db, brand)
            human_action_task_ids: set[int] = set()
            auto_action_task_ids: set[int] = set()
            duration_seconds = 0.0
            token_usage = 0
            latest_event_time = None
            execution_date = (
                date.isoformat()
                if date
                else latest_metric.data_date.isoformat()
                if latest_metric and latest_metric.data_date
                else None
            )
            for task in tasks:
                task_actions = actions_by_task.get(task.id, [])
                if any(
                    action.execution_mode == "human_approval"
                    and action.status in {"pending", "waiting_human_approval", "pending_confirmation", "approved"}
                    for action in task_actions
                ):
                    human_action_task_ids.add(task.id)
                if any(
                    action.execution_mode == "auto"
                    and action.status in {"executed", "verified", "completed"}
                    for action in task_actions
                ):
                    auto_action_task_ids.add(task.id)
                aggregate = event_aggregates_by_task.get(task.id) or {}
                duration_seconds += float(aggregate.get("duration") or 0)
                token_usage += int(aggregate.get("token_usage") or 0)
                aggregate_latest = aggregate.get("latest_event_time")
                if aggregate_latest and (latest_event_time is None or aggregate_latest > latest_event_time):
                    latest_event_time = aggregate_latest
                if latest_event_time and not date:
                    execution_date = latest_event_time.date().isoformat()
            auto_completed_count = sum(
                1 for task in tasks
                if task.id in auto_action_task_ids and task.id not in human_action_task_ids
            )
            needs_governance_count = sum(
                1 for task in tasks
                if not _is_top_query_invalidated(task)
                and task.display_status not in {"normal", "fixed"}
                and task.display_status != "fixed"
            )
            top_query_pv_share = _top_query_traffic_coverage(tasks, latest_metric)
            items.append({
                "brand": brand,
                "dataDate": latest_metric.data_date.isoformat() if latest_metric and latest_metric.data_date else None,
                "executionDate": execution_date,
                **_brand_metric_payload(latest_metric),
                **metric_7d_by_brand.get(brand, _brand_metric_7d_payload(db, brand, latest_metric)),
                "topQueryCount": len(tasks),
                "topQueryPvShare": top_query_pv_share or 0,
                "needsGovernanceCount": needs_governance_count,
                "autoCompletedCount": auto_completed_count,
                "manualRequiredCount": sum(1 for task in tasks if task.status == "manual_required" or task.display_status == "waiting_human_approval"),
                "durationSeconds": duration_seconds,
                "tokenUsage": token_usage,
                "experiment": active_experiment,
            })
        return {"brands": items}
    finally:
        db.close()

@router.get("/api/dashboard/top-query/brands/{brand}")
def get_top_query_brand_detail(
    brand: str,
    date: Optional[datetime.date] = Query(None),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_brand_permission(brand, user)
    data_date = _top_query_data_date_for_monitoring_date(date)
    db = SessionLocal()
    try:
        context = _top_query_brand_detail_context(db, brand, data_date)
        latest_metric = context["latest_metric"]
        tasks = context["tasks"]
        query_payloads = context["query_payloads"]
        active_experiment = context["active_experiment"]
        trend = _top_query_brand_metric_trend(db, brand, latest_metric.data_date if latest_metric else data_date)
        stats_payload = _top_query_brand_detail_stats_payload(
            db,
            brand,
            latest_metric,
            tasks,
            context["steps_by_task"],
            context["actions_by_task"],
            context["event_totals"],
            query_payloads=query_payloads,
            metric_7d_payload=context["metric_7d_payload"],
        )
        if active_experiment:
            stats_payload["experimentMode"] = True
        return {
            "brand": brand,
            "dataDate": (
                latest_metric.data_date.isoformat()
                if latest_metric and latest_metric.data_date
                else data_date.isoformat() if data_date else None
            ),
            "executionDate": (
                _top_query_execution_date_payload(date, latest_metric, data_date)
            ),
            "stats": stats_payload,
            "trend": trend,
            "queries": query_payloads,
            "experiment": active_experiment,
        }
    finally:
        db.close()


@router.get("/api/dashboard/top-query/brands/{brand}/summary")
def get_top_query_brand_summary(
    brand: str,
    date: Optional[datetime.date] = Query(None),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_brand_permission(brand, user)
    data_date = _top_query_data_date_for_monitoring_date(date)
    db = SessionLocal()
    try:
        context = _top_query_brand_detail_context(db, brand, data_date)
        latest_metric = context["latest_metric"]
        tasks = context["tasks"]
        active_experiment = context["active_experiment"]
        stats_payload = _top_query_brand_detail_stats_payload(
            db,
            brand,
            latest_metric,
            tasks,
            context["steps_by_task"],
            context["actions_by_task"],
            context["event_totals"],
            query_payloads=context["query_payloads"],
            metric_7d_payload=context["metric_7d_payload"],
        )
        if active_experiment:
            stats_payload["experimentMode"] = True
        return {
            "brand": brand,
            "dataDate": (
                latest_metric.data_date.isoformat()
                if latest_metric and latest_metric.data_date
                else data_date.isoformat() if data_date else None
            ),
            "executionDate": (
                _top_query_execution_date_payload(date, latest_metric, data_date)
            ),
            "stats": stats_payload,
            "experiment": active_experiment,
        }
    finally:
        db.close()


@router.get("/api/dashboard/top-query/brands/{brand}/queries")
def get_top_query_brand_queries(
    brand: str,
    date: Optional[datetime.date] = Query(None),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_brand_permission(brand, user)
    data_date = _top_query_data_date_for_monitoring_date(date)
    db = SessionLocal()
    try:
        context = _top_query_brand_detail_context(db, brand, data_date)
        latest_metric = context["latest_metric"]
        tasks = context["tasks"]
        return {
            "brand": brand,
            "dataDate": (
                latest_metric.data_date.isoformat()
                if latest_metric and latest_metric.data_date
                else data_date.isoformat() if data_date else None
            ),
            "executionDate": (
                _top_query_execution_date_payload(date, latest_metric, data_date)
            ),
            "queries": context["query_payloads"],
            "total": len(tasks),
            "experiment": context["active_experiment"],
        }
    finally:
        db.close()


@router.get("/api/dashboard/top-query/brands/{brand}/trend")
def get_top_query_brand_trend(
    brand: str,
    date: Optional[datetime.date] = Query(None),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_brand_permission(brand, user)
    data_date = _top_query_data_date_for_monitoring_date(date)
    db = SessionLocal()
    try:
        latest_metric = _query_brand_metric(db, brand, data_date)
        return {
            "brand": brand,
            "dataDate": (
                latest_metric.data_date.isoformat()
                if latest_metric and latest_metric.data_date
                else data_date.isoformat() if data_date else None
            ),
            "trend": _top_query_brand_metric_trend(db, brand, data_date),
        }
    finally:
        db.close()

@router.get("/api/dashboard/top-query/tasks")
def get_top_query_tasks(
    brand: Optional[str] = None,
    status_value: Optional[str] = Query(None, alias="status"),
    user: User = Depends(get_current_user),
):
    if brand:
        _ensure_top_query_brand_permission(brand, user)
        brands = [brand]
    else:
        brands = _user_brand_permissions(user)
    db = SessionLocal()
    try:
        metrics_by_brand = _query_brand_metrics_by_brand(db, brands)
        tasks_by_brand = _latest_top_query_tasks_by_brand(
            db,
            brands,
            metrics_by_brand,
            status_value=status_value,
        )
        tasks = [task for allowed_brand in brands for task in tasks_by_brand.get(allowed_brand, [])]
        tasks.sort(key=lambda task: task.search_pv or 0, reverse=True)
        task_ids = [task.id for task in tasks]
        steps_by_task = _safe_latest_steps_by_task(db, task_ids, context=f"task-list:{brand or 'multi-brand'}:{status_value or 'all'}")
        actions_by_task = _safe_top_query_actions_by_task(db, task_ids, context=f"task-list-actions:{brand or 'multi-brand'}:{status_value or 'all'}")
        return {
            "items": [
                _top_query_task_payload(
                    task,
                    steps_by_task.get(task.id, []),
                    actions_by_task.get(task.id, []),
                )
                for task in tasks
            ],
            "total": len(tasks),
        }
    finally:
        db.close()

@router.get("/api/dashboard/top-query/queries/{task_id}")
def get_top_query_detail(task_id: int, user: User = Depends(get_current_user)):
    task = _get_top_query_task_or_404(task_id, user)
    db = SessionLocal()
    try:
        business_config = get_brand_business_config(task.brand)
        product_display_limit = int(business_config.get("top_query_check_product_limit") or 16)
        inventory_threshold_percent = float(business_config.get("top_query_inventory_broken_rate_threshold") or 25)
        actions = _safe_optional_query_all(
            db.query(TopQueryActionPlan).filter(TopQueryActionPlan.task_id == task_id).order_by(TopQueryActionPlan.id.asc()),
            context=f"task-detail-actions:{task.brand}:{task_id}",
        )
        steps = _safe_steps_for_task_detail(db, task_id, context=f"task-detail:{task.brand}:{task_id}")
        return {
            "task": _top_query_task_payload(task, list(_latest_steps_by_task(steps).get(task_id, steps)), actions),
            "actions": _top_query_action_payloads(actions),
            "stepStatuses": _merged_top_query_step_payloads(list(_latest_steps_by_task(steps).get(task_id, steps))),
            "productDisplayLimit": product_display_limit,
            "inventoryBrokenRateThreshold": inventory_threshold_percent / 100.0,
            "trend": [],
        }
    finally:
        db.close()


@router.get("/api/dashboard/top-query/queries/{task_id}/trend")
def get_top_query_query_trend(task_id: int, user: User = Depends(get_current_user)):
    task = _get_top_query_task_or_404(task_id, user)
    db = SessionLocal()
    try:
        return {
            "taskId": task_id,
            "brand": task.brand,
            "queryText": task.query_text,
            "trend": _top_query_query_trend(db, task),
        }
    finally:
        db.close()


@router.get("/api/dashboard/top-query/queries/{task_id}/current-result-count")
def get_top_query_current_result_count(task_id: int, user: User = Depends(get_current_user)):
    task = _get_top_query_task_or_404(task_id, user)
    business_config = get_brand_business_config(task.brand)
    product_display_limit = int(business_config.get("top_query_check_product_limit") or 16)
    cache_key = (task.brand, task.query_text, product_display_limit)
    cached = _top_query_cache_get("current_result_count", cache_key)
    if cached is not None:
        return {"taskId": task_id, **cached}
    try:
        live_result = service.search_products_with_total(task.brand, task.query_text, page_size=product_display_limit)
        if not isinstance(live_result, dict) or live_result.get("success") is not True:
            raise ValueError("search evidence unavailable")
        raw_products = live_result.get("products")
        raw_result_complete = live_result.get("result_complete")
        if (
            not isinstance(raw_products, list)
            or not all(isinstance(product, dict) for product in raw_products)
            or not isinstance(raw_result_complete, bool)
        ):
            raise ValueError("invalid search response")
        products = list(raw_products)
        raw_result_count = live_result.get("total_count")
        if raw_result_count is None:
            result_count = None
            result_count_known = False
        elif (
            isinstance(raw_result_count, int)
            and not isinstance(raw_result_count, bool)
            and raw_result_count >= len(products)
        ):
            result_count = raw_result_count
            result_count_known = True
        else:
            raise ValueError("invalid search total")
        if raw_result_complete and (
            result_count is None or result_count != len(products)
        ):
            raise ValueError("invalid complete search response")
    except Exception:
        result_count = None
        result_count_known = False
        products = []
    payload = {
        "resultCount": result_count,
        "resultCountKnown": result_count_known,
        "products": products,
    }
    _top_query_cache_set("current_result_count", cache_key, payload, ttl_seconds=30)
    return {"taskId": task_id, **payload}


@router.get("/api/dashboard/top-query/brands/{brand}/products/{product_id}")
def get_top_query_product_detail(
    brand: str,
    product_id: str,
    task_id: Optional[int] = Query(None, alias="taskId"),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_brand_permission(brand, user)
    if task_id is not None:
        task = _get_top_query_task_or_404(task_id, user)
        if task.brand != brand:
            raise HTTPException(status_code=400, detail="Product task does not belong to brand")
    cache_key = (brand, product_id, task_id)
    cached = _top_query_cache_get("product_detail", cache_key)
    if cached is not None:
        return cached
    snapshot_product = None
    snapshot_source = None
    snapshot_captured_at = None
    if task_id is not None:
        snapshot_product, snapshot_source, snapshot_captured_at = _top_query_snapshot_product_for_task(task_id, product_id)
    try:
        products = service.get_product_details(brand, product_id)
    except Exception:
        products = []
    live_product = products[0] if products else None
    if live_product and snapshot_product:
        product = {**snapshot_product, **live_product}
        detail_source = "live_with_snapshot"
        is_snapshot = False
    elif live_product:
        product = live_product
        detail_source = "live"
        is_snapshot = False
    elif snapshot_product:
        product = snapshot_product
        detail_source = snapshot_source or "snapshot"
        is_snapshot = True
    else:
        raise HTTPException(status_code=404, detail="Query Governance product detail not found")
    payload = {
        "product": product,
        "detailMeta": {
            "detailSource": detail_source,
            "capturedAt": snapshot_captured_at,
            "isSnapshot": is_snapshot,
            "taskId": task_id,
        },
    }
    _top_query_cache_set("product_detail", cache_key, payload, ttl_seconds=300)
    return payload


@router.get("/api/dashboard/top-query/brands/{brand}/snapshot/validate")
def validate_top_query_snapshot(
    brand: str,
    date: Optional[str] = None,
    limit: Optional[int] = None,
    user: User = Depends(get_current_user),
):
    _ensure_top_query_brand_permission(brand, user)
    monitoring_date = _parse_dashboard_date(date)
    cache_key = (brand, monitoring_date.isoformat(), int(limit) if limit is not None else None)
    cached = _top_query_cache_get("snapshot_validate", cache_key)
    if cached is not None:
        return cached
    payload = TopQueryGovernanceService().compare_snapshot_for_monitoring_date(brand, monitoring_date, limit)
    _top_query_cache_set("snapshot_validate", cache_key, payload, ttl_seconds=300)
    return payload


@router.post("/api/dashboard/top-query/brands/{brand}/snapshot/rebuild")
def rebuild_top_query_snapshot(
    brand: str,
    date: Optional[str] = None,
    limit: Optional[int] = None,
    request_payload: Dict = Body(default={}),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_brand_permission(brand, user)
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    monitoring_date = _parse_dashboard_date(date)
    data_date = monitoring_date - datetime.timedelta(days=1)
    operator = str(user.id or user.username or "unknown")
    normalized_limit = int(limit) if limit is not None else None
    request_id, business_payload = _admin_request_id(
        {
            **request_payload,
            "brand": brand,
            "monitoringDate": monitoring_date.isoformat(),
            "dataDate": data_date.isoformat(),
            "limit": normalized_limit,
        },
        action_type="top_query_snapshot_rebuild",
        operator=operator,
        target=f"{brand}:{monitoring_date.isoformat()}",
    )
    intent = ActionIntent(
        action_type="top_query_snapshot_rebuild",
        idempotency_key=request_id,
        payload=business_payload,
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        target=f"{brand}:{monitoring_date.isoformat()}",
    )
    result_holder: Dict[str, Any] = {}

    def execute(_context: ExecutionContext) -> Dict[str, Any]:
        result = TopQueryGovernanceService().rebuild_snapshot_for_monitoring_date(
            brand,
            monitoring_date,
            normalized_limit,
        )
        if not isinstance(result, dict):
            raise ValueError("top_query_snapshot_result_invalid")
        governed_result = {**result, "requestId": request_id}
        result_holder["value"] = governed_result
        return governed_result

    def readback(_context: ExecutionContext) -> Dict[str, Any]:
        result = result_holder.get("value")
        if not isinstance(result, dict):
            return {"status": "failed", "reason": "snapshot_rebuild_result_missing"}
        task_ids = result.get("taskIds")
        top_query_count = result.get("topQueryCount")
        if (
            result.get("brand") != brand
            or result.get("monitoringDate") != monitoring_date.isoformat()
            or result.get("dataDate") != data_date.isoformat()
            or not isinstance(task_ids, list)
            or any(
                isinstance(task_id, bool)
                or not isinstance(task_id, int)
                or task_id <= 0
                for task_id in task_ids
            )
            or len(set(task_ids)) != len(task_ids)
            or isinstance(top_query_count, bool)
            or not isinstance(top_query_count, int)
            or top_query_count != len(task_ids)
        ):
            return {"status": "failed", "reason": "snapshot_rebuild_result_invalid"}

        db = SessionLocal()
        try:
            active_task_ids = sorted(
                int(task.id)
                for task in db.query(TopQueryTask).filter(
                    TopQueryTask.brand == brand,
                    TopQueryTask.data_date == data_date,
                ).all()
                if str(task.status or "").strip().lower() != "superseded"
            )
        finally:
            db.close()
        expected_task_ids = sorted(task_ids)
        if active_task_ids != expected_task_ids:
            return {
                "status": "failed",
                "reason": "snapshot_rebuild_task_readback_mismatch",
            }
        return {
            "status": "success",
            "brand": brand,
            "monitoringDate": monitoring_date.isoformat(),
            "dataDate": data_date.isoformat(),
            "topQueryCount": top_query_count,
            "taskIds": task_ids,
            "requestId": request_id,
        }

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == brand
            and _has_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        )
    )
    try:
        payload = gateway.execute(
            intent,
            execute,
            readback=readback,
            atomic_local_transaction=False,
            authoritative_readback_on_success=True,
        )
    except ActionConflictError as exc:
        raise HTTPException(status_code=409, detail="Action idempotency conflict") from exc
    except ActionInProgressError as exc:
        raise HTTPException(status_code=409, detail="Action is still in progress") from exc
    except ActionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="Action authorization denied") from exc
    except ActionExecutionFailedError as exc:
        failure = exc.result if isinstance(exc.result, dict) else {}
        error = str(failure.get("error") or "top_query_snapshot_rebuild_failed")
        status_code = 502 if error == "top_query_snapshot_result_invalid" else 500
        raise HTTPException(status_code=status_code, detail="Query Governance snapshot rebuild failed") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="Action result contract invalid")
    if payload.get("status") in {"failed", "inconclusive"}:
        raise HTTPException(status_code=409, detail="Snapshot rebuild verification failed")

    target_date = data_date.isoformat()
    _top_query_cache_invalidate("snapshot_validate", lambda key: key[0] == brand and key[1] == monitoring_date.isoformat())
    _top_query_cache_invalidate("product_detail", lambda key: key[0] == brand)
    _top_query_cache_invalidate("current_result_count", lambda key: key[0] == brand)
    _top_query_cache_invalidate("snapshot_validate", lambda key: key[0] == brand and key[1] == target_date)
    return payload


@router.get("/api/dashboard/top-query/experiments/brands/{brand}")
def get_top_query_brand_experiments(
    brand: str,
    status_value: Optional[str] = Query(None, alias="status"),
    start_date: Optional[datetime.date] = Query(None, alias="startDate"),
    end_date: Optional[datetime.date] = Query(None, alias="endDate"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, alias="pageSize", ge=1, le=100),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_brand_permission(brand, user)
    return TopQueryExperimentService().list_history_summaries(
        brand,
        status=status_value,
        start_date=start_date,
        end_date=end_date,
        page=page,
        page_size=page_size,
    )


@router.get("/api/dashboard/top-query/experiments/brands/{brand}/sample-pool")
def get_top_query_experiment_sample_pool(
    brand: str,
    today: Optional[datetime.date] = Query(None),
    limit: int = Query(150, ge=1, le=500),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_brand_permission(brand, user)
    try:
        return TopQueryExperimentService().get_sample_pool(brand, today=today, limit=limit)
    except ExperimentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/dashboard/top-query/experiments/brands/{brand}/draft")
def create_top_query_experiment_draft(
    brand: str,
    payload: Dict = Body(...),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_brand_permission(brand, user)
    _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG)
    planned_end_date = payload.get("plannedEndDate")
    today = payload.get("today")
    try:
        return TopQueryExperimentService().create_or_replace_draft(
            brand=brand,
            created_by=user.username,
            experiment_name=str(payload.get("experimentName") or "").strip() or f"{brand} A/B Test",
            treatment_queries=payload.get("treatmentQueries") or [],
            control_queries=payload.get("controlQueries") or [],
            today=datetime.date.fromisoformat(today) if today else None,
            planned_end_date=datetime.date.fromisoformat(planned_end_date) if planned_end_date else None,
        )
    except ExperimentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/dashboard/top-query/experiments/{experiment_id}/schedule")
def schedule_top_query_experiment(
    experiment_id: int,
    user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        experiment = db.get(TopQueryExperiment, experiment_id)
        if not experiment:
            raise HTTPException(status_code=404, detail="Query Governance experiment not found")
        _ensure_top_query_brand_permission(experiment.brand, user)
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG)
    finally:
        db.close()
    try:
        return TopQueryExperimentService().schedule_experiment(experiment_id)
    except ExperimentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/dashboard/top-query/experiments/{experiment_id}/end")
def end_top_query_experiment(
    experiment_id: int,
    user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        experiment = db.get(TopQueryExperiment, experiment_id)
        if not experiment:
            raise HTTPException(status_code=404, detail="Query Governance experiment not found")
        _ensure_top_query_brand_permission(experiment.brand, user)
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG)
    finally:
        db.close()
    try:
        return TopQueryExperimentService().end_experiment(experiment_id)
    except ExperimentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/dashboard/top-query/experiments/{experiment_id}/cancel")
def cancel_top_query_experiment(
    experiment_id: int,
    user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        experiment = db.get(TopQueryExperiment, experiment_id)
        if not experiment:
            raise HTTPException(status_code=404, detail="Query Governance experiment not found")
        _ensure_top_query_brand_permission(experiment.brand, user)
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG)
    finally:
        db.close()
    try:
        return TopQueryExperimentService().cancel_experiment(experiment_id)
    except ExperimentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/dashboard/top-query/experiments/{experiment_id}")
def get_top_query_experiment_detail(
    experiment_id: int,
    as_of_date: Optional[datetime.date] = Query(None, alias="asOfDate"),
    user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        experiment = db.get(TopQueryExperiment, experiment_id)
        if not experiment:
            raise HTTPException(status_code=404, detail="Query Governance experiment not found")
        _ensure_top_query_brand_permission(experiment.brand, user)
    finally:
        db.close()
    return TopQueryExperimentService().get_experiment_detail(experiment_id, as_of_date=as_of_date)


@router.get("/api/dashboard/top-query/experiments/{experiment_id}/report")
def get_top_query_experiment_report(
    experiment_id: int,
    user: User = Depends(get_current_user),
):
    _ensure_top_query_experiment_permission(experiment_id, user)
    report = ExperimentAnalyticsService().get_report(experiment_id)
    try:
        snapshot_value = encode_report_snapshot(report, SECRET_KEY)
    except ReportSnapshotTokenError as exc:
        logger.error(
            "Experiment report snapshot encoding failed for experiment %s",
            experiment_id,
        )
        raise HTTPException(
            status_code=500,
            detail="experiment report snapshot failed",
        ) from exc
    return {**report, "reportSnapshotToken": snapshot_value}


@router.get("/api/dashboard/top-query/experiments/{experiment_id}/export")
def export_top_query_experiment(
    experiment_id: int,
    export_format: str = Query(..., alias="format"),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_experiment_permission(experiment_id, user)
    normalized_format = export_format.lower()
    if normalized_format not in {"csv", "zip", "xlsx"}:
        raise HTTPException(status_code=400, detail="format must be csv, zip or xlsx")
    try:
        content, media_type, filename = ExperimentExportService().export(
            experiment_id,
            normalized_format,
        )
    except Exception as exc:
        logger.error(
            "Experiment export failed for experiment %s: %s",
            experiment_id,
            sanitize_action_error(exc),
        )
        raise HTTPException(
            status_code=500,
            detail="experiment export failed",
        ) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/dashboard/top-query/experiments/{experiment_id}/export")
def export_top_query_experiment_snapshot(
    experiment_id: int,
    request: Dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
):
    _ensure_top_query_experiment_permission(experiment_id, user)
    export_format = request.get("format") if isinstance(request, dict) else None
    normalized_format = str(export_format or "").lower()
    if normalized_format not in {"csv", "zip", "xlsx"}:
        raise HTTPException(status_code=400, detail="format must be csv, zip or xlsx")
    snapshot_token = request.get("snapshotToken") if isinstance(request, dict) else None
    try:
        report = decode_report_snapshot(
            snapshot_token,
            SECRET_KEY,
            experiment_id,
        )
    except ReportSnapshotTokenError as exc:
        raise HTTPException(
            status_code=400,
            detail="invalid report snapshot token",
        ) from exc
    try:
        content, media_type, filename = ExperimentExportService().export_report(
            report,
            normalized_format,
            experiment_id,
        )
    except Exception as exc:
        logger.error(
            "Experiment snapshot export failed for experiment %s: %s",
            experiment_id,
            sanitize_action_error(exc),
        )
        raise HTTPException(
            status_code=500,
            detail="experiment export failed",
        ) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@router.get("/api/dashboard/top-query/queries/{task_id}/events")
def get_top_query_events(task_id: int, user: User = Depends(get_current_user)):
    _get_top_query_task_or_404(task_id, user)
    db = SessionLocal()
    try:
        events = _safe_top_query_events(db, task_id, context=f"top-query-events:{task_id}")
        items = []
        for event in events:
            payload, model_metrics = _top_query_event_payload(
                event.output_payload
            )
            item = {
                "id": event.id,
                "agentName": event.agent_name,
                "stage": event.stage,
                "eventType": event.event_type,
                "summary": event.summary,
                "payload": payload,
                "duration": event.duration,
                "createdAt": event.created_at.isoformat() if event.created_at else None,
            }
            if model_metrics is not None:
                if "tokenUsage" not in model_metrics:
                    model_metrics["tokenUsage"] = int(event.token_usage or 0)
                if "durationSeconds" not in model_metrics:
                    model_metrics["durationSeconds"] = float(event.duration or 0)
                item["modelMetrics"] = model_metrics
            items.append(item)
        return {"items": items}
    finally:
        db.close()

def _get_top_query_action_or_404(action_id: int, user: User) -> TopQueryActionPlan:
    db = SessionLocal()
    try:
        action = db.get(TopQueryActionPlan, action_id)
        if not action:
            raise HTTPException(status_code=404, detail="Query Governance action not found")
        _ensure_top_query_brand_permission(action.brand, user)
        db.expunge(action)
        return action
    finally:
        db.close()


def _raise_top_query_action_http_error(exc: Exception) -> None:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, LegacyQueryDemotionContractError):
        raise HTTPException(
            status_code=409,
            detail={
                "blockedReason": exc.blocked_reason,
                "requiresRebuild": exc.requires_rebuild,
            },
        ) from exc
    detail = sanitize_action_error(exc)
    if isinstance(exc, ActionStateConflictError):
        raise HTTPException(status_code=409, detail=detail) from exc
    if isinstance(exc, (ActionValidationError, ExperimentValidationError)):
        raise HTTPException(status_code=400, detail=detail) from exc
    if isinstance(exc, ActionExecutionError):
        raise HTTPException(status_code=502, detail=detail) from exc
    raise HTTPException(status_code=500, detail=detail) from exc


def _approve_top_query_action_via_gateway(
    *,
    action: TopQueryActionPlan,
    payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    """Approve one action with its terminal fact in the same local transaction."""
    action_id = int(action.id or 0)
    if action_id <= 0:
        raise HTTPException(status_code=404, detail="Query Governance action not found")
    operator = str(user.username or user.id or "unknown")
    brand = str(action.brand or "").strip()
    query_text = str(action.query_text or "").strip()
    request_id, business_payload = _admin_request_id(
        payload,
        action_type="top_query_action_approve",
        operator=operator,
        target=str(action_id),
    )
    intent = ActionIntent(
        action_type="top_query_action_approve",
        idempotency_key=request_id,
        payload=business_payload,
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        query_text=query_text,
        target=str(action_id),
    )

    def fail(code: str, detail: str | None = None) -> None:
        error = code if not detail else f"{code}:{sanitize_action_error(detail)}"
        raise _GovernedActionFailure(error, 409)

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        if db is None:
            raise RuntimeError("top_query_approval_transaction_missing")
        current = (
            db.query(TopQueryActionPlan)
            .filter(TopQueryActionPlan.id == action_id)
            .with_for_update()
            .first()
        )
        if current is None:
            raise _GovernedActionFailure("top_query_action_not_found", 404)
        if str(current.brand or "").strip() != brand:
            fail("top_query_action_scope_changed")
        task = db.get(TopQueryTask, current.task_id)
        current_query = str(current.query_text or (task.query_text if task else "") or "")
        try:
            assert_query_governance_allowed(brand, current_query)
        except ExperimentValidationError as exc:
            raise _GovernedActionFailure(
                f"top_query_action_scope_denied:{sanitize_action_error(exc)}",
                400,
            ) from exc

        current_status = str(current.status or "").lower()
        if (
            str(current.action_type or "") == "query_demote"
            and get_env() == "prod"
            and current_status in {"waiting_human_approval", "pending_confirmation"}
            and should_block_legacy_query_demotion(
                env="prod",
                status=current_status,
                payload=_safe_json_loads(current.request_payload),
            )
        ):
            raise LegacyQueryDemotionContractError(action_id, current_status)
        if current_status != "waiting_human_approval":
            fail("top_query_action_state_conflict", current_status)

        response = _safe_json_loads(current.response_payload)
        if not isinstance(response, dict):
            response = {}
        approved_at = datetime.datetime.now().isoformat()
        lifecycle = response.get("lifecycle") if isinstance(response.get("lifecycle"), dict) else {}
        updated_response = {
            **response,
            "operator": operator,
            "reason": str(business_payload.get("reason") or ""),
            "approved_at": approved_at,
            "lifecycle": {
                **lifecycle,
                "approvedAt": approved_at,
                "operator": operator,
            },
        }
        claimed = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.id == action_id,
            TopQueryActionPlan.status == "waiting_human_approval",
        ).update(
            {
                TopQueryActionPlan.status: "approved",
                TopQueryActionPlan.response_payload: _bounded_action_response_payload(
                    updated_response
                ),
            },
            synchronize_session=False,
        )
        if claimed != 1:
            fail("top_query_action_state_conflict")
        db.flush()
        return {
            "action_id": action_id,
            "status": "approved",
        }

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        if db is None:
            return {"status": "failed", "reason": "transaction_missing"}
        current = (
            db.query(TopQueryActionPlan)
            .populate_existing()
            .filter(TopQueryActionPlan.id == action_id)
            .first()
        )
        if current is None or str(current.status or "").lower() != "approved":
            return {"status": "failed", "reason": "approval_state_not_persisted"}
        response = _safe_json_loads(current.response_payload)
        lifecycle = response.get("lifecycle") if isinstance(response, dict) else None
        if (
            not isinstance(response, dict)
            or response.get("operator") != operator
            or response.get("reason") != str(business_payload.get("reason") or "")
            or not isinstance(lifecycle, dict)
            or not str(lifecycle.get("approvedAt") or "").strip()
        ):
            return {"status": "failed", "reason": "approval_readback_mismatch"}
        return {
            "status": "success",
            "action_id": action_id,
            "approval_status": "approved",
        }

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == brand
            and _has_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        )
    )
    try:
        with top_query_brand_governance_lock(brand):
            with top_query_task_lock(int(action.task_id or 0)):
                result = gateway.execute(
                    intent,
                    execute,
                    readback=readback,
                    atomic_local_transaction=True,
                    authoritative_readback_on_success=True,
                )
    except TopQueryTaskLockBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ActionConflictError as exc:
        raise HTTPException(status_code=409, detail="Action idempotency conflict") from exc
    except ActionInProgressError as exc:
        raise HTTPException(status_code=409, detail="Action is still in progress") from exc
    except ActionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="Action authorization denied") from exc
    except ActionExecutionFailedError as exc:
        failure = exc.result if isinstance(exc.result, dict) else {}
        raw_error = str(failure.get("error") or "top_query_action_approval_failed")
        code, separator, context = raw_error.partition(":")
        if code == LEGACY_QUERY_DEMOTION_BLOCK_REASON:
            raise HTTPException(
                status_code=409,
                detail={
                    "blockedReason": LEGACY_QUERY_DEMOTION_BLOCK_REASON,
                    "requiresRebuild": True,
                },
            ) from exc
        status_code = {
            "top_query_action_not_found": 404,
            "top_query_action_scope_denied": 400,
            "top_query_action_scope_changed": 409,
            "top_query_action_state_conflict": 409,
        }.get(code, 500)
        detail = "Query Governance action approval failed"
        if separator and code == "top_query_action_scope_denied":
            detail = sanitize_action_error(context)
        elif separator and code == "top_query_action_state_conflict":
            detail = f"action cannot be approved from status: {sanitize_action_error(context)}"
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except Exception as exc:
        _raise_top_query_action_http_error(exc)
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Action result contract invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(status_code=409, detail="Action approval verification inconclusive")
    if result.get("status") == "failed":
        raise HTTPException(status_code=409, detail="Action approval verification failed")
    return result


def _reject_top_query_action_via_gateway(
    *,
    action: TopQueryActionPlan,
    payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    """Reject one action with its terminal fact in the same local transaction."""
    action_id = int(action.id or 0)
    if action_id <= 0:
        raise HTTPException(status_code=404, detail="Query Governance action not found")
    operator = str(user.username or user.id or "unknown")
    brand = str(action.brand or "").strip()
    query_text = str(action.query_text or "").strip()
    request_id, business_payload = _admin_request_id(
        payload,
        action_type="top_query_action_reject",
        operator=operator,
        target=str(action_id),
    )
    intent = ActionIntent(
        action_type="top_query_action_reject",
        idempotency_key=request_id,
        payload=business_payload,
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        query_text=query_text,
        target=str(action_id),
    )

    def fail(code: str, detail: str | None = None) -> None:
        error = code if not detail else f"{code}:{sanitize_action_error(detail)}"
        raise _GovernedActionFailure(error, 409)

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        if db is None:
            raise RuntimeError("top_query_rejection_transaction_missing")
        current = (
            db.query(TopQueryActionPlan)
            .filter(TopQueryActionPlan.id == action_id)
            .with_for_update()
            .first()
        )
        if current is None:
            raise _GovernedActionFailure("top_query_action_not_found", 404)
        if str(current.brand or "").strip() != brand:
            fail("top_query_action_scope_changed")
        task = db.get(TopQueryTask, current.task_id)
        current_query = str(current.query_text or (task.query_text if task else "") or "")
        try:
            assert_query_governance_allowed(brand, current_query)
        except ExperimentValidationError as exc:
            raise _GovernedActionFailure(
                f"top_query_action_scope_denied:{sanitize_action_error(exc)}",
                400,
            ) from exc

        current_status = str(current.status or "").lower()
        if current_status not in {"waiting_human_approval", "approved"}:
            fail("top_query_action_state_conflict", current_status)

        response = _safe_json_loads(current.response_payload)
        if not isinstance(response, dict):
            response = {}
        rejected_at = datetime.datetime.now().isoformat()
        updated_response = {
            **response,
            "rejection": {
                "operator": operator,
                "reason": str(business_payload.get("reason") or ""),
                "rejectedAt": rejected_at,
            },
        }
        claimed = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.id == action_id,
            TopQueryActionPlan.status == current.status,
            TopQueryActionPlan.status.in_(["waiting_human_approval", "approved"]),
        ).update(
            {
                TopQueryActionPlan.status: "rejected",
                TopQueryActionPlan.response_payload: _bounded_action_response_payload(
                    updated_response
                ),
            },
            synchronize_session=False,
        )
        if claimed != 1:
            fail("top_query_action_state_conflict")
        db.flush()
        return {
            "action_id": action_id,
            "status": "rejected",
        }

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        if db is None:
            return {"status": "failed", "reason": "transaction_missing"}
        current = (
            db.query(TopQueryActionPlan)
            .populate_existing()
            .filter(TopQueryActionPlan.id == action_id)
            .first()
        )
        if current is None or str(current.status or "").lower() != "rejected":
            return {"status": "failed", "reason": "rejection_state_not_persisted"}
        response = _safe_json_loads(current.response_payload)
        rejection = response.get("rejection") if isinstance(response, dict) else None
        if (
            not isinstance(rejection, dict)
            or rejection.get("operator") != operator
            or rejection.get("reason") != str(business_payload.get("reason") or "")
            or not str(rejection.get("rejectedAt") or "").strip()
        ):
            return {"status": "failed", "reason": "rejection_readback_mismatch"}
        return {
            "status": "success",
            "action_id": action_id,
            "rejection_status": "rejected",
        }

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == brand
            and _has_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        )
    )
    try:
        with top_query_brand_governance_lock(brand):
            with top_query_task_lock(int(action.task_id or 0)):
                result = gateway.execute(
                    intent,
                    execute,
                    readback=readback,
                    atomic_local_transaction=True,
                    authoritative_readback_on_success=True,
                )
    except TopQueryTaskLockBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ActionConflictError as exc:
        raise HTTPException(status_code=409, detail="Action idempotency conflict") from exc
    except ActionInProgressError as exc:
        raise HTTPException(status_code=409, detail="Action is still in progress") from exc
    except ActionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="Action authorization denied") from exc
    except ActionExecutionFailedError as exc:
        failure = exc.result if isinstance(exc.result, dict) else {}
        raw_error = str(failure.get("error") or "top_query_action_rejection_failed")
        code, separator, context = raw_error.partition(":")
        status_code = {
            "top_query_action_not_found": 404,
            "top_query_action_scope_denied": 400,
            "top_query_action_scope_changed": 409,
            "top_query_action_state_conflict": 409,
        }.get(code, 500)
        detail = "Query Governance action rejection failed"
        if separator and code == "top_query_action_scope_denied":
            detail = sanitize_action_error(context)
        elif separator and code == "top_query_action_state_conflict":
            detail = f"action cannot be rejected from status: {sanitize_action_error(context)}"
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except Exception as exc:
        _raise_top_query_action_http_error(exc)
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Action result contract invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(status_code=409, detail="Action rejection verification inconclusive")
    if result.get("status") == "failed":
        raise HTTPException(status_code=409, detail="Action rejection verification failed")
    return result


@router.post("/api/dashboard/top-query/actions/{action_id}/approve")
def approve_top_query_action(action_id: int, payload: Dict = Body(default={}), user: User = Depends(get_current_user)):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    action = _get_top_query_action_or_404(action_id, user)
    return _approve_top_query_action_via_gateway(
        action=action,
        payload=payload,
        user=user,
    )

@router.post("/api/dashboard/top-query/actions/{action_id}/execute")
def execute_top_query_action(action_id: int, payload: Dict = Body(default={}), user: User = Depends(get_current_user)):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    _get_top_query_action_or_404(action_id, user)
    try:
        kwargs: Dict[str, Any] = {
            "dry_run": payload.get("dryRun", False),
        }
        if "expectedConfigVersion" in payload:
            kwargs["expected_config_version"] = payload.get("expectedConfigVersion")
        return TopQueryGovernanceService().execute_approved_action(action_id, **kwargs)
    except Exception as exc:
        _raise_top_query_action_http_error(exc)

@router.post("/api/dashboard/top-query/actions/{action_id}/reject")
def reject_top_query_action(action_id: int, payload: Dict = Body(default={}), user: User = Depends(get_current_user)):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    action = _get_top_query_action_or_404(action_id, user)
    return _reject_top_query_action_via_gateway(
        action=action,
        payload=payload,
        user=user,
    )

@router.post("/api/dashboard/top-query/tasks/{task_id}/execute-action")
def execute_top_query_task_manual_actions(task_id: int, payload: Dict = Body(default={}), user: User = Depends(get_current_user)):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    _get_top_query_task_or_404(task_id, user)
    try:
        dry_run = payload.get("dryRun", False)
        expected_config_version = payload.get("expectedConfigVersion")
        expected_action_ids = payload.get("actionIds")
        db = SessionLocal()
        try:
            authoritative_actions = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.task_id == task_id,
                TopQueryActionPlan.action_type == "query_demote",
                TopQueryActionPlan.status.in_([
                    "approved",
                    "waiting_human_approval",
                    "pending_confirmation",
                ]),
            ).all()
            requires_authoritative_contract = any(
                uses_authoritative_query_demotion(_safe_json_loads(action.request_payload))
                for action in authoritative_actions
            )
        finally:
            db.close()
        if requires_authoritative_contract and not dry_run:
            if not isinstance(expected_config_version, str) or not expected_config_version.strip():
                raise ActionValidationError(
                    "expectedConfigVersion is required for authoritative query demotion"
                )
            if (
                not isinstance(expected_action_ids, list)
                or not expected_action_ids
                or any(
                    not isinstance(action_id, int) or isinstance(action_id, bool)
                    for action_id in expected_action_ids
                )
            ):
                raise ActionValidationError(
                    "actionIds is required for authoritative query demotion"
                )
        kwargs: Dict[str, Any] = {"dry_run": dry_run}
        if "expectedConfigVersion" in payload or "actionIds" in payload:
            kwargs.update({
                "expected_config_version": expected_config_version,
                "expected_action_ids": expected_action_ids,
            })
        return TopQueryGovernanceService().execute_task_manual_actions(task_id, **kwargs)
    except Exception as exc:
        _raise_top_query_action_http_error(exc)


@router.get("/api/dashboard/top-query/tasks/{task_id}/execute-action-preview")
def preview_top_query_task_manual_actions(
    task_id: int,
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    _get_top_query_task_or_404(task_id, user)
    try:
        return TopQueryGovernanceService().preview_task_manual_actions(task_id)
    except Exception as exc:
        _raise_top_query_action_http_error(exc)


@router.post("/api/dashboard/top-query/actions/{action_id}/rollback")
def rollback_top_query_action(
    action_id: int,
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    _get_top_query_action_or_404(action_id, user)
    try:
        return TopQueryGovernanceService().rollback_query_demote_action(
            action_id,
            operator=user.username,
        )
    except Exception as exc:
        _raise_top_query_action_http_error(exc)


def _mark_top_query_task_processed_via_gateway(
    *,
    task: TopQueryTask,
    payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    task_id = int(task.id or 0)
    if task_id <= 0:
        raise HTTPException(status_code=404, detail="Query Governance task not found")
    operator = str(user.username or user.id or "unknown")
    brand = str(task.brand or "").strip()
    query_text = str(task.query_text or "").strip()
    request_id, business_payload = _admin_request_id(
        payload,
        action_type="top_query_task_mark_processed",
        operator=operator,
        target=str(task_id),
    )
    reason = str(business_payload.get("reason") or "")
    intent = ActionIntent(
        action_type="top_query_task_mark_processed",
        idempotency_key=request_id,
        payload=business_payload,
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        query_text=query_text,
        target=str(task_id),
    )

    def fail(code: str, detail: str | None = None) -> None:
        error = code if not detail else f"{code}:{sanitize_action_error(detail)}"
        raise _GovernedActionFailure(error, 409)

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        if db is None:
            raise RuntimeError("top_query_mark_processed_transaction_missing")
        current = (
            db.query(TopQueryTask)
            .filter(TopQueryTask.id == task_id)
            .with_for_update()
            .first()
        )
        if current is None:
            raise _GovernedActionFailure("top_query_task_not_found", 404)
        if (
            str(current.brand or "").strip() != brand
            or str(current.query_text or "").strip() != query_text
        ):
            fail("top_query_task_scope_changed")
        try:
            assert_query_governance_allowed(brand, str(current.query_text or ""))
        except ExperimentValidationError as exc:
            raise _GovernedActionFailure(
                f"top_query_task_scope_denied:{sanitize_action_error(exc)}",
                400,
            ) from exc

        current_status = str(current.status or "").lower()
        if current_status != "manual_required":
            fail("top_query_task_state_conflict", current_status or "unknown")
        raw_payload = _safe_json_loads(current.raw_payload)
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        processed_at = datetime.datetime.now()
        raw_payload["manual_processing"] = {
            "operator": operator,
            "reason": reason,
            "processedAt": processed_at.isoformat(),
        }
        updated = db.query(TopQueryTask).filter(
            TopQueryTask.id == task_id,
            TopQueryTask.status == "manual_required",
        ).update(
            {
                TopQueryTask.status: "completed",
                TopQueryTask.display_status: "fixed",
                TopQueryTask.completed_at: processed_at,
                TopQueryTask.raw_payload: json.dumps(raw_payload, ensure_ascii=False),
            },
            synchronize_session=False,
        )
        if updated != 1:
            fail("top_query_task_state_conflict")
        db.flush()
        return {"task_id": task_id, "status": "completed"}

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        if db is None:
            return {"status": "failed", "reason": "transaction_missing"}
        current = (
            db.query(TopQueryTask)
            .populate_existing()
            .filter(TopQueryTask.id == task_id)
            .first()
        )
        raw_payload = _safe_json_loads(current.raw_payload) if current is not None else {}
        marker = raw_payload.get("manual_processing") if isinstance(raw_payload, dict) else None
        if (
            current is None
            or str(current.status or "").lower() != "completed"
            or str(current.display_status or "").lower() != "fixed"
            or current.completed_at is None
            or not isinstance(marker, dict)
            or marker.get("operator") != operator
            or marker.get("reason") != reason
            or not str(marker.get("processedAt") or "").strip()
        ):
            return {"status": "failed", "reason": "processed_state_not_persisted"}
        return {
            "status": "success",
            "task_id": task_id,
            "taskStatus": str(current.status),
            "displayStatus": str(current.display_status),
            "operator": marker.get("operator"),
            "reason": marker.get("reason"),
            "processedAt": marker.get("processedAt"),
        }

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == brand
            and _has_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        )
    )
    try:
        with top_query_brand_governance_lock(brand):
            with top_query_task_lock(task_id):
                result = gateway.execute(
                    intent,
                    execute,
                    readback=readback,
                    atomic_local_transaction=True,
                    authoritative_readback_on_success=True,
                )
    except TopQueryTaskLockBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ActionConflictError as exc:
        raise HTTPException(status_code=409, detail="Action idempotency conflict") from exc
    except ActionInProgressError as exc:
        raise HTTPException(status_code=409, detail="Action is still in progress") from exc
    except ActionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="Action authorization denied") from exc
    except ActionExecutionFailedError as exc:
        failure = exc.result if isinstance(exc.result, dict) else {}
        raw_error = str(failure.get("error") or "top_query_mark_processed_failed")
        code, separator, context = raw_error.partition(":")
        status_code = {
            "top_query_task_not_found": 404,
            "top_query_task_scope_denied": 400,
            "top_query_task_scope_changed": 409,
            "top_query_task_state_conflict": 409,
        }.get(code, 500)
        detail = "Query Governance task processing failed"
        if separator and code == "top_query_task_scope_denied":
            detail = sanitize_action_error(context)
        elif separator and code == "top_query_task_state_conflict":
            detail = f"task cannot be marked processed from status: {sanitize_action_error(context)}"
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except Exception as exc:
        _raise_top_query_action_http_error(exc)
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Task processing result contract invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(status_code=409, detail="Task processing verification inconclusive")
    if result.get("status") == "failed":
        raise HTTPException(status_code=409, detail="Task processing verification failed")
    return result


@router.post("/api/dashboard/top-query/tasks/{task_id}/mark-processed")
def mark_top_query_task_processed(
    task_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    task = _get_top_query_task_or_404(task_id, user)
    return _mark_top_query_task_processed_via_gateway(
        task=task,
        payload=payload,
        user=user,
    )


__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
