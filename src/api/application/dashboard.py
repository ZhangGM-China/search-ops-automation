"""Operational dashboard application handlers."""

from __future__ import annotations

import hashlib
import concurrent.futures
from fastapi import Query, Body, HTTPException, Depends, status
from typing import Optional, Dict, Any
import datetime
import json
from src.api.application.query_governance_support import _extract_product_image, _product_key
from src.integrations.operational_service import OperationalService
from src.persistence import get_dashboard_action_logs, get_dashboard_inventory_tasks, SessionLocal, User, ActionLog, InventoryRecoveryTask, ACTION_EXECUTE_GOVERNANCE_ACTIONS, ACTION_CANCEL_GOVERNANCE_ACTIONS
from src.persistence.inventory_recovery_dual_write import sync_inventory_recovery_task
from src.governance.action_gateway import ActionAuthorizationError, ActionConflictError, ActionExecutionFailedError, ActionGateway, ActionInProgressError, ActionIntent, ExecutionContext
from src.runtime.release_info import get_release_info
from src.governance.query_policy import ExperimentValidationError, assert_query_governance_allowed
from src.query_governance.relevance_guidance import RelevanceGuidanceService
from fastapi import APIRouter
from ..dependencies import *  # noqa: F401,F403
from ..shared import *  # noqa: F401,F403
from .actions import *  # noqa: F401,F403

router = APIRouter()
_operational_service = OperationalService()

@router.get("/api/dashboard/blacklist-logs")
def list_blacklist_logs(
    date: Optional[str] = None,
    brand: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(check_page_permission("/blacklist"))
):
    # 品牌权限过滤
    user_brands = _user_brand_permissions(user)
    if brand and brand not in user_brands:
        return {"total": 0, "items": []}

    filters = {"date": date, "brand": brand, "search": search}
    if not brand: filters["brands"] = user_brands

    paginated_results = get_dashboard_action_logs(1, filters, page, limit) # 1: 拉黑
    results = paginated_results["items"]
    total = paginated_results["total"]

    db = SessionLocal()
    try:
        output = []
        search_snapshot_cache: dict[tuple[str, str], dict[str, Any]] = {}
        for r in results:
            if r.brand not in user_brands: continue

            # 检查该拉黑记录是否已被取消 (type=5)
            cancel_log = db.query(ActionLog).filter(
                ActionLog.brand == r.brand,
                ActionLog.product_id == r.product_id,
                ActionLog.query_text == r.query_text,
                ActionLog.action_type == 5,
                ActionLog.created_at > r.created_at
            ).first()

            image_url = getattr(r, "image_url", None)
            if not image_url:
                cache_key = (str(r.brand), str(r.query_text))
                if cache_key not in search_snapshot_cache:
                    from src.persistence.models import OperationLog
                    latest = db.query(OperationLog.response_payload).filter(
                        OperationLog.brand == r.brand,
                        OperationLog.query_text == r.query_text,
                        OperationLog.module == "OperationalService",
                        OperationLog.action == "POST mixed",
                        OperationLog.status == "success",
                        OperationLog.response_payload.isnot(None),
                    ).order_by(OperationLog.created_at.desc()).first()
                    rows = []
                    if latest and latest[0]:
                        try:
                            rows = json.loads(latest[0]).get("data", {}).get("rows", [])
                        except (TypeError, json.JSONDecodeError):
                            rows = []
                    search_snapshot_cache[cache_key] = {
                        _product_key(row): _extract_product_image(row)
                        for row in rows if isinstance(row, dict) and _product_key(row)
                    }
                image_url = search_snapshot_cache[cache_key].get(str(r.product_id))
            output.append({
                "id": r.id,
                "productName": r.product_name or r.product_id,
                "imageUrl": image_url,
                "sku": r.product_id,
                "brand": r.brand,
                "query": r.query_text,
                "reason": r.reason,
                "createdAt": r.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "isCanceled": cancel_log is not None
            })
        return {"total": total, "items": output}
    finally:
        db.close()

