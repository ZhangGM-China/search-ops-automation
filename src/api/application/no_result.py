"""No-result governance application handlers."""

from __future__ import annotations

from fastapi import Query, Body, HTTPException, Depends, status
from typing import Optional, Dict, Any
import datetime
import json
from sqlalchemy import func
from src.persistence import SessionLocal, User, NoResultGovernanceAction, NoResultGovernanceBatch, NoResultQuery, ACTION_EXECUTE_GOVERNANCE_ACTIONS, ACTION_MANAGE_NO_RESULT_EXECUTION
from src.governance.action_gateway import ActionAuthorizationError, ActionConflictError, ActionExecutionFailedError, ActionGateway, ActionInProgressError, ActionIntent, ExecutionContext
from src.runtime.release_info import get_release_info
from src.no_result.governance import NoResultGovernanceService
from src.top_query.service import sanitize_action_error
from src.governance.query_policy import ExperimentValidationError, assert_query_governance_allowed
from fastapi import APIRouter
from ..dependencies import *  # noqa: F401,F403
from .actions import *  # noqa: F401,F403
from .query_governance_support import *  # noqa: F401,F403

router = APIRouter()

@router.get("/api/dashboard/no-result-governance")
def list_no_result_governance(
    date: Optional[str] = None,
    brand: Optional[str] = None,
    query_text: Optional[str] = None,
    status_value: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
):
    user_brands = _user_brand_permissions(user)
    if brand and brand not in user_brands:
        return {"total": 0, "items": []}
    brands = [brand] if brand else user_brands
    db = SessionLocal()
    try:
        query = db.query(NoResultQuery).filter(
            NoResultQuery.brand.in_(brands),
            NoResultQuery.search_pv >= 5,
        )
        if date:
            query = query.filter(func.date(NoResultQuery.data_date) == _parse_dashboard_date(date))
        if query_text:
            query = query.filter(NoResultQuery.query_text.contains(query_text))
        if status_value:
            query = query.filter(NoResultQuery.status == status_value)
        total = query.count()
        rows = query.order_by(NoResultQuery.updated_at.desc(), NoResultQuery.created_at.desc()).offset((page - 1) * limit).limit(limit).all()
        return {"total": total, "items": [_serialize_no_result_query(row) for row in rows]}
    finally:
        db.close()

