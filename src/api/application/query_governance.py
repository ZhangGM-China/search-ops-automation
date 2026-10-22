"""Deterministic Query Governance read API.

This module is the public read boundary for the rewritten workflow.  It keeps
authentication and brand permissions, but never reads legacy Top Query ORM
models or exposes Planner/Executor/price/health-score fields.
"""

from __future__ import annotations

import os
import json
import datetime
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request

from src.integrations.operational_service import OperationalService
from src.config import BRAND_CONFIGS, get_brand_business_config, is_brand_enabled
from src.persistence import SessionLocal, User
from src.query_governance.read_api import (
    QueryGovernanceNotFoundError,
    QueryGovernanceReadService,
)
from src.query_governance.experiments import (
    QueryGovernanceExperimentError,
    QueryGovernanceExperimentService,
)
from src.persistence import ACTION_EXECUTE_GOVERNANCE_ACTIONS, ACTION_MANAGE_BRAND_CONFIG
from src.llm.config_store import capture_runtime_snapshot
from src.query_governance.model_runtime import QueryGovernanceModelRuntime
from src.query_governance.relevance_guidance import GuidanceNotFoundError, RelevanceGuidanceService
from src.query_governance.langfuse import QueryGovernanceObservability
from src.query_governance.composition import build_manual_relevance_check_service
from src.query_governance.manual_relevance import ProductDetailBatchGateway
from src.query_governance.kg_execution import QueryGovernanceKGActionExecutor
from src.query_governance.recall_rate_actions import execute_pending_recall_rate_action
from src.query_governance.kg_agent import KGPlanConflictChecker, KnowledgeGraphReadTools
from src.query_governance.models import QueryGovernanceActionPlan
from src.api.contracts import (
    ManualRelevanceCheckResponse,
    ManualRelevanceCheckSubmitRequest,
    ManualRelevanceCheckSubmitResponse,
)
from .query_governance_support import _top_query_data_date_for_monitoring_date

from ..dependencies import _ensure_action_permission, _user_brand_permissions, get_current_user
from ..shared import (
    _clear_top_query_endpoint_caches,
    _top_query_cache_get,
    _top_query_cache_set,
)


router = APIRouter()
_operational_service = OperationalService()


def _live_result_timeout_seconds() -> float:
    try:
        value = float(os.getenv("AIOPS_TOP_QUERY_LIVE_RESULT_TIMEOUT_SECONDS", "3"))
    except (TypeError, ValueError):
        value = 3.0
    return value if value > 0 else 3.0


def _service() -> QueryGovernanceReadService:
    def live_search(brand: str, query: str, limit: int) -> dict[str, Any]:
        result = _operational_service.search_products_with_total(
            brand,
            query,
            page_size=limit,
            enrich_details=False,
            request_timeout=_live_result_timeout_seconds(),
            max_attempts=1,
        )
        return result if isinstance(result, dict) else {}

    return QueryGovernanceReadService(SessionLocal, live_search=live_search)


def _experiment_service() -> QueryGovernanceExperimentService:
    return QueryGovernanceExperimentService(SessionLocal)


def _manual_relevance_service():
    return build_manual_relevance_check_service(
        operational_service=_operational_service,
    )


def _ensure_brand(brand: str, user: User) -> None:
    if brand not in _user_brand_permissions(user):
        raise HTTPException(status_code=403, detail="No permission for brand")


def _date_value(value: datetime.date | None) -> datetime.date | None:
    """Convert the UI monitoring date to the underlying T-1 business date."""
    monitoring_date = value or datetime.date.today()
    return _top_query_data_date_for_monitoring_date(monitoring_date)


def _product_display_limit(brand: str) -> int:
    """Resolve the configured per-Query product sample size for the detail UI."""
    try:
        value = get_brand_business_config(brand).get("top_query_check_product_limit", 16)
        if isinstance(value, bool):
            raise ValueError
        value = int(value)
        return value if value > 0 else 16
    except (TypeError, ValueError):
        return 16