@router.get("/api/dashboard/demoted-logs")
def list_demoted_logs(
    date: Optional[str] = None,
    brand: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(check_page_permission("/demotion"))
):
    user_brands = _user_brand_permissions(user)
    if brand and brand not in user_brands:
        return {"total": 0, "items": []}

    filters = {"date": date, "brand": brand, "search": search}
    if not brand: filters["brands"] = user_brands

    paginated_results = get_dashboard_action_logs(2, filters, page, limit) # 2: 降权
    results = paginated_results["items"]
    total = paginated_results["total"]

    db = SessionLocal()
    try:
        output = []
        for r in results:
            if r.brand not in user_brands: continue
            # 检查该降权记录是否已被恢复 (type=3)
            cancel_log = db.query(ActionLog).filter(
                ActionLog.brand == r.brand,
                ActionLog.product_id == r.product_id,
                ActionLog.query_text == r.query_text,
                ActionLog.action_type == 3,
                ActionLog.created_at > r.created_at
            ).first()

            output.append({
                "id": r.id,
                "productName": r.product_name or r.product_id,
                "imageUrl": getattr(r, "image_url", None),
                "sku": r.product_id,
                "brand": r.brand,
                "query": r.query_text,
                "reason": r.reason,
                "createdAt": r.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "isCanceled": cancel_log is not None
            })
        return {"total": total, "items": output}
    finally:
        db.close()

@router.get("/api/dashboard/stock-tasks")
def list_stock_tasks(
    brand: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(check_page_permission("/stock-tracking"))
):
    user_brands = _user_brand_permissions(user)
    if brand and brand not in user_brands:
        return {"total": 0, "items": []}

    filters = {"brand": brand, "status": status, "search": search}
    if not brand: filters["brands"] = user_brands

    paginated_results = get_dashboard_inventory_tasks(filters, page, limit)
    results = paginated_results["items"]
    total = paginated_results["total"]

    output = []
    for r in results:
        if r.brand not in user_brands: continue
        output.append({
            "id": r.id,
            "productName": r.product_name or r.product_id,
            "imageUrl": getattr(r, "image_url", None),
            "sku": r.product_id,
            "brand": r.brand,
            "query": r.original_query,
            "lastStock": r.last_check_stock,
            "lastBrokenRate": r.last_check_stock,
            "status": r.status,
            "createdAt": r.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "updatedAt": r.updated_at.strftime('%Y-%m-%d %H:%M:%S')
        })
    return {"total": total, "items": output}