@router.post("/api/dashboard/no-result-governance/batches/{batch_id}/rollback")
def rollback_no_result_batch(
    batch_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    return _rollback_no_result_batch_via_gateway(
        batch_id=batch_id,
        payload=payload,
        user=user,
    )

@router.post("/api/dashboard/no-result-governance/queries/{query_id}/rollback")
def rollback_no_result_query(
    query_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    return _rollback_no_result_query_via_gateway(
        query_id=query_id,
        payload=payload,
        user=user,
    )

@router.post("/api/dashboard/no-result-governance/brands/{brand}/dates/{date}/rollback")
def rollback_no_result_brand_date(
    brand: str,
    date: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    return _rollback_no_result_brand_date_via_gateway(
        brand=brand,
        date=date,
        payload=payload,
        user=user,
    )

def _ensure_no_result_action_permission(action_id: int, user: User) -> NoResultGovernanceAction:
    user_brands = _user_brand_permissions(user)
    db = SessionLocal()
    try:
        action = db.get(NoResultGovernanceAction, action_id)
        if not action:
            raise HTTPException(status_code=404, detail="No result governance action not found")
        if action.brand not in user_brands:
            raise HTTPException(status_code=403, detail="Permission denied for this brand")
        return action
    finally:
        db.close()

def _ensure_no_result_query_permission(query_id: int, user: User) -> None:
    user_brands = _user_brand_permissions(user)
    db = SessionLocal()
    try:
        query = db.get(NoResultQuery, query_id)
        if not query:
            raise HTTPException(status_code=404, detail="No result query not found")
        if query.brand not in user_brands:
            raise HTTPException(status_code=403, detail="Permission denied for this brand")
    finally:
        db.close()


def _has_live_no_result_execution_permission(user: User, brand: str) -> bool:
    """Authorize execution from the current persisted user state, not request data."""
    if user.id is None:
        return False
    db = SessionLocal()
    try:
        current_user = db.get(User, user.id)
        if (
            current_user is None
            or not bool(current_user.is_active)
            or not user.username
            or current_user.username != user.username
        ):
            return False
        return (
            _has_action_permission(current_user, ACTION_MANAGE_NO_RESULT_EXECUTION)
            and brand in _user_brand_permissions(current_user)
        )
    except Exception:
        return False
    finally:
        db.close()


def _fence_no_result_query_execution(
    *,
    query_id: int,
    brand: str,
    query_text: str,
    operator: str,
    reason: str,
    external_request_id: str,
) -> Dict[str, Any]:
    """Fence only actions that have not reached an external-write claim."""
    db = SessionLocal()
    try:
        query = (
            db.query(NoResultQuery)
            .filter(NoResultQuery.id == query_id)
            .with_for_update()
            .first()
        )
        if (
            query is None
            or str(query.brand or "").strip() != brand
            or str(query.query_text or "").strip() != query_text
        ):
            return {"status": "not_fenced", "reason": "query_scope_unavailable"}

        expected_note = NoResultGovernanceService._operator_note(
            "execution_started",
            operator,
            reason,
            f"gatewayRequestId={external_request_id}",
        )
        note_present = expected_note in str(query.ai_reason or "").splitlines()
        if not note_present and str(query.status or "") == "execution_running":
            return {"status": "not_fenced", "reason": "execution_owner_unproven"}

        actions = NoResultGovernanceService._query_actions(db, query_id)
        if any(
            str(action.status or "") in {"executing", "execution_unknown"}
            for action in actions
        ):
            return {"status": "not_fenced", "reason": "external_write_unresolved"}

        pending_actions = [
            action for action in actions if str(action.status or "") == "pending_execute"
        ]
        now = datetime.datetime.now()
        if pending_actions:
            pending_ids = [int(action.id) for action in pending_actions if action.id]
            updated = (
                db.query(NoResultGovernanceAction)
                .filter(
                    NoResultGovernanceAction.id.in_(pending_ids),
                    NoResultGovernanceAction.status == "pending_execute",
                )
                .update(
                    {
                        NoResultGovernanceAction.status: "manual_review",
                        NoResultGovernanceAction.rollback_status: "not_required",
                        NoResultGovernanceAction.error_message: (
                            "gateway lease expired before external-write claim"
                        ),
                        NoResultGovernanceAction.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if updated != len(pending_ids):
                db.rollback()
                return {"status": "not_fenced", "reason": "pending_claim_raced"}
            query.status = "manual_required" # type: ignore
            query.updated_at = now # type: ignore
            query.ai_reason = NoResultGovernanceService._append_operator_note( # type: ignore
                query.ai_reason,
                NoResultGovernanceService._operator_note(
                    "execution_fenced", operator, reason, f"gatewayRequestId={external_request_id}"
                ),
            )
        db.commit()
        return {"status": "fenced", "queryId": query_id}
    finally:
        db.close()

@router.get("/api/dashboard/no-result-governance/queries/{query_id}/execution")
def get_no_result_query_execution(
    query_id: int,
    user: User = Depends(get_current_user),
):
    _ensure_no_result_query_permission(query_id, user)
    try:
        return NoResultGovernanceService().get_query_execution_progress(query_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.post("/api/dashboard/no-result-governance/queries/{query_id}/execution/start")
def start_no_result_query_execution(
    query_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_MANAGE_NO_RESULT_EXECUTION)
    _ensure_no_result_query_permission(query_id, user)
    return _run_no_result_query_execution_via_gateway(
        query_id=query_id,
        payload=payload,
        user=user,
        operation="start",
    )

@router.post("/api/dashboard/no-result-governance/queries/{query_id}/execution/pause")
def pause_no_result_query_execution(
    query_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_MANAGE_NO_RESULT_EXECUTION)
    _ensure_no_result_query_permission(query_id, user)
    return _pause_no_result_query_execution_via_gateway(
        query_id=query_id,
        payload=payload,
        user=user,
    )

@router.post("/api/dashboard/no-result-governance/queries/{query_id}/execution/resume")
def resume_no_result_query_execution(
    query_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_MANAGE_NO_RESULT_EXECUTION)
    _ensure_no_result_query_permission(query_id, user)
    return _run_no_result_query_execution_via_gateway(
        query_id=query_id,
        payload=payload,
        user=user,
        operation="resume",
    )


def _run_no_result_query_execution_via_gateway(
    *,
    query_id: int,
    payload: Dict[str, Any],
    user: User,
    operation: str,
) -> Dict[str, Any]:
    if operation not in {"start", "resume"}:
        raise ValueError(f"unsupported No Result execution operation: {operation}")
    operator = str(user.username or user.id or "unknown")
    db = SessionLocal()
    try:
        query = db.get(NoResultQuery, query_id)
        if query is None:
            raise HTTPException(status_code=404, detail="No result query not found")
        brand = str(query.brand or "").strip()
        query_text = str(query.query_text or "").strip()
    finally:
        db.close()

    action_type = f"no_result_query_{operation}"
    request_id, business_payload = _admin_request_id(
        payload,
        action_type=action_type,
        operator=operator,
        target=str(query_id),
    )
    reason = str(business_payload.get("reason") or "").strip()
    intent = ActionIntent(
        action_type=action_type,
        idempotency_key=request_id,
        payload={"operation": operation, "queryId": query_id, "reason": reason},
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_MANAGE_NO_RESULT_EXECUTION,
        query_text=query_text,
        target=str(query_id),
    )

    def execute(_context: ExecutionContext) -> Dict[str, Any]:
        service = NoResultGovernanceService()
        if operation == "start":
            return service.start_query_execution(
                query_id,
                operator=operator,
                reason=reason,
                external_request_id=_context.external_request_id,
                expected_brand=brand,
                expected_query_text=query_text,
            )
        return service.resume_query_execution(
            query_id,
            operator=operator,
            reason=reason,
            external_request_id=_context.external_request_id,
            expected_brand=brand,
            expected_query_text=query_text,
        )

    execution_result: Dict[str, Any] = {}

    def execute_and_capture(context: ExecutionContext) -> Dict[str, Any]:
        result = execute(context)
        execution_result.clear()
        execution_result.update(result)
        return result

    def readback(_context: ExecutionContext) -> Dict[str, Any]:
        readback_db = SessionLocal()
        try:
            current = readback_db.get(NoResultQuery, query_id)
            expected_note = NoResultGovernanceService._operator_note(
                "execution_started",
                operator,
                reason,
                f"gatewayRequestId={_context.external_request_id}",
            )
            actions = NoResultGovernanceService._query_actions(readback_db, query_id)
            unresolved_actions = [
                action
                for action in actions
                if str(action.status or "")
                in {"pending_execute", "executing", "execution_unknown"}
            ]
            if (
                current is None
                or str(current.brand or "").strip() != brand
                or str(current.query_text or "").strip() != query_text
                or expected_note not in str(current.ai_reason or "").splitlines()
                or execution_result.get("status") in {"blocked", "inconclusive"}
                or unresolved_actions
                or any(str(action.status or "") != "success" for action in actions)
            ):
                return {
                    "status": "failed",
                    "reason": "execution_state_not_persisted",
                }
            return {"status": "success", "queryId": query_id}
        finally:
            readback_db.close()

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == brand
            and _has_live_no_result_execution_permission(user, brand)
        )
    )
    try:
        result = gateway.execute(
            intent,
            execute_and_capture,
            readback=readback,
            stale_execution_fence=lambda context: _fence_no_result_query_execution(
                query_id=query_id,
                brand=brand,
                query_text=query_text,
                operator=operator,
                reason=reason,
                external_request_id=context.external_request_id,
            ),
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
        detail = str(failure.get("error") or "No Result query execution failed")
        raise HTTPException(status_code=400, detail=sanitize_action_error(detail)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="No Result query execution failed") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="No Result query execution result invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(
            status_code=409,
            detail="No Result query execution verification inconclusive",
        )
    if result.get("status") == "failed":
        raise HTTPException(
            status_code=409,
            detail="No Result query execution verification failed",
        )
    return result


def _pause_no_result_query_execution_via_gateway(
    *,
    query_id: int,
    payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    operator = str(user.username or user.id or "unknown")
    db = SessionLocal()
    try:
        query = db.get(NoResultQuery, query_id)
        if query is None:
            raise HTTPException(status_code=404, detail="No result query not found")
        brand = str(query.brand or "").strip()
        query_text = str(query.query_text or "").strip()
    finally:
        db.close()

    request_id, business_payload = _admin_request_id(
        payload,
        action_type="no_result_query_pause",
        operator=operator,
        target=str(query_id),
    )
    reason = str(business_payload.get("reason") or "").strip()
    intent = ActionIntent(
        action_type="no_result_query_pause",
        idempotency_key=request_id,
        payload={"queryId": query_id, "reason": reason},
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_MANAGE_NO_RESULT_EXECUTION,
        query_text=query_text,
        target=str(query_id),
    )

    def fail(code: str, detail: str | None = None) -> None:
        error = code if not detail else f"{code}:{sanitize_action_error(detail)}"
        raise _GovernedActionFailure(error, 409)

    def pause_result(current: NoResultQuery, transaction: Any) -> Dict[str, Any]:
        actions = NoResultGovernanceService._query_actions(transaction, query_id)
        pending_count = sum(action.status == "pending_execute" for action in actions)
        success_count = sum(action.status == "success" for action in actions)
        failed_count = sum(
            action.status in {"failed", "manual_review"} for action in actions
        )
        return {
            "queryId": query_id,
            "status": NoResultGovernanceService._query_execution_status(
                str(current.status or ""), pending_count, failed_count
            ),
            "executedActions": success_count,
            "pendingActions": pending_count,
            "actions": [
                NoResultGovernanceService._action_progress(action) for action in actions
            ],
        }

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        transaction = context.transaction
        if transaction is None:
            raise RuntimeError("no_result_pause_transaction_missing")
        current = (
            transaction.query(NoResultQuery)
            .filter(NoResultQuery.id == query_id)
            .with_for_update()
            .first()
        )
        if current is None:
            raise _GovernedActionFailure("no_result_query_not_found", 404)
        if (
            str(current.brand or "").strip() != brand
            or str(current.query_text or "").strip() != query_text
        ):
            fail("no_result_query_scope_changed")
        current_status = str(current.status or "").lower()
        if current_status != "execution_running":
            fail("no_result_query_state_conflict", current_status)

        current.status = "execution_paused"
        current.ai_reason = NoResultGovernanceService._append_operator_note(
            current.ai_reason,
            NoResultGovernanceService._operator_note(
                "execution_paused", operator, reason
            ),
        )
        current.updated_at = datetime.datetime.now()
        transaction.flush()
        return pause_result(current, transaction)

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        transaction = context.transaction
        if transaction is None:
            return {"status": "failed", "reason": "transaction_missing"}
        current = (
            transaction.query(NoResultQuery)
            .populate_existing()
            .filter(NoResultQuery.id == query_id)
            .first()
        )
        expected_note = NoResultGovernanceService._operator_note(
            "execution_paused", operator, reason
        )
        if (
            current is None
            or str(current.status or "").lower() != "execution_paused"
            or expected_note not in str(current.ai_reason or "").splitlines()
        ):
            return {"status": "failed", "reason": "pause_state_not_persisted"}
        return {"status": "success", "queryId": query_id}

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == brand
            and _has_action_permission(user, ACTION_MANAGE_NO_RESULT_EXECUTION)
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
        failure = exc.result if isinstance(exc.result, dict) else {}
        code = str(failure.get("error") or "no_result_query_pause_failed").split(":", 1)[0]
        if code == "no_result_query_not_found":
            raise HTTPException(status_code=404, detail="No result query not found") from exc
        if code == "no_result_query_state_conflict":
            raise HTTPException(
                status_code=409, detail="No Result query is not running"
            ) from exc
        if code == "no_result_query_scope_changed":
            raise HTTPException(status_code=409, detail="No Result query scope changed") from exc
        raise HTTPException(status_code=500, detail="No Result query pause failed") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="No Result query pause failed") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="No Result query pause result invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(status_code=409, detail="No Result query pause verification inconclusive")
    if result.get("status") == "failed":
        raise HTTPException(status_code=409, detail="No Result query pause verification failed")
    return result


def _reject_no_result_action_via_gateway(
    *,
    action: NoResultGovernanceAction,
    payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    """Reject one No Result action with its query state and terminal fact."""
    action_id = int(action.id or 0)
    if action_id <= 0:
        raise HTTPException(status_code=404, detail="No result governance action not found")
    operator = str(user.username or user.id or "unknown")
    brand = str(action.brand or "").strip()
    query_text = str(action.query_text or "").strip()
    request_id, business_payload = _admin_request_id(
        payload,
        action_type="no_result_action_reject",
        operator=operator,
        target=str(action_id),
    )
    reason = str(business_payload.get("reason") or "").strip()
    intent = ActionIntent(
        action_type="no_result_action_reject",
        idempotency_key=request_id,
        payload={**business_payload, "reason": reason},
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
            raise RuntimeError("no_result_rejection_transaction_missing")
        current = (
            db.query(NoResultGovernanceAction)
            .filter(NoResultGovernanceAction.id == action_id)
            .with_for_update()
            .first()
        )
        if current is None:
            raise _GovernedActionFailure("no_result_action_not_found", 404)
        if str(current.brand or "").strip() != brand:
            fail("no_result_action_scope_changed")
        query = (
            db.query(NoResultQuery)
            .filter(NoResultQuery.id == current.query_id)
            .with_for_update()
            .first()
            if current.query_id
            else None
        )
        current_query = str(current.query_text or (query.query_text if query else "") or "").strip()
        if query is not None:
            if str(query.brand or "").strip() != brand:
                fail("no_result_action_scope_changed")
            query_text_fact = str(query.query_text or "").strip()
            if query_text_fact and current_query and query_text_fact != current_query:
                fail("no_result_action_scope_changed")
        try:
            assert_query_governance_allowed(brand, current_query)
        except ExperimentValidationError as exc:
            raise _GovernedActionFailure(
                f"no_result_action_scope_denied:{sanitize_action_error(exc)}",
                400,
            ) from exc
        current_status = str(current.status or "").lower()
        if current_status != "manual_review":
            fail("no_result_action_state_conflict", current_status)

        current_time = datetime.datetime.now()
        operator_note = NoResultGovernanceService._operator_note(
            "rejected",
            operator,
            reason,
        )
        claimed = db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.id == action_id,
            NoResultGovernanceAction.status == "manual_review",
        ).update(
            {
                NoResultGovernanceAction.status: "rejected",
                NoResultGovernanceAction.rollback_status: "not_required",
                NoResultGovernanceAction.error_message: operator_note,
                NoResultGovernanceAction.updated_at: current_time,
            },
            synchronize_session=False,
        )
        if claimed != 1:
            fail("no_result_action_state_conflict")
        if query is not None:
            query.status = "manual_required"
            query.updated_at = current_time
        db.flush()
        return {"actionId": action_id, "status": "rejected"}

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        if db is None:
            return {"status": "failed", "reason": "transaction_missing"}
        current = (
            db.query(NoResultGovernanceAction)
            .populate_existing()
            .filter(NoResultGovernanceAction.id == action_id)
            .first()
        )
        expected_note = NoResultGovernanceService._operator_note(
            "rejected",
            operator,
            reason,
        )
        if (
            current is None
            or str(current.status or "").lower() != "rejected"
            or str(current.rollback_status or "").lower() != "not_required"
            or current.error_message != expected_note
        ):
            return {"status": "failed", "reason": "rejection_state_not_persisted"}
        if current.query_id:
            query = db.get(NoResultQuery, current.query_id)
            if query is None or str(query.status or "").lower() != "manual_required":
                return {"status": "failed", "reason": "query_state_not_persisted"}
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
        failure = exc.result if isinstance(exc.result, dict) else {}
        raw_error = str(failure.get("error") or "no_result_action_rejection_failed")
        code, separator, error_context = raw_error.partition(":")
        status_code = {
            "no_result_action_not_found": 404,
            "no_result_action_scope_denied": 400,
            "no_result_action_scope_changed": 409,
            "no_result_action_state_conflict": 409,
        }.get(code, 500)
        detail = "No Result action rejection failed"
        if separator and code == "no_result_action_scope_denied":
            detail = sanitize_action_error(error_context)
        elif separator and code == "no_result_action_state_conflict":
            detail = f"action is not waiting for manual review: {sanitize_action_error(error_context)}"
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="No Result action rejection failed",
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Action result contract invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(status_code=409, detail="Action rejection verification inconclusive")
    if result.get("status") == "failed":
        raise HTTPException(status_code=409, detail="Action rejection verification failed")
    return result


def _rollback_no_result_query_via_gateway(
    *,
    query_id: int,
    payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    """Rollback one No Result query through the shared action fact gateway."""
    operator = str(user.username or user.id or "unknown")
    db = SessionLocal()
    try:
        query = db.get(NoResultQuery, query_id)
        if query is None:
            raise HTTPException(status_code=404, detail="No result query not found")
        brand = str(query.brand or "").strip()
        query_text = str(query.query_text or "").strip()
        if brand not in _user_brand_permissions(user):
            raise HTTPException(status_code=403, detail="Permission denied for this brand")
        batch_id = int(query.batch_id) if query.batch_id is not None else None
    finally:
        db.close()

    request_id, business_payload = _admin_request_id(
        payload,
        action_type="no_result_query_rollback",
        operator=operator,
        target=str(query_id),
    )
    reason = str(business_payload.get("reason") or "").strip()
    intent = ActionIntent(
        action_type="no_result_query_rollback",
        idempotency_key=request_id,
        payload={**business_payload, "queryId": query_id, "reason": reason},
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        query_text=query_text,
        target=str(query_id),
    )

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        result = NoResultGovernanceService().rollback_query(
            query_id,
            operator=operator,
            reason=reason,
            external_request_id=context.external_request_id,
        )
        if not isinstance(result, dict) or result.get("status") != "rolled_back":
            status = result.get("status") if isinstance(result, dict) else "invalid"
            raise _GovernedActionFailure(
                f"no_result_query_rollback_not_complete:{status}",
                409,
            )
        return result

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            current = db.get(NoResultQuery, query_id)
            batch = db.get(NoResultGovernanceBatch, batch_id) if batch_id else None
            actions = db.query(NoResultGovernanceAction).filter(
                NoResultGovernanceAction.query_id == query_id,
                NoResultGovernanceAction.status == "success",
            ).all()
            if (
                current is None
                or str(current.brand or "").strip() != brand
                or str(current.query_text or "").strip() != query_text
                or (int(current.batch_id) if current.batch_id is not None else None) != batch_id
                or str(current.status or "").strip().lower() != "rolled_back"
            ):
                return {"status": "inconclusive", "reason": "query_scope_readback_invalid"}
            if batch is not None and str(batch.brand or "").strip() != brand:
                return {"status": "inconclusive", "reason": "batch_scope_readback_invalid"}
            for action in actions:
                if str(action.rollback_status or "").strip().lower() not in {"success", "not_required"}:
                    return {"status": "inconclusive", "reason": "action_rollback_readback_invalid"}
                if str(action.rollback_status or "").strip().lower() == "success":
                    rollback_payload = _safe_json_loads(action.rollback_payload, {})
                    action_request_id = str(rollback_payload.get("externalRequestId") or "")
                    if not action_request_id.startswith(context.external_request_id):
                        return {"status": "inconclusive", "reason": "rollback_request_id_readback_invalid"}
            return {
                "status": "success",
                "queryId": query_id,
                "batchId": batch_id,
                "externalRequestId": context.external_request_id,
                "queryStatus": str(current.status),
                "batchStatus": str(batch.status) if batch is not None else None,
                "rolledBackActions": sum(
                    1 for action in actions if str(action.rollback_status or "").lower() == "success"
                ),
            }
        finally:
            db.close()

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == brand
            and _has_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        )
    )
    try:
        result = gateway.execute(
            intent,
            execute,
            readback=readback,
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
        raw_error = str(failure.get("error") or "no_result_query_rollback_failed")
        if raw_error.startswith("no_result_query_rollback_not_complete:"):
            raise HTTPException(status_code=409, detail="No Result query rollback was not completed") from exc
        raise HTTPException(status_code=409, detail="No Result query rollback failed closed") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="No Result query rollback failed") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Action result contract invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(status_code=409, detail="No Result query rollback verification inconclusive")
    if result.get("status") == "failed":
        raise HTTPException(status_code=409, detail="No Result query rollback verification failed")
    return result


def _rollback_no_result_batch_via_gateway(
    *,
    batch_id: int,
    payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    """Rollback one No Result batch through the shared action fact gateway."""
    operator = str(user.username or user.id or "unknown")
    db = SessionLocal()
    try:
        batch = db.get(NoResultGovernanceBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="No result governance batch not found")
        brand = str(batch.brand or "").strip()
        if brand not in _user_brand_permissions(user):
            raise HTTPException(status_code=403, detail="Permission denied for this brand")
    finally:
        db.close()

    request_id, business_payload = _admin_request_id(
        payload,
        action_type="no_result_batch_rollback",
        operator=operator,
        target=str(batch_id),
    )
    reason = str(business_payload.get("reason") or "").strip()
    intent = ActionIntent(
        action_type="no_result_batch_rollback",
        idempotency_key=request_id,
        payload={**business_payload, "batchId": batch_id, "reason": reason},
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        target=str(batch_id),
    )

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        result = NoResultGovernanceService().rollback_batch(
            batch_id,
            operator=operator,
            reason=reason,
            external_request_id=context.external_request_id,
        )
        if not isinstance(result, dict) or result.get("status") != "rolled_back":
            status = result.get("status") if isinstance(result, dict) else "invalid"
            raise _GovernedActionFailure(
                f"no_result_batch_rollback_not_complete:{status}",
                409,
            )
        return result

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            current = db.get(NoResultGovernanceBatch, batch_id)
            if (
                current is None
                or str(current.brand or "").strip() != brand
                or str(current.status or "").strip().lower() != "rolled_back"
            ):
                return {"status": "inconclusive", "reason": "batch_scope_readback_invalid"}
            queries = db.query(NoResultQuery).filter(
                NoResultQuery.batch_id == batch_id,
            ).all()
            if any(str(query.brand or "").strip() != brand for query in queries):
                return {"status": "inconclusive", "reason": "query_brand_readback_invalid"}
            if any(str(query.status or "").strip().lower() != "rolled_back" for query in queries):
                return {"status": "inconclusive", "reason": "query_rollback_readback_invalid"}
            actions = db.query(NoResultGovernanceAction).filter(
                NoResultGovernanceAction.batch_id == batch_id,
                NoResultGovernanceAction.status == "success",
            ).all()
            for action in actions:
                if str(action.rollback_status or "").strip().lower() not in {"success", "not_required"}:
                    return {"status": "inconclusive", "reason": "action_rollback_readback_invalid"}
                if str(action.rollback_status or "").strip().lower() == "success":
                    rollback_payload = _safe_json_loads(action.rollback_payload, {})
                    action_request_id = str(rollback_payload.get("externalRequestId") or "")
                    if not action_request_id.startswith(context.external_request_id):
                        return {"status": "inconclusive", "reason": "rollback_request_id_readback_invalid"}
            return {
                "status": "success",
                "batchId": batch_id,
                "externalRequestId": context.external_request_id,
                "batchStatus": str(current.status),
                "queryCount": len(queries),
                "rolledBackActions": sum(
                    1 for action in actions if str(action.rollback_status or "").lower() == "success"
                ),
            }
        finally:
            db.close()

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == brand
            and _has_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        )
    )
    try:
        result = gateway.execute(
            intent,
            execute,
            readback=readback,
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
        raw_error = str(failure.get("error") or "no_result_batch_rollback_failed")
        if raw_error.startswith("no_result_batch_rollback_not_complete:"):
            raise HTTPException(status_code=409, detail="No Result batch rollback was not completed") from exc
        raise HTTPException(status_code=409, detail="No Result batch rollback failed closed") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="No Result batch rollback failed") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Action result contract invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(status_code=409, detail="No Result batch rollback verification inconclusive")
    if result.get("status") == "failed":
        raise HTTPException(status_code=409, detail="No Result batch rollback verification failed")
    return result


def _rollback_no_result_brand_date_via_gateway(
    *,
    brand: str,
    date: str,
    payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    """Rollback one brand/date scope through the shared action fact gateway."""
    operator = str(user.username or user.id or "unknown")
    normalized_brand = str(brand or "").strip()
    if normalized_brand not in _user_brand_permissions(user):
        raise HTTPException(status_code=403, detail="Permission denied for this brand")
    try:
        target_date = _parse_dashboard_date(date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    start_at = datetime.datetime.combine(target_date, datetime.time.min)
    end_at = start_at + datetime.timedelta(days=1)
    db = SessionLocal()
    try:
        initial_batch_ids = [
            int(row.id)
            for row in db.query(NoResultGovernanceBatch.id).filter(
                NoResultGovernanceBatch.brand == normalized_brand,
                NoResultGovernanceBatch.batch_date >= start_at,
                NoResultGovernanceBatch.batch_date < end_at,
            ).all()
        ]
        pre_settled_action_ids = {
            int(row.id)
            for row in db.query(NoResultGovernanceAction.id).filter(
                NoResultGovernanceAction.batch_id.in_(initial_batch_ids),
                NoResultGovernanceAction.status == "success",
                NoResultGovernanceAction.rollback_status.in_(("success", "not_required")),
            ).all()
        } if initial_batch_ids else set()
    finally:
        db.close()
    target = f"{normalized_brand}:{target_date.isoformat()}"
    request_id, business_payload = _admin_request_id(
        payload,
        action_type="no_result_brand_date_rollback",
        operator=operator,
        target=target,
    )
    reason = str(business_payload.get("reason") or "").strip()
    intent = ActionIntent(
        action_type="no_result_brand_date_rollback",
        idempotency_key=request_id,
        payload={
            **business_payload,
            "brand": normalized_brand,
            "date": target_date.isoformat(),
            "reason": reason,
        },
        operator=operator,
        brand=normalized_brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        target=target,
    )
    execution_result: Optional[Dict[str, Any]] = None

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        nonlocal execution_result
        result = NoResultGovernanceService().rollback_brand_date(
            normalized_brand,
            target_date,
            operator=operator,
            reason=reason,
            external_request_id=context.external_request_id,
        )
        if not isinstance(result, dict) or result.get("status") != "rolled_back":
            status = result.get("status") if isinstance(result, dict) else "invalid"
            raise _GovernedActionFailure(
                f"no_result_brand_date_rollback_not_complete:{status}",
                409,
            )
        execution_result = result
        return result

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            batches = db.query(NoResultGovernanceBatch).filter(
                NoResultGovernanceBatch.brand == normalized_brand,
                NoResultGovernanceBatch.batch_date >= start_at,
                NoResultGovernanceBatch.batch_date < end_at,
            ).all()
            if not batches:
                if execution_result is not None and int(execution_result.get("batchCount") or 0) == 0:
                    return {
                        "status": "success",
                        "brand": normalized_brand,
                        "date": target_date.isoformat(),
                        "externalRequestId": context.external_request_id,
                        "batchCount": 0,
                        "queryCount": 0,
                        "rolledBackActions": 0,
                    }
                return {"status": "inconclusive", "reason": "brand_date_scope_readback_empty"}
            if any(str(batch.status or "").strip().lower() != "rolled_back" for batch in batches):
                return {"status": "inconclusive", "reason": "batch_rollback_readback_invalid"}
            batch_ids = [int(batch.id) for batch in batches]
            queries = db.query(NoResultQuery).filter(
                NoResultQuery.batch_id.in_(batch_ids),
            ).all() if batch_ids else []
            if any(str(query.brand or "").strip() != normalized_brand for query in queries):
                return {"status": "inconclusive", "reason": "query_brand_readback_invalid"}
            if any(str(query.status or "").strip().lower() != "rolled_back" for query in queries):
                return {"status": "inconclusive", "reason": "query_rollback_readback_invalid"}
            actions = db.query(NoResultGovernanceAction).filter(
                NoResultGovernanceAction.batch_id.in_(batch_ids),
                NoResultGovernanceAction.status == "success",
            ).all() if batch_ids else []
            for action in actions:
                if str(action.brand or "").strip() != normalized_brand:
                    return {"status": "inconclusive", "reason": "action_brand_readback_invalid"}
                if str(action.rollback_status or "").strip().lower() not in {"success", "not_required"}:
                    return {"status": "inconclusive", "reason": "action_rollback_readback_invalid"}
                if str(action.rollback_status or "").strip().lower() == "success":
                    rollback_payload = _safe_json_loads(action.rollback_payload, {})
                    action_request_id = str(rollback_payload.get("externalRequestId") or "")
                    if not action_request_id:
                        return {"status": "inconclusive", "reason": "rollback_request_id_readback_missing"}
                    if (
                        int(action.id) not in pre_settled_action_ids
                        and not action_request_id.startswith(context.external_request_id)
                    ):
                        return {"status": "inconclusive", "reason": "rollback_request_id_readback_invalid"}
            return {
                "status": "success",
                "brand": normalized_brand,
                "date": target_date.isoformat(),
                "externalRequestId": context.external_request_id,
                "batchCount": len(batches),
                "queryCount": len(queries),
                "rolledBackActions": sum(
                    1 for action in actions if str(action.rollback_status or "").lower() == "success"
                ),
            }
        finally:
            db.close()

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == normalized_brand
            and _has_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        )
    )
    try:
        result = gateway.execute(
            intent,
            execute,
            readback=readback,
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
        raw_error = str(failure.get("error") or "no_result_brand_date_rollback_failed")
        if raw_error.startswith("no_result_brand_date_rollback_not_complete:"):
            raise HTTPException(status_code=409, detail="No Result brand/date rollback was not completed") from exc
        raise HTTPException(status_code=409, detail="No Result brand/date rollback failed closed") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="No Result brand/date rollback failed") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Action result contract invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(status_code=409, detail="No Result brand/date rollback verification inconclusive")
    if result.get("status") == "failed":
        raise HTTPException(status_code=409, detail="No Result brand/date rollback verification failed")
    return result


def _approve_no_result_action_via_gateway(
    *,
    action: NoResultGovernanceAction,
    payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    """Approve one external KG write with terminal replay and strict readback."""
    action_id = int(action.id or 0)
    if action_id <= 0:
        raise HTTPException(status_code=404, detail="No result governance action not found")
    operator = str(user.username or user.id or "unknown")
    brand = str(action.brand or "").strip()
    query_text = str(action.query_text or "").strip()
    expected_query_id = int(action.query_id) if action.query_id is not None else None
    expected_batch_id = int(action.batch_id) if action.batch_id is not None else None
    request_id, business_payload = _admin_request_id(
        payload,
        action_type="no_result_action_approve",
        operator=operator,
        target=str(action_id),
    )
    reason = str(business_payload.get("reason") or "").strip()
    intent = ActionIntent(
        action_type="no_result_action_approve",
        idempotency_key=request_id,
        payload={**business_payload, "reason": reason},
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        query_text=query_text,
        target=str(action_id),
    )

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        return NoResultGovernanceService().approve_manual_action(
            action_id,
            operator=operator,
            reason=reason,
            external_request_id=context.external_request_id,
        )

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            current = db.get(NoResultGovernanceAction, action_id)
            if (
                current is None
                or str(current.brand or "").strip() != brand
                or str(current.query_text or "").strip() != query_text
                or (int(current.query_id) if current.query_id is not None else None)
                != expected_query_id
                or (int(current.batch_id) if current.batch_id is not None else None)
                != expected_batch_id
            ):
                return {
                    "status": "inconclusive",
                    "reason": "action_scope_readback_invalid",
                }

            request_payload = _safe_json_loads(current.request_payload)
            if (
                not isinstance(request_payload, dict)
                or str(request_payload.get("externalRequestId") or "")
                != context.external_request_id
            ):
                return {
                    "status": "inconclusive",
                    "reason": "external_request_id_readback_invalid",
                }

            action_status = str(current.status or "").strip().lower()
            rollback_status = str(current.rollback_status or "").strip().lower()
            expected_note = NoResultGovernanceService._operator_note(
                "approved",
                operator,
                reason,
            )
            if action_status in {"executing", "execution_unknown"}:
                return {
                    "status": "inconclusive",
                    "reason": "external_write_unresolved",
                }
            if (
                action_status != "success"
                or rollback_status != "pending"
                or current.error_message != expected_note
            ):
                return {
                    "status": "failed",
                    "reason": "approval_state_not_persisted",
                }

            query_status = None
            if expected_query_id is not None:
                query = db.get(NoResultQuery, expected_query_id)
                if (
                    query is None
                    or str(query.brand or "").strip() != brand
                    or str(query.query_text or "").strip() != query_text
                    or str(query.status or "").strip().lower() != "awaiting_retag"
                ):
                    return {
                        "status": "inconclusive",
                        "reason": "query_readback_invalid",
                    }
                query_status = "awaiting_retag"

            batch_status = None
            if expected_batch_id is not None:
                batch = db.get(NoResultGovernanceBatch, expected_batch_id)
                if (
                    batch is None
                    or str(batch.brand or "").strip() != brand
                    or str(batch.status or "").strip().lower() != "awaiting_retag"
                ):
                    return {
                        "status": "inconclusive",
                        "reason": "batch_readback_invalid",
                    }
                batch_status = "awaiting_retag"

            return {
                "status": "success",
                "actionId": action_id,
                "externalRequestId": context.external_request_id,
                "actionStatus": action_status,
                "rollbackStatus": rollback_status,
                "queryStatus": query_status,
                "batchStatus": batch_status,
            }
        finally:
            db.close()

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == brand
            and _has_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        )
    )
    try:
        result = gateway.execute(
            intent,
            execute,
            readback=readback,
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
        raw_error = str(failure.get("error") or "no_result_action_approval_failed")
        lowered = raw_error.lower()
        status_code = 500
        if "not found" in lowered:
            status_code = 404
        elif "not waiting for manual review" in lowered or "claimed concurrently" in lowered:
            status_code = 409
        raise HTTPException(
            status_code=status_code,
            detail="No Result action approval failed",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="No Result action approval failed",
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Action result contract invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(
            status_code=409,
            detail="Action approval verification inconclusive",
        )
    if result.get("status") == "failed":
        raise HTTPException(status_code=409, detail="Action approval verification failed")
    return result


def _execute_no_result_action_via_gateway(
    *,
    action: NoResultGovernanceAction,
    payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    """Execute one external No Result write with durable terminal replay."""
    action_id = int(action.id or 0)
    if action_id <= 0:
        raise HTTPException(status_code=404, detail="No result governance action not found")
    operator = str(user.username or user.id or "unknown")
    brand = str(action.brand or "").strip()
    query_text = str(action.query_text or "").strip()
    query_id = int(action.query_id or 0)
    batch_id = int(action.batch_id or 0)
    request_id, business_payload = _admin_request_id(
        payload,
        action_type="no_result_action_execute",
        operator=operator,
        target=str(action_id),
    )
    reason = str(business_payload.get("reason") or "").strip()
    intent = ActionIntent(
        action_type="no_result_action_execute",
        idempotency_key=request_id,
        payload={**business_payload, "actionId": action_id, "reason": reason},
        operator=operator,
        brand=brand,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=ACTION_EXECUTE_GOVERNANCE_ACTIONS,
        query_text=query_text,
        target=str(action_id),
    )

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        result = NoResultGovernanceService().execute_pending_action(
            action_id,
            operator=operator,
            reason=reason,
            external_request_id=context.external_request_id,
        )
        status = str(result.get("status") or "").strip().lower()
        if status not in {"success", "failed"}:
            result_reason = str(result.get("reason") or "not_executed").strip()
            raise _GovernedActionFailure(
                f"no_result_action_execute_not_performed:{status}:{result_reason}",
                409,
            )
        return result

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        readback_db = SessionLocal()
        try:
            current = readback_db.get(NoResultGovernanceAction, action_id)
            query = readback_db.get(NoResultQuery, query_id) if query_id else None
            batch = readback_db.get(NoResultGovernanceBatch, batch_id) if batch_id else None
            if (
                current is None
                or str(current.brand or "").strip() != brand
                or str(current.query_text or "").strip() != query_text
                or int(current.query_id or 0) != query_id
                or int(current.batch_id or 0) != batch_id
                or (query_id and query is None)
                or (batch_id and batch is None)
            ):
                return {"status": "inconclusive", "reason": "action_scope_readback_invalid"}
            if query is not None and (
                str(query.brand or "").strip() != brand
                or str(query.query_text or "").strip() != query_text
                or int(query.batch_id or 0) != batch_id
            ):
                return {"status": "inconclusive", "reason": "query_scope_readback_invalid"}
            if batch is not None and str(batch.brand or "").strip() != brand:
                return {"status": "inconclusive", "reason": "batch_scope_readback_invalid"}

            request_payload = _safe_json_loads(current.request_payload, None)
            response_payload = _safe_json_loads(current.response_payload, None)
            before_snapshot = _safe_json_loads(current.before_snapshot, None)
            rollback_payload = _safe_json_loads(current.rollback_payload, None)
            action_status = str(current.status or "").strip().lower()
            rollback_status = str(current.rollback_status or "").strip().lower()
            query_status = str(query.status or "").strip().lower() if query is not None else None
            batch_status = str(batch.status or "").strip().lower() if batch is not None else None
            response_succeeded = (
                (
                    response_payload.get("success") is True
                    or NoResultGovernanceService._response_succeeded(response_payload)
                )
                if isinstance(response_payload, dict)
                else False
            )
            expected_note = NoResultGovernanceService._operator_note(
                "executed",
                operator,
                reason,
            )
            note = str(current.error_message or "")
            facts_valid = (
                isinstance(request_payload, dict)
                and request_payload.get("externalRequestId") == context.external_request_id
                and isinstance(response_payload, dict)
                and isinstance(before_snapshot, dict)
                and isinstance(rollback_payload, dict)
                and (note == expected_note or note.startswith(expected_note + " | "))
            )
            if not facts_valid or action_status not in {"success", "failed"}:
                return {"status": "inconclusive", "reason": "external_fact_readback_invalid"}

            expected_rollback_status = "pending" if action_status == "success" else "not_required"
            expected_query_statuses = (
                {"awaiting_retag", "execution_paused"}
                if action_status == "success"
                else {"manual_required", "execution_paused"}
            )
            expected_batch_status = "awaiting_retag" if action_status == "success" else "manual_required"
            if (
                rollback_status != expected_rollback_status
                or response_succeeded != (action_status == "success")
                or (query is not None and query_status not in expected_query_statuses)
                or (batch is not None and batch_status != expected_batch_status)
            ):
                return {"status": "inconclusive", "reason": "terminal_state_readback_invalid"}
            return {
                "status": action_status,
                "actionId": action_id,
                "externalRequestId": context.external_request_id,
                "actionStatus": action_status,
                "rollbackStatus": rollback_status,
                "queryStatus": query_status,
                "batchStatus": batch_status,
                "responseSucceeded": response_succeeded,
                "beforeSnapshot": before_snapshot,
                "responsePayload": response_payload,
                "rollbackPayload": rollback_payload,
            }
        finally:
            readback_db.close()

    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == brand
            and _has_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        )
    )
    try:
        result = gateway.execute(
            intent,
            execute,
            readback=readback,
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
        raw_error = str(failure.get("error") or "no_result_action_execution_failed")
        if raw_error == (
            "no_result_action_execute_not_performed:"
            "pending_execute:real_write_not_authorized"
        ):
            return {
                "actionId": action_id,
                "status": "pending_execute",
                "reason": "real_write_not_authorized",
            }
        if raw_error.startswith("no_result_action_execute_not_performed:"):
            raise HTTPException(status_code=409, detail="No Result action was not executed") from exc
        raise HTTPException(status_code=500, detail="No Result action execution failed") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="No Result action execution failed") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Action result contract invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(status_code=409, detail="Action execution verification inconclusive")
    if result.get("status") == "failed":
        raise HTTPException(status_code=409, detail="Action execution verification failed")
    return result


@router.post("/api/dashboard/no-result-governance/actions/{action_id}/approve")
def approve_no_result_action(
    action_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    action = _ensure_no_result_action_permission(action_id, user)
    return _approve_no_result_action_via_gateway(
        action=action,
        payload=payload,
        user=user,
    )

@router.post("/api/dashboard/no-result-governance/actions/{action_id}/execute")
def execute_no_result_pending_action(
    action_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    action = _ensure_no_result_action_permission(action_id, user)
    return _execute_no_result_action_via_gateway(
        action=action,
        payload=payload,
        user=user,
    )

@router.post("/api/dashboard/no-result-governance/actions/{action_id}/reject")
def reject_no_result_action(
    action_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
    action = _ensure_no_result_action_permission(action_id, user)
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Reject reason is required")
    return _reject_no_result_action_via_gateway(
        action=action,
        payload={**payload, "reason": reason},
        user=user,
    )

__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