def _kg_records(payload: Any) -> list[dict[str, Any]]:
    records: list[Any] = []
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                data = None
        if isinstance(data, dict):
            if "name" in data or "tagName" in data or "id" in data:
                records.append(data)
            for key in ("records", "list", "rows", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    records.extend(value)
        elif isinstance(data, list):
            records.extend(data)
    elif isinstance(payload, list):
        records.extend(payload)
    return [record for record in records if isinstance(record, dict)]


def _check_kg_read(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("success") is False or str(payload.get("code", "0")) not in {"0", "200"}:
        raise HTTPException(status_code=502, detail="Knowledge graph read failed")


@router.get("/api/dashboard/top-query/brands/{brand}/kg-tag-details")
def get_query_governance_kg_tag_details(
    brand: str,
    name: str = Query(..., min_length=1, max_length=200),
    user: User = Depends(get_current_user),
):
    """Return the small, user-facing attribute set for one exact KG tag."""
    _ensure_brand(brand, user)
    tag_name = name.strip()
    if not tag_name:
        raise HTTPException(status_code=400, detail="Tag name is empty")
    try:
        payload = KnowledgeGraphReadTools(_operational_service.kg_client).query_tag(brand, tag_name)
    except Exception as exc:  # external read failures need an explicit UI error
        raise HTTPException(status_code=502, detail="Knowledge graph tag read failed") from exc
    _check_kg_read(payload)
    record = next((record for record in _kg_records(payload) if str(record.get("name") or record.get("tagName") or "").strip() == tag_name), None)
    if record is None:
        raise HTTPException(status_code=404, detail="Knowledge graph tag not found")
    fields = (
        "name", "viewTag", "isRoot", "dimensions", "weight", "isEnglish",
        "segmentingAgain", "isCompose", "nameAnd", "nameNot",
    )
    return {field: tag_name if field == "name" else record.get(field) for field in fields}


@router.get("/api/dashboard/top-query/brands/{brand}/kg-relationship-details")
def get_query_governance_kg_relationship_details(
    brand: str,
    id: str = Query(..., min_length=1, max_length=200),
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    try:
        payload = _operational_service.kg_client.snapshot({
            "brand": brand, "actionType": "delete_relationship", "params": {"id": id},
        })
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Knowledge graph relationship read failed") from exc
    _check_kg_read(payload)
    record = next((item for item in _kg_records(payload) if str(item.get("id") or "") == id), None)
    if record is None:
        raise HTTPException(status_code=404, detail="Knowledge graph relationship not found")
    return {key: record.get(key) for key in ("tagName", "responseTagName", "relationships")}


def _relevance_rules_runtime() -> QueryGovernanceModelRuntime:
    snapshot = capture_runtime_snapshot(
        node_keys=("query_governance.relevance_rules",),
    )
    return QueryGovernanceModelRuntime.from_snapshot(
        "query_governance.relevance_rules",
        snapshot,
        timeout=120,
        temperature=0.1,
        max_completion_tokens=20000,
    )


def _guidance_service() -> RelevanceGuidanceService:
    return RelevanceGuidanceService(
        SessionLocal,
        organizer_runtime_factory=_relevance_rules_runtime,
        observability=QueryGovernanceObservability.from_environment(),
    )


@router.post(
    "/api/query-governance/relevance-checks",
    response_model=ManualRelevanceCheckSubmitResponse,
    status_code=202,
    operation_id="manual_relevance_check_submit",
)
def submit_manual_relevance_check(
    payload: ManualRelevanceCheckSubmitRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
):
    brand = payload.brand.strip().lower()
    _ensure_brand(brand, user)
    service = _manual_relevance_service()
    try:
        submission = service.submit(
            brand=brand,
            queries=[
                {"query": item.query, "skus": list(item.products)}
                for item in payload.queries
            ],
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        if "idempotency" in str(exc).lower():
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if submission.created:
        background_tasks.add_task(service.run, submission.check_id)
    return {
        "checkId": submission.check_id,
        "status": submission.status,
    }


@router.get(
    "/api/query-governance/relevance-checks/latest",
    response_model=ManualRelevanceCheckResponse | None,
    operation_id="manual_relevance_check_latest_read",
)
def get_latest_manual_relevance_check(
    brand: str = Query(...),
    query: str = Query(...),
    user: User = Depends(get_current_user),
):
    normalized_brand = brand.strip().lower()
    _ensure_brand(normalized_brand, user)
    service = _manual_relevance_service()
    payload = service.get_latest(brand=normalized_brand, query=query)
    # 无验证记录是业务常态而非资源缺失；返回 200 + null，避免前端与网关把常态读当报错。
    return payload


@router.get(
    "/api/query-governance/relevance-checks/{check_id}",
    response_model=ManualRelevanceCheckResponse,
    operation_id="manual_relevance_check_read",
)
def get_manual_relevance_check(
    check_id: str,
    user: User = Depends(get_current_user),
):
    service = _manual_relevance_service()
    payload = service.get(check_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="manual relevance check not found")
    _ensure_brand(str(payload.get("brand") or ""), user)
    return payload


@router.get("/api/dashboard/top-query/overview")
def get_query_governance_overview(
    date: datetime.date | None = Query(None),
    user: User = Depends(get_current_user),
):
    # 已关闭的品牌不进入总览，避免前端品牌卡片展示长期空数据。
    brands = [
        brand
        for brand in _user_brand_permissions(user)
        if brand not in BRAND_CONFIGS or is_brand_enabled(brand)
    ]
    data_date = _date_value(date)
    cache_key = (tuple(sorted(brands)), data_date.isoformat())
    cached = _top_query_cache_get("query_governance_overview", cache_key)
    if cached is not None:
        return cached
    payload = _service().overview(brands, data_date=data_date)
    monitoring_date = date or datetime.date.today()
    ttl_seconds = 30 if monitoring_date >= datetime.date.today() else 300
    _top_query_cache_set(
        "query_governance_overview",
        cache_key,
        payload,
        ttl_seconds=ttl_seconds,
    )
    return payload


@router.get("/api/dashboard/top-query/brands/{brand}")
def get_query_governance_brand_detail(
    brand: str,
    date: datetime.date | None = Query(None),
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    return _service().brand_detail(brand, data_date=_date_value(date))


@router.get("/api/dashboard/top-query/brands/{brand}/summary")
def get_query_governance_brand_summary(
    brand: str,
    date: datetime.date | None = Query(None),
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    data_date = _date_value(date)
    cache_key = (brand, data_date.isoformat() if data_date else None)
    cached = _top_query_cache_get("query_governance_brand_summary", cache_key)
    if cached is not None:
        return cached
    service = _service()
    payload = service.brand_summary(brand, data_date=data_date) if hasattr(service, "brand_summary") else service.brand_detail(brand, data_date=data_date)
    response = {
        "brand": brand,
        "batch": payload["batch"],
        "summary": payload["summary"],
        "stats": payload.get("stats"),
    }
    monitoring_date = date or datetime.date.today()
    _top_query_cache_set(
        "query_governance_brand_summary",
        cache_key,
        response,
        ttl_seconds=60 if monitoring_date >= datetime.date.today() else 300,
    )
    return response


@router.get("/api/dashboard/top-query/brands/{brand}/queries")
def get_query_governance_brand_queries(
    brand: str,
    date: datetime.date | None = Query(None),
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(30, alias="pageSize", ge=1, le=100),
    filter_key: str = Query("all", alias="filter"),
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    data_date = _date_value(date)
    service = _service()
    use_lightweight_read = hasattr(service, "brand_queries")
    cache_key = (brand, data_date.isoformat() if data_date else None, page, page_size, filter_key)
    if use_lightweight_read:
        cached = _top_query_cache_get("query_governance_brand_queries", cache_key)
        if cached is not None:
            return cached
    payload = (
        service.brand_queries(
            brand,
            data_date=data_date,
            page=page,
            page_size=page_size,
            filter_key=filter_key,
        )
        if use_lightweight_read
        else service.brand_detail(brand, data_date=data_date)
    )
    response = {
        "brand": brand,
        "dataDate": payload["batch"]["dataDate"] if payload["batch"] else None,
        "reason": payload["batch"].get("reason") if payload["batch"] else None,
        "queries": payload["queries"],
        "total": payload.get("total", len(payload["queries"])),
        "page": payload.get("page", page),
        "pageSize": payload.get("pageSize", page_size),
        "hasNext": payload.get("hasNext", False),
        "filter": payload.get("filter", filter_key),
        "stats": payload.get("stats"),
    }
    monitoring_date = date or datetime.date.today()
    if use_lightweight_read:
        _top_query_cache_set(
            "query_governance_brand_queries",
            cache_key,
            response,
            ttl_seconds=60 if monitoring_date >= datetime.date.today() else 300,
        )
    return response


@router.get("/api/dashboard/top-query/queries/{task_id}/workspace")
def get_query_governance_query_workspace(
    task_id: int,
    request: Request,
    include_trend: bool = Query(True, alias="includeTrend"),
    user: User = Depends(get_current_user),
):
    service = _service()
    started = time.perf_counter()
    try:
        payload = service.query_workspace(task_id, include_trend=include_trend)
    except QueryGovernanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        if request is not None:
            request.state.workspace_duration_ms = (time.perf_counter() - started) * 1000
    query_payload = payload.get("query") or payload
    _ensure_brand(query_payload["brand"], user)
    response = {
        **payload,
        "productDisplayLimit": _product_display_limit(query_payload["brand"]),
    }
    if not include_trend:
        response["trend"] = []
    return response


@router.get("/api/dashboard/top-query/brands/{brand}/trend")
def get_query_governance_brand_trend(
    brand: str,
    date: datetime.date | None = Query(None),
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    service = _service()
    if hasattr(service, "brand_trend"):
        return service.brand_trend(brand, data_date=_date_value(date))
    payload = service.brand_detail(brand, data_date=_date_value(date))
    return {
        "brand": brand,
        "dataDate": payload["batch"]["dataDate"] if payload["batch"] else None,
        # BrandDailyMetric is the persisted business-metric source for the
        # operations drawer.  Keep the trend endpoint aligned with the
        # detail payload instead of returning the old placeholder empty list.
        "trend": payload.get("trend", []),
    }


@router.get("/api/dashboard/top-query/brands/{brand}/metric-trend")
def get_query_governance_brand_metric_trend(
    brand: str,
    start_date: datetime.date | None = Query(None, alias="startDate"),
    end_date: datetime.date | None = Query(None, alias="endDate"),
    user: User = Depends(get_current_user),
):
    """Read per-day operations metric snapshots for the overview drawer modal.

    ``startDate``/``endDate`` are monitoring dates and are converted to the
    underlying T-1 business date, matching the existing brand trend endpoint.
    The server clamps the window to 180 days.
    """

    _ensure_brand(brand, user)
    return _service().metric_trend(
        brand,
        start_date=_date_value(start_date),
        end_date=_date_value(end_date),
    )


@router.get("/api/dashboard/top-query/tasks")
def get_query_governance_tasks(
    brand: str | None = None,
    status_value: str | None = Query(None, alias="status"),
    user: User = Depends(get_current_user),
):
    brands = [_ensure_brand(brand, user) or brand] if brand else _user_brand_permissions(user)
    return _service().task_list(brands, status=status_value)


@router.get("/api/dashboard/top-query/queries/{task_id}")
def get_query_governance_detail(
    task_id: int,
    user: User = Depends(get_current_user),
):
    try:
        payload = _service().query_detail(task_id)
    except QueryGovernanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _ensure_brand(payload["query"]["brand"], user)
    return {
        **payload,
        "productDisplayLimit": _product_display_limit(payload["query"]["brand"]),
    }


@router.post("/api/query-governance/actions/{action_id}/execute")
def execute_query_governance_kg_action(
    action_id: int,
    payload: dict[str, Any] | None = None,
    user: User = Depends(get_current_user),
):
    """Execute one structured governance plan through the governed boundary."""
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    request = payload or {}
    group_id = request.get("groupId") or request.get("group_id")
    with SessionLocal() as session:
        action_plan = session.get(QueryGovernanceActionPlan, action_id)
        if action_plan is None:
            raise HTTPException(status_code=404, detail="Governance action plan not found")
        action_type = str(action_plan.action_type)
        if action_type in {"whitelist_tag", "query_whitelist"}:
            _ensure_brand(str(action_plan.brand), user)
            try:
                return execute_pending_recall_rate_action(
                    action_id,
                    session_factory=SessionLocal,
                    operational_service=_operational_service,
                    operator=str(user.username or user.id or "operator"),
                    user_id=str(user.id or user.username or "operator"),
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        if action_type not in {"kg_plan", "relevance_plan"}:
            raise HTTPException(status_code=404, detail="KG action plan not found")
        _ensure_brand(str(action_plan.brand), user)
    executor = QueryGovernanceKGActionExecutor(
        session_factory=SessionLocal,
        kg_client=_operational_service.kg_client,
        authorizer=lambda _intent: True,
        operator=str(user.username or user.id or "operator"),
        plan_guard=KGPlanConflictChecker(
            KnowledgeGraphReadTools(
                _operational_service.kg_client,
                search=_operational_service,
            )
        ),
    )
    try:
        return executor.execute(
            action_id,
            user_id=str(user.id or user.username or "operator"),
            group_id=str(group_id) if group_id else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/query-governance/kg-plans/{plan_id}/execute")
def execute_query_governance_kg_plan(
    plan_id: int,
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    return execute_query_governance_kg_action(
        plan_id,
        payload={},
        user=user,
    )


@router.get("/api/dashboard/top-query/queries/{task_id}/trend")
def get_query_governance_query_trend(
    task_id: int,
    user: User = Depends(get_current_user),
):
    try:
        service = _service()
        payload = service.query_workspace(task_id) if hasattr(service, "query_workspace") else service.query_detail(task_id)
    except QueryGovernanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    query_payload = payload.get("query") or payload
    _ensure_brand(query_payload["brand"], user)
    return {
        "taskId": task_id,
        "brand": query_payload["brand"],
        "queryText": query_payload["queryText"],
        "trend": payload.get("trend", []),
    }


@router.get("/api/dashboard/top-query/queries/{task_id}/current-result-count")
def get_query_governance_live_result(
    task_id: int,
    user: User = Depends(get_current_user),
):
    try:
        service = _service()
        payload = service.query_identity(task_id) if hasattr(service, "query_identity") else service.query_detail(task_id)
    except QueryGovernanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    query_payload = payload.get("query") or payload
    _ensure_brand(query_payload["brand"], user)
    product_display_limit = _product_display_limit(query_payload["brand"])
    cache_key = (
        query_payload["brand"],
        query_payload.get("queryText") or query_payload.get("query_text") or "",
        product_display_limit,
    )
    cached = _top_query_cache_get("current_result_count", cache_key)
    if cached is not None:
        return {
            "taskId": task_id,
            **cached,
            "productDisplayLimit": product_display_limit,
        }
    live_result = service.live_result(
        task_id,
        limit=product_display_limit,
    )
    cache_payload = {
        key: value
        for key, value in live_result.items()
        if key != "taskId"
    }
    _top_query_cache_set(
        "current_result_count",
        cache_key,
        cache_payload,
        ttl_seconds=30 if not live_result.get("errorCode") else 5,
    )
    return {
        "taskId": task_id,
        **cache_payload,
        "productDisplayLimit": product_display_limit,
    }


@router.get("/api/dashboard/top-query/brands/{brand}/products/{product_id}")
def get_query_governance_product_detail(
    brand: str,
    product_id: str,
    task_id: int | None = Query(None, alias="taskId"),
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    snapshot_product = None
    query_text = ""
    if task_id is not None:
        try:
            payload = _service().query_detail(task_id)
        except QueryGovernanceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if payload["query"]["brand"] != brand:
            raise HTTPException(status_code=400, detail="Product task does not belong to brand")
        query_text = str(payload["query"].get("queryText") or "").strip()
        snapshot_products = (payload.get("s0") or {}).get("products") or []
        snapshot_product = next(
            (
                item
                for item in snapshot_products
                if str(product_id) in {
                    str(item.get(key) or "")
                    for key in ("productId", "product_id", "spuId", "spu", "skuId", "sku_id")
                }
            ),
            None,
        )
    try:
        live_products = _operational_service.get_product_details(brand, product_id)
    except Exception:
        live_products = []
    live_product = next((item for item in live_products if isinstance(item, dict)), None)
    if live_product and brand != "summit":
        description = live_product.get("description")
        record_id = live_product.get("id")
        if record_id and not (isinstance(description, str) and description.strip()):
            try:
                # 商品列表不含描述；按记录 ID 补读，避免覆盖库存和标签等列表字段。
                attributes = _operational_service.get_product_tag_sources(brand, record_id)
                description = attributes.get("description")
                if attributes.get("id") == record_id and isinstance(description, str) and description.strip():
                    live_product = {**live_product, "description": description.strip()}
            except Exception:
                pass
    description = (live_product or {}).get("description")
    if query_text and not (isinstance(description, str) and description.strip()):
        try:
            # 测试租户可能无商品管理权限；同一 Query 的召回结果仍包含商品描述。
            result = _operational_service.search_products_with_total(
                brand, query_text, page_size=100, enrich_details=False,
                request_timeout=_live_result_timeout_seconds(), max_attempts=1,
            )
            rows = result.get("products", []) if isinstance(result, dict) and result.get("success") is not False else []
            rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            exact = next((row for row in rows if product_id in {
                str(row.get(key) or "") for key in ("productId", "product_id", "spuId", "spu", "skuId", "taggingUid")
            }), None)
            matched = exact or next((row for row in rows if ProductDetailBatchGateway._matches_identifier(row, product_id)), None)
            if matched:
                if not live_product:
                    live_product = matched
                elif isinstance(matched.get("description"), str) and matched["description"].strip():
                    live_product = {**live_product, "description": matched["description"].strip()}
        except Exception:
            pass
    if live_product and snapshot_product:
        product = {
            **snapshot_product,
            **{
                key: value
                for key, value in live_product.items()
                if value is not None and (not isinstance(value, str) or value.strip())
            },
        }
        detail_source = "live_with_snapshot"
        is_snapshot = False
    elif live_product:
        product = live_product
        detail_source = "live"
        is_snapshot = False
    elif snapshot_product:
        product = snapshot_product
        detail_source = "s0"
        is_snapshot = True
    else:
        if task_id is not None:
            raise HTTPException(status_code=404, detail="Query Governance product detail not found")
        product = {"productId": product_id, "skuId": product_id}
        detail_source = "operations_log_fallback"
        is_snapshot = False
    return {
        "product": product,
        "detailMeta": {
            "detailSource": detail_source,
            "capturedAt": None,
            "isSnapshot": is_snapshot,
            "taskId": task_id,
        },
    }


@router.get("/api/dashboard/top-query/brands/{brand}/relevance-guidance")
def get_relevance_guidance(
    brand: str,
    query: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    service = _guidance_service()
    try:
        if query is not None and query.strip():
            return service.get(brand, query)
        return {"brand": brand, "items": service.list(brand)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/dashboard/top-query/brands/{brand}/relevance-guidance")
def save_relevance_guidance(
    brand: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG)
    try:
        return _guidance_service().save(
            brand,
            str(payload.get("query") or ""),
            str(payload.get("rules") or ""),
            operator=str(user.username or user.id or "unknown"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/dashboard/top-query/brands/{brand}/relevance-guidance/append")
def append_relevance_guidance_rule(
    brand: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    try:
        return _guidance_service().append_rule(
            brand,
            str(payload.get("query") or ""),
            str(payload.get("rule") or ""),
            operator=str(user.username or user.id or "unknown"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/dashboard/top-query/brands/{brand}/relevance-guidance/deactivate")
def deactivate_relevance_guidance(
    brand: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG)
    try:
        return _guidance_service().deactivate(
            brand,
            str(payload.get("query") or ""),
            operator=str(user.username or user.id or "unknown"),
        )
    except GuidanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/dashboard/top-query/brands/{brand}/relevance-guidance/organize")
def preview_relevance_guidance_organization(
    brand: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG)
    try:
        return _guidance_service().preview_organize(
            brand,
            str(payload.get("query") or ""),
            operator=str(user.username or user.id or "unknown"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/dashboard/top-query/brands/{brand}/relevance-guidance/confirm")
def confirm_relevance_guidance_organization(
    brand: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG)
    try:
        return _guidance_service().confirm_organize(
            brand,
            str(payload.get("query") or ""),
            str(payload.get("rules") or ""),
            operator=str(user.username or user.id or "unknown"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/dashboard/top-query/queries/{task_id}/events")
def get_query_governance_events(
    task_id: int,
    user: User = Depends(get_current_user),
):
    try:
        service = _service()
        payload = service.query_workspace(task_id) if hasattr(service, "query_workspace") else service.query_detail(task_id)
    except QueryGovernanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _ensure_brand(payload["query"]["brand"], user)
    return {"items": payload.get("events", [])}


@router.get("/api/dashboard/top-query/experiments/brands/{brand}")
def get_query_governance_experiment_history(
    brand: str,
    status_value: str | None = Query(None, alias="status"),
    start_date: datetime.date | None = Query(None, alias="startDate"),
    end_date: datetime.date | None = Query(None, alias="endDate"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, alias="pageSize", ge=1, le=100),
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    return _experiment_service().list_history_summaries(
        brand,
        status=status_value,
        start_date=start_date,
        end_date=end_date,
        page=page,
        page_size=page_size,
    )


@router.get("/api/dashboard/top-query/experiments/brands/{brand}/sample-pool")
def get_query_governance_experiment_sample_pool(
    brand: str,
    today: datetime.date | None = Query(None),
    limit: int = Query(150, ge=1, le=500),
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    return _experiment_service().get_sample_pool(brand, today=today, limit=limit)


@router.post("/api/dashboard/top-query/experiments/brands/{brand}/draft")
def create_query_governance_experiment_draft(
    brand: str,
    payload: dict[str, Any],
    user: User = Depends(get_current_user),
):
    _ensure_brand(brand, user)
    _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG)
    try:
        return _experiment_service().create_or_replace_draft(
            brand=brand,
            created_by=user.username,
            experiment_name=str(payload.get("experimentName") or ""),
            treatment_queries=payload.get("treatmentQueries") or [],
            control_queries=payload.get("controlQueries") or [],
            today=(
                datetime.date.fromisoformat(str(payload["today"]))
                if payload.get("today")
                else None
            ),
            planned_end_date=(
                datetime.date.fromisoformat(str(payload["plannedEndDate"]))
                if payload.get("plannedEndDate")
                else None
            ),
        )
    except (QueryGovernanceExperimentError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _experiment_mutation(
    experiment_id: int,
    user: User,
    operation: str,
) -> dict[str, Any]:
    _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG)
    service = _experiment_service()
    try:
        detail = service.get_experiment_detail(experiment_id)
        _ensure_brand(detail["brand"], user)
        return getattr(service, operation)(experiment_id)
    except QueryGovernanceExperimentError as exc:
        status_code = 404 if str(exc) == "实验不存在" else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/api/dashboard/top-query/experiments/{experiment_id}/schedule")
def schedule_query_governance_experiment(
    experiment_id: int,
    user: User = Depends(get_current_user),
):
    return _experiment_mutation(experiment_id, user, "schedule_experiment")


@router.post("/api/dashboard/top-query/experiments/{experiment_id}/end")
def end_query_governance_experiment(
    experiment_id: int,
    user: User = Depends(get_current_user),
):
    return _experiment_mutation(experiment_id, user, "end_experiment")


@router.post("/api/dashboard/top-query/experiments/{experiment_id}/cancel")
def cancel_query_governance_experiment(
    experiment_id: int,
    user: User = Depends(get_current_user),
):
    return _experiment_mutation(experiment_id, user, "cancel_experiment")


@router.get("/api/dashboard/top-query/experiments/{experiment_id}")
def get_query_governance_experiment_detail(
    experiment_id: int,
    as_of_date: datetime.date | None = Query(None, alias="asOfDate"),
    user: User = Depends(get_current_user),
):
    try:
        payload = _experiment_service().get_experiment_detail(
            experiment_id, as_of_date=as_of_date
        )
    except QueryGovernanceExperimentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _ensure_brand(payload["brand"], user)
    return payload


@router.get("/api/dashboard/top-query/experiments/{experiment_id}/report")
def get_query_governance_experiment_report(
    experiment_id: int,
    user: User = Depends(get_current_user),
):
    try:
        detail = _experiment_service().get_experiment_detail(experiment_id)
    except QueryGovernanceExperimentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _ensure_brand(detail["brand"], user)
    return {
        "experiment": {
            "experimentId": detail["id"],
            "brand": detail["brand"],
            "experimentName": detail["experimentName"],
            "status": detail["status"],
        },
        "queryDailyMetrics": detail.get("dailyMetrics", []),
        "groups": detail.get("groupSummary", {}),
        "overview": detail,
    }


__all__ = ["router"]