@router.post("/api/dashboard/stock-tasks/{task_id}/toggle-status")
def toggle_stock_task_status(
    task_id: int,
    payload: Dict = Body(...),
    user: User = Depends(get_current_user)
):
    from src.persistence import ActionAttempt, SessionLocal, InventoryRecoveryTask
    new_status = payload.get("status")

    if new_status not in ['monitoring', 'manually_stopped']:
        raise HTTPException(status_code=400, detail="Invalid status")

    db = SessionLocal()
    task = db.get(InventoryRecoveryTask, task_id)
    if not task:
        db.close()
        raise HTTPException(status_code=404, detail="Task not found")

    user_brands = _user_brand_permissions(user)
    if task.brand not in user_brands:
        db.close()
        raise HTTPException(status_code=403, detail="Permission denied for this brand")

    db.close()
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    action_payload = {
        "task_id": task_id,
        "brand": task.brand,
        "query": task.original_query,
        "product_id": task.product_id,
        "status": new_status,
    }
    supplied_idempotency_key = str(payload.get("idempotencyKey") or "").strip()
    if supplied_idempotency_key:
        idempotency_key = supplied_idempotency_key
    elif task.status == new_status and task.last_action_request_id:
        idempotency_key = str(task.last_action_request_id)
    else:
        predecessor = (
            f"{task.last_action_request_id}:{int(task.last_action_generation or 0)}"
            if task.last_action_request_id
            else "initial"
        )
        transition_fact = (
            f"{task_id}:{str(task.status or '')}:{new_status}:{predecessor}"
        )
        transition_hash = hashlib.sha256(
            transition_fact.encode("utf-8")
        ).hexdigest()
        idempotency_key = f"inventory-toggle-status:{task_id}:{transition_hash}"
    intent = ActionIntent(
        action_type="inventory.toggle_status",
        idempotency_key=idempotency_key,
        payload=action_payload,
        operator=str(user.username),
        brand=str(task.brand),
        release=str(get_release_info().get("releaseSha") or ""),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        query_text=str(task.original_query or ""),
        target=str(task.product_id or ""),
    )

    def execute_status_update(context: ExecutionContext) -> Dict[str, Any]:
        transaction = context.transaction
        if transaction is None:
            raise RuntimeError("inventory action requires gateway transaction")
        current = transaction.get(InventoryRecoveryTask, task_id)
        if current is None:
            raise RuntimeError("inventory task disappeared before execution")
        if (
            str(current.brand or "") != str(intent.brand)
            or str(current.original_query or "") != str(intent.query_text or "")
            or str(current.product_id or "") != str(intent.target or "")
        ):
            raise RuntimeError("inventory task scope changed before execution")
        current.status = new_status
        current.updated_at = datetime.datetime.now()
        current.last_action_request_id = context.external_request_id
        current.last_action_generation = context.generation
        sync_inventory_recovery_task(transaction, current)
        return {
            "status": "success",
            "task_id": task_id,
            "new_status": new_status,
            "external_request_id": context.external_request_id,
            "generation": context.generation,
        }

    def readback_status(context: ExecutionContext) -> Dict[str, Any]:
        readback_db = SessionLocal()
        try:
            current = readback_db.get(InventoryRecoveryTask, task_id)
            if current is None:
                return {"status": "failed", "reason": "task_not_found"}
            marker_matches = (
                str(current.last_action_request_id or "")
                == context.external_request_id
                and int(current.last_action_generation or 0) == context.generation
            )
            if current.status != new_status or not marker_matches:
                return {
                    "status": "failed",
                    "reason": (
                        "status_mismatch"
                        if current.status != new_status
                        else "request_marker_mismatch"
                    ),
                    "actual_status": current.status,
                }
            return {
                "status": "success",
                "task_id": task_id,
                "status_value": current.status,
                "external_request_id": context.external_request_id,
                "generation": context.generation,
            }
        finally:
            readback_db.close()

    def fence_status_update(context: ExecutionContext) -> Dict[str, Any]:
        fence_db = SessionLocal()
        try:
            attempt = fence_db.get(ActionAttempt, context.attempt_id)
            if (
                attempt is None
                or attempt.status != "fencing"
                or attempt.claim_token != context.claim_token
                or int(attempt.execution_generation or 0) != context.generation
                or str(attempt.external_request_id or "")
                != context.external_request_id
            ):
                raise RuntimeError("inventory database fence claim is not current")
            current = fence_db.get(InventoryRecoveryTask, task_id)
            return {
                "status": "fenced",
                "method": "shared_database_transaction_cas",
                "task_id": task_id,
                "external_request_id": context.external_request_id,
                "generation": context.generation,
                "observed_request_id": (
                    str(current.last_action_request_id or "")
                    if current is not None
                    else None
                ),
                "observed_generation": (
                    int(current.last_action_generation or 0)
                    if current is not None
                    else None
                ),
            }
        finally:
            fence_db.close()

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.brand in user_brands
            and _has_action_permission(user, candidate.permission)
        )
    )
    result = gateway.execute(
        intent,
        execute_status_update,
        readback=readback_status,
        stale_execution_fence=fence_status_update,
        atomic_local_transaction=True,
    )
    if not isinstance(result, dict) or result.get("status") != "success":
        raise HTTPException(status_code=500, detail="Status update could not be verified")
    return {"msg": "Status updated"}

def _cancel_blacklist_via_gateway(
    *,
    payload: Dict[str, Any],
    user: User,
    brand: str,
    query: str,
    sku: str,
) -> str:
    operator = str(user.username or user.id or "unknown")
    request_id, business_payload = _admin_request_id(
        payload,
        action_type="dashboard.cancel_blacklist",
        operator=operator,
        target=sku,
    )
    intent = ActionIntent(
        action_type="dashboard.cancel_blacklist",
        idempotency_key=request_id,
        payload=business_payload,
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_CANCEL_GOVERNANCE_ACTIONS,
        query_text=query,
        target=sku,
    )
    cancel_reason = str(payload.get("reason") or "").strip()
    correction_rule = ""
    if "correctionRule" in payload:
        correction_rule = str(payload.get("correctionRule") or "").strip()
        if not correction_rule:
            raise HTTPException(status_code=400, detail="correctionRule must not be empty")

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        success = service.remove_from_blacklist(
            brand,
            sku,
            [query],
            external_request_id=context.external_request_id,
        )
        if not success:
            raise RuntimeError("dashboard_cancel_blacklist_executor_failed")
        if context.transaction is None:
            raise RuntimeError("dashboard_cancel_blacklist_local_transaction_missing")
        context.transaction.add(ActionLog(
            brand=brand,
            query_text=query,
            product_id=sku,
            product_name="手动取消拉黑",
            action_type=5,
            reason=cancel_reason or f"运营({user.username})手动撤回",
        ))
        if cancel_reason:
            # 取消理由只作审计记录，永不自动生成相关性规则
            RelevanceGuidanceService(SessionLocal).record_cancel_reason(
                brand,
                query,
                cancel_reason,
                product_id=sku,
                operator=operator,
                session=context.transaction,
            )
        if correction_rule:
            RelevanceGuidanceService(SessionLocal).append_rule(
                brand,
                query,
                correction_rule,
                operator=operator,
                session=context.transaction,
            )
        return {
            "status": "success",
            "summary": "已执行取消拉黑",
            "externalRequestId": context.external_request_id,
        }

    # 上游写接口返回业务成功即视为生效，不再发起 product/list 权威回读。
    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand in _user_brand_permissions(user)
            and _has_action_permission(user, candidate.permission)
        )
    )
    try:
        result = gateway.execute(
            intent,
            execute,
            atomic_local_transaction=True,
        )
    except ActionConflictError as exc:
        raise HTTPException(status_code=409, detail="Action idempotency conflict") from exc
    except ActionInProgressError as exc:
        raise HTTPException(status_code=409, detail="Action is still in progress") from exc
    except ActionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="Action authorization denied") from exc
    except ActionExecutionFailedError as exc:
        raise HTTPException(status_code=500, detail="API call failed") from exc
    if not isinstance(result, dict) or result.get("status") != "success":
        raise HTTPException(status_code=409, detail="Action verification failed")
    return request_id


