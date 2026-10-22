"""Shared governed-action helpers for API application handlers."""

from __future__ import annotations

import hashlib
import hmac
from fastapi import HTTPException
from typing import Dict, Any
import datetime
import json
from src.persistence import User
from src.governance.action_gateway import ActionAuthorizationError, ActionConflictError, ActionExecutionFailedError, ActionGateway, ActionInProgressError, ActionIntent
from src.runtime.release_info import get_release_info
from ..dependencies import *  # noqa: F401,F403

class _GovernedActionFailure(RuntimeError):
    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class _GovernedExternalWriteUnknown(RuntimeError):
    ambiguous_external = True


def _admin_scope_fact(user: User) -> str:
    if _is_platform_admin(user):
        return "platform"
    encoded = json.dumps(
        sorted(_user_brand_permissions(user)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "brand:" + hmac.new(
        SECRET_KEY.encode("utf-8"),
        encoded.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:44]


def _admin_request_id(
    payload: Dict[str, Any],
    *,
    action_type: str,
    operator: str,
    target: str | None,
) -> tuple[str, Dict[str, Any]]:
    business_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"idempotencyKey", "requestId", "externalRequestId"}
    }
    supplied = str(
        payload.get("idempotencyKey")
        or payload.get("requestId")
        or payload.get("externalRequestId")
        or ""
    ).strip()
    if supplied:
        return supplied, business_payload
    encoded = json.dumps(
        business_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    target_fact = str(target or "collection")
    return f"{action_type}:{operator}:{target_fact}:{payload_hash}", business_payload


def _temporary_credential_for_request(request_id: str, purpose: str) -> str:
    digest = hmac.new(
        SECRET_KEY.encode("utf-8"),
        f"{purpose}:{request_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"Aiops-{digest[:20]}!"


def _run_admin_action(
    *,
    user: User,
    action_type: str,
    permission: str,
    payload: Dict[str, Any],
    target: str | None,
    executor: Any,
    readback: Any,
    atomic_local_transaction: bool = True,
) -> tuple[Dict[str, Any], str]:
    operator = str(user.id or user.username or "unknown")
    request_id, business_payload = _admin_request_id(
        payload,
        action_type=action_type,
        operator=operator,
        target=target,
    )
    scope_fact = _admin_scope_fact(user)
    intent = ActionIntent(
        action_type=action_type,
        idempotency_key=request_id,
        payload=business_payload,
        operator=operator,
        brand=scope_fact,
        release=str(get_release_info().get("releaseSha") or "local"),
        permission=permission,
        target=target,
    )
    gateway = ActionGateway(
        authorizer=lambda candidate: (
            candidate.operator == operator
            and candidate.brand == scope_fact
            and _has_action_permission(user, permission)
        )
    )
    try:
        result = gateway.execute(
            intent,
            executor,
            readback=readback,
            atomic_local_transaction=atomic_local_transaction,
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
        code = str(failure.get("error") or "action_execution_failed")
        status_code = {
            "username_exists": 400,
            "user_not_found": 404,
            "cannot_delete_admin": 400,
            "no_permission_to_manage_user": 403,
            "session_revocation_conflict": 409,
            "assistant_config_reference_invalid": 400,
            "assistant_config_scope_denied": 403,
            "assistant_config_historical_reference": 409,
            "assistant_config_clear_confirmation_required": 409,
            "node_model_config_conflict": 409,
            "node_model_config_not_found": 404,
            "node_model_config_invalid": 400,
            "page_config_conflict": 409,
        }.get(code, 500)
        detail = {
            "username_exists": "Username already exists",
            "user_not_found": "User not found",
            "cannot_delete_admin": "Cannot delete admin user",
            "no_permission_to_manage_user": "No permission to manage this user",
            "session_revocation_conflict": "Session revocation conflict",
            "assistant_config_reference_invalid": "Assistant config reference is invalid",
            "assistant_config_scope_denied": "Assistant config reference is outside the authorized scope",
            "assistant_config_historical_reference": "Assistant config is referenced by historical sessions",
            "assistant_config_clear_confirmation_required": "Explicit confirmation is required to clear assistant config",
            "node_model_config_conflict": "Node model config version conflict",
            "node_model_config_not_found": "Node model config version not found",
            "node_model_config_invalid": "Node model config payload is invalid",
            "page_config_conflict": "Page config version conflict",
        }.get(code, "Action execution failed")
        raise HTTPException(status_code=status_code, detail=detail) from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Action result contract invalid")
    if result.get("status") == "inconclusive":
        raise HTTPException(status_code=409, detail="Action verification inconclusive")
    if result.get("status") == "failed":
        raise HTTPException(status_code=500, detail="Action verification failed")
    return result, request_id


def _managed_user_or_fail(db: Any, manager: User, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise _GovernedActionFailure("user_not_found", 404)
    if not _can_manage_user(manager, target):
        raise _GovernedActionFailure("no_permission_to_manage_user", 403)
    return target


def _authoritative_user_fact(user: User) -> Dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "password_hash": user.password_hash,
        "page_permissions": _json_list(user.page_permissions),
        "brand_permissions": _json_list(user.brand_permissions),
        "action_permissions": _json_list(user.action_permissions),
        "role": user.role,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def _stage_revoke_user_sessions(db: Any, user_id: int) -> datetime.datetime:
    target = db.get(User, user_id)
    if target is None:
        raise _GovernedActionFailure("user_not_found", 404)
    previous = target.updated_at
    filters = [User.id == user_id]
    if previous is None:
        filters.append(User.updated_at.is_(None))
    else:
        filters.append(User.updated_at == previous)
    next_version = _next_user_authorization_version(previous)
    updated_rows = db.query(User).filter(*filters).update(
        {User.updated_at: next_version},
        synchronize_session=False,
    )
    if updated_rows != 1:
        raise _GovernedActionFailure("session_revocation_conflict", 409)
    db.flush()
    return next_version

__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