def _blacklist_product_via_gateway(
    *,
    payload: Dict[str, Any],
    user: User,
    brand: str,
    query: str,
    sku: str,
) -> str:
    operator = str(user.username or user.id or "unknown")
    request_id, business_payload = _admin_request_id(
        payload,
        action_type="dashboard.blacklist_product",
        operator=operator,
        target=sku,
    )
    intent = ActionIntent(
        action_type="dashboard.blacklist_product",
        idempotency_key=request_id,
        payload=business_payload,
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        query_text=query,
        target=sku,
    )
    product_name = str(payload.get("productName") or payload.get("product_name") or sku).strip()
    reason = str(payload.get("reason") or "运营手动拉黑相关性错误商品").strip()

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        success = service.add_to_blacklist(
            brand,
            sku,
            [query],
            external_request_id=context.external_request_id,
        )
        if not success:
            raise RuntimeError("dashboard_blacklist_product_executor_failed")
        if context.transaction is None:
            raise RuntimeError("dashboard_blacklist_product_local_transaction_missing")
        context.transaction.add(ActionLog(
            brand=brand,
            query_text=query,
            product_id=sku,
            product_name=product_name,
            action_type=1,
            reason=reason,
        ))
        return {
            "status": "success",
            "summary": "已执行商品拉黑",
            "externalRequestId": context.external_request_id,
        }

    # 上游写接口返回业务成功即视为生效，不再发起 product/list 权威回读。
    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand in _user_brand_permissions(user)
            and _has_action_permission(user, candidate.permission)
        )
    )
    try:
        result = gateway.execute(
            intent,
            execute,
            atomic_local_transaction=True,
        )
    except ActionConflictError as exc:
        raise HTTPException(status_code=409, detail="Action idempotency conflict") from exc
    except ActionInProgressError as exc:
        raise HTTPException(status_code=409, detail="Action is still in progress") from exc
    except ActionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="Action authorization denied") from exc
    except ActionExecutionFailedError as exc:
        raise HTTPException(status_code=500, detail="API call failed") from exc
    if not isinstance(result, dict) or result.get("status") != "success":
        raise HTTPException(status_code=409, detail="Action verification failed")
    return request_id


def _cancel_demotion_via_gateway(
    *,
    payload: Dict[str, Any],
    user: User,
    brand: str,
    query: str,
    sku: str,
) -> str:
    operator = str(user.username or user.id or "unknown")
    request_id, business_payload = _admin_request_id(
        payload,
        action_type="dashboard.cancel_demotion",
        operator=operator,
        target=sku,
    )
    intent = ActionIntent(
        action_type="dashboard.cancel_demotion",
        idempotency_key=request_id,
        payload=business_payload,
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_CANCEL_GOVERNANCE_ACTIONS,
        query_text=query,
        target=sku,
    )

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        success = service.sync_broken_code_to_redis(
            brand,
            remove_items={query: [sku]},
            external_request_id=context.external_request_id,
            allow_prod_recovery=True,
        )
        if not success:
            raise RuntimeError("dashboard_cancel_demotion_executor_failed")
        return {
            "status": "success",
            "summary": "已执行取消降权",
            "externalRequestId": context.external_request_id,
        }

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        observed = service.get_ops_low_weight_config(brand, query)
        if not isinstance(observed, dict) or observed.get("success") is not True:
            return {"status": "inconclusive", "reason": "authoritative_readback_unavailable"}
        product_ids = observed.get("product_ids")
        if not isinstance(product_ids, list):
            return {"status": "inconclusive", "reason": "authoritative_readback_invalid"}
        normalized_ids = {str(product_id).strip() for product_id in product_ids}
        if sku in normalized_ids:
            return {"status": "failed", "reason": "target_remains_demoted"}
        if context.transaction is None:
            raise RuntimeError("dashboard_cancel_demotion_local_transaction_missing")

        context.transaction.add(ActionLog(
            brand=brand,
            query_text=query,
            product_id=sku,
            product_name="手动取消降权",
            action_type=3,
            reason=f"运营({user.username})手动撤回",
        ))
        recovered_tasks = context.transaction.query(InventoryRecoveryTask).filter(
            InventoryRecoveryTask.brand == brand,
            InventoryRecoveryTask.original_query == query,
            InventoryRecoveryTask.product_id == sku,
            InventoryRecoveryTask.status == "monitoring",
        ).all()
        for recovered_task in recovered_tasks:
            recovered_task.status = "recovered"
            recovered_task.last_check_stock = -1
            recovered_task.updated_at = datetime.datetime.now()
            sync_inventory_recovery_task(context.transaction, recovered_task)
        return {
            "status": "success",
            "externalRequestId": context.external_request_id,
            "version": observed.get("version"),
        }

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand in _user_brand_permissions(user)
            and _has_action_permission(user, candidate.permission)
        )
    )
    try:
        result = gateway.execute(
            intent,
            execute,
            readback=readback,
            atomic_local_transaction=True,
            authoritative_readback_on_success=True,
        )
    except ActionConflictError as exc:
        raise HTTPException(status_code=409, detail="Action idempotency conflict") from exc
    except ActionInProgressError as exc:
        raise HTTPException(status_code=409, detail="Action is still in progress") from exc
    except ActionAuthorizationError as exc:
        raise HTTPException(status_code=403, detail="Action authorization denied") from exc
    except ActionExecutionFailedError as exc:
        raise HTTPException(status_code=500, detail="API call failed") from exc
    if not isinstance(result, dict) or result.get("status") != "success":
        raise HTTPException(status_code=409, detail="Action verification failed")
    return request_id


@router.post("/api/dashboard/actions/cancel-blacklist")
def cancel_blacklist(payload: Dict = Body(...), user: User = Depends(get_current_user)):
    _ensure_action_permission(user, ACTION_CANCEL_GOVERNANCE_ACTIONS)
    brand = str(payload.get("brand") or "").strip()
    query = str(payload.get("query") or "").strip()
    sku = str(payload.get("sku") or "").strip()

    if brand not in _user_brand_permissions(user):
        raise HTTPException(status_code=403, detail="No permission for this brand")
    if not all([brand, query, sku]):
        raise HTTPException(status_code=400, detail="Missing parameters")
    try:
        assert_query_governance_allowed(brand, query)
    except ExperimentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if brand == "summit":
        raise HTTPException(
            status_code=409,
            detail="Authoritative blacklist readback is unavailable for this brand",
        )

    request_id = _cancel_blacklist_via_gateway(
        payload=payload,
        user=user,
        brand=brand,
        query=query,
        sku=sku,
    )
    return {"code": 0, "msg": "ok", "requestId": request_id}


@router.post("/api/dashboard/actions/blacklist")
def blacklist_product(payload: Dict = Body(...), user: User = Depends(get_current_user)):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    brand = str(payload.get("brand") or "").strip()
    query = str(payload.get("query") or "").strip()
    sku = str(payload.get("sku") or "").strip()

    if brand not in _user_brand_permissions(user):
        raise HTTPException(status_code=403, detail="No permission for this brand")
    if not all([brand, query, sku]):
        raise HTTPException(status_code=400, detail="Missing parameters")
    try:
        assert_query_governance_allowed(brand, query)
    except ExperimentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    request_id = _blacklist_product_via_gateway(
        payload=payload,
        user=user,
        brand=brand,
        query=query,
        sku=sku,
    )
    return {"code": 0, "msg": "ok", "requestId": request_id}

@router.post("/api/dashboard/actions/cancel-blacklist/batch")
def cancel_blacklist_batch(payload: Dict = Body(...), user: User = Depends(get_current_user)):
    """Batch cancel blacklist rows while preserving per-item authorization/audit."""
    _ensure_action_permission(user, ACTION_CANCEL_GOVERNANCE_ACTIONS)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="items must be a non-empty list")

    def cancel_item(item: Any) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {"status": "failed", "detail": "invalid item"}
        try:
            brand = str(item.get("brand") or "").strip()
            query = str(item.get("query") or "").strip()
            sku = str(item.get("sku") or "").strip()
            if not all([brand, query, sku]):
                raise HTTPException(status_code=400, detail="Missing parameters")
            if brand not in _user_brand_permissions(user):
                raise HTTPException(status_code=403, detail="No permission for this brand")
            assert_query_governance_allowed(brand, query)
            if brand == "summit":
                raise HTTPException(status_code=409, detail="Authoritative blacklist readback is unavailable for this brand")
            request_id = _cancel_blacklist_via_gateway(payload=item, user=user, brand=brand, query=query, sku=sku)
            return {"brand": brand, "query": query, "sku": sku, "status": "success", "requestId": request_id}
        except HTTPException as exc:
            return {"brand": item.get("brand"), "query": item.get("query"), "sku": item.get("sku"), "status": "failed", "detail": exc.detail}
        except Exception as exc:
            return {"brand": item.get("brand"), "query": item.get("query"), "sku": item.get("sku"), "status": "failed", "detail": str(exc)}

    # Each item has its own ActionGateway intent and transaction. Run the
    # independent remote write/readback chains concurrently, while the shared
    # business request governor still caps in-flight calls at four.
    max_workers = min(4, len(items))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(cancel_item, items))
    succeeded = sum(1 for result in results if result["status"] == "success")
    return {"code": 0 if succeeded else 1, "msg": "ok" if succeeded else "批量取消拉黑失败", "succeeded": succeeded, "failed": len(results) - succeeded, "items": results}


@router.post("/api/dashboard/actions/blacklist/batch")
def blacklist_batch(payload: Dict = Body(...), user: User = Depends(get_current_user)):
    """Batch blacklist products while preserving one guarded action per item."""
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="items must be a non-empty list")

    def blacklist_item(item: Any) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {"status": "failed", "detail": "invalid item"}
        try:
            brand = str(item.get("brand") or "").strip()
            query = str(item.get("query") or "").strip()
            sku = str(item.get("sku") or "").strip()
            if not all([brand, query, sku]):
                raise HTTPException(status_code=400, detail="Missing parameters")
            if brand not in _user_brand_permissions(user):
                raise HTTPException(status_code=403, detail="No permission for this brand")
            assert_query_governance_allowed(brand, query)
            request_id = _blacklist_product_via_gateway(payload=item, user=user, brand=brand, query=query, sku=sku)
            return {"brand": brand, "query": query, "sku": sku, "status": "success", "requestId": request_id}
        except HTTPException as exc:
            return {"brand": item.get("brand"), "query": item.get("query"), "sku": item.get("sku"), "status": "failed", "detail": exc.detail}
        except Exception as exc:
            return {"brand": item.get("brand"), "query": item.get("query"), "sku": item.get("sku"), "status": "failed", "detail": str(exc)}

    max_workers = min(4, len(items))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(blacklist_item, items))
    succeeded = sum(1 for result in results if result["status"] == "success")
    return {"code": 0 if succeeded else 1, "msg": "ok" if succeeded else "批量拉黑失败", "succeeded": succeeded, "failed": len(results) - succeeded, "items": results}

@router.post("/api/dashboard/actions/cancel-demotion")
def cancel_demotion(payload: Dict = Body(...), user: User = Depends(get_current_user)):
    _ensure_action_permission(user, ACTION_CANCEL_GOVERNANCE_ACTIONS)
    brand = str(payload.get("brand") or "").strip()
    query = str(payload.get("query") or "").strip()
    sku = str(payload.get("sku") or "").strip()

    if brand not in _user_brand_permissions(user):
        raise HTTPException(status_code=403, detail="No permission for this brand")

    if not all([brand, query, sku]):
        raise HTTPException(status_code=400, detail="Missing parameters")
    try:
        assert_query_governance_allowed(brand, query)
    except ExperimentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if brand == "summit":
        raise HTTPException(
            status_code=409,
            detail="Authoritative demotion write is unavailable for this brand",
        )

    request_id = _cancel_demotion_via_gateway(
        payload=payload,
        user=user,
        brand=brand,
        query=query,
        sku=sku,
    )
    return {"code": 0, "msg": "ok", "requestId": request_id}

__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
