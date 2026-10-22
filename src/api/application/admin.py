"""Administration application handlers."""

from __future__ import annotations

import copy
import math
from fastapi import Body, HTTPException, Depends
from typing import List, Dict, Any
import datetime
import json
from sqlalchemy.exc import IntegrityError
from src.persistence import SessionLocal, User, AssistantBrandSpace, AssistantAgentBinding, AssistantUserBinding, NodeModelConfigVersion, PLATFORM_ADMIN_ROLE, BRAND_MEMBER_ROLE, ACTION_MANAGE_BRAND_USERS, ACTION_MANAGE_PLATFORM_CONFIG, ACTION_MANAGE_BRAND_CONFIG, ACTION_MANAGE_ASSISTANT_ACCESS
from src.governance.action_gateway import ExecutionContext
from src.llm.node_config import NODE_REGISTRY, capture_snapshot
from src.llm.config_store import NodeModelConfigConflict, NodeModelConfigNotFound, NodeModelConfigValidationError, create_draft as create_node_model_config_draft, get_active_bundle as get_active_node_model_config_bundle, publish_draft as publish_node_model_config_draft, rollback_active as rollback_node_model_config_active
from src.assistant.service import PiAgentAssistantService
from src.config import GLOBAL_SAFETY_CONFIG, BRAND_CONFIGS, save_config, reload_config, _raw_config, get_brand_business_config
from fastapi import APIRouter
from ..dependencies import *  # noqa: F401,F403
from ..shared import *  # noqa: F401,F403
from .actions import *  # noqa: F401,F403
from .assistant import *  # noqa: F401,F403
from src.query_governance.relevance_buckets import KNOWN_RELEVANCE_BUCKETS
from src.query_governance.visibility import governance_today, record_check_mode_changes
from src.page_config import apply_page_config_update, page_config_payload, page_config_version, validate_page_config_values

router = APIRouter()

@router.get("/api/admin/users")
def list_users(user: User = Depends(get_current_user)):
    if _is_platform_admin(user):
        allowed_scope = None
    else:
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_USERS, "Only authorized brand admins can access this")
        if not _is_brand_admin(user):
            raise HTTPException(status_code=403, detail="Only authorized brand admins can access this")
        allowed_scope = set(_user_brand_permissions(user))
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.username.asc()).all()
        results = []
        for item in users:
            if allowed_scope is not None:
                if _user_role(item) != BRAND_MEMBER_ROLE:
                    continue
                item_brands = set(_user_brand_permissions(item))
                if not item_brands or not item_brands.issubset(allowed_scope):
                    continue
            results.append(_serialize_user(item))
        return results
    finally:
        db.close()


@router.post("/api/admin/users")
def create_user(payload: Dict = Body(...), user: User = Depends(get_current_user)):
    username = payload.get("username")
    page_perms = payload.get("pagePermissions", [])
    requested_role = str(payload.get("role") or BRAND_MEMBER_ROLE).strip() or BRAND_MEMBER_ROLE
    requested_action_perms = _json_list(payload.get("actionPermissions"))
    if _is_platform_admin(user):
        role = requested_role
        brand_perms = _normalize_brand_permissions(payload.get("brandPermissions", []))
        action_perms = _normalize_action_permissions(requested_action_perms, role)
    else:
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_USERS, "Only authorized brand admins can access this")
        if not _is_brand_admin(user):
            raise HTTPException(status_code=403, detail="Only authorized brand admins can access this")
        role = BRAND_MEMBER_ROLE
        brand_perms = _normalize_brand_permissions(payload.get("brandPermissions", []), _user_brand_permissions(user))
        if not brand_perms:
            brand_perms = _user_brand_permissions(user)[:1]
        action_perms = []
    expected_user_id: Dict[str, int] = {}

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        if db.query(User).filter(User.username == username).first():
            raise _GovernedActionFailure("username_exists", 400)
        temporary_credential = _temporary_credential_for_request(
            context.external_request_id,
            "create-user",
        )
        new_user = User(
            username=username,
            password_hash=get_password_hash(temporary_credential),
            page_permissions=json.dumps(page_perms),
            brand_permissions=json.dumps(brand_perms),
            action_permissions=json.dumps(action_perms, ensure_ascii=False),
            is_admin=1 if role == PLATFORM_ADMIN_ROLE else 0,
            role=role,
        )
        db.add(new_user)
        db.flush()
        expected_user_id["value"] = new_user.id
        return {
            "status": "success",
            "msg": "User created",
            "temporaryPassword": temporary_credential,
            "userId": new_user.id,
        }

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        db.expire_all()
        new_user = db.get(User, expected_user_id.get("value"))
        temporary_credential = _temporary_credential_for_request(
            context.external_request_id,
            "create-user",
        )
        matches = (
            new_user is not None
            and new_user.username == username
            and _json_list(new_user.page_permissions) == list(page_perms)
            and _json_list(new_user.brand_permissions) == list(brand_perms)
            and _json_list(new_user.action_permissions) == list(action_perms)
            and new_user.role == role
            and new_user.is_admin == (1 if role == PLATFORM_ADMIN_ROLE else 0)
            and new_user.is_active == 1
            and verify_password(temporary_credential, new_user.password_hash)
        )
        return {
            "status": "success" if matches else "failed",
            "userId": new_user.id if new_user is not None else None,
            "username": username,
        }

    result, request_id = _run_admin_action(
        user=user,
        action_type="admin.user.create",
        permission=ACTION_MANAGE_BRAND_USERS,
        payload=payload,
        target=username,
        executor=execute,
        readback=readback,
    )
    if result.get("status") == "success":
        result["msg"] = "User created"
        result["temporaryPassword"] = _temporary_credential_for_request(
            request_id,
            "create-user",
        )
    return result

@router.put("/api/admin/users/{user_id}")
def update_user(user_id: int, payload: Dict = Body(...), user: User = Depends(get_current_user)):
    if not _is_platform_admin(user):
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_USERS, "Only authorized brand admins can access this")
        if not _is_brand_admin(user):
            raise HTTPException(status_code=403, detail="Only authorized brand admins can access this")
    expected_user_fact: Dict[str, Any] = {}

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        db_user = _managed_user_or_fail(db, user, user_id)
        expected_user_fact.clear()
        expected_user_fact.update(_authoritative_user_fact(db_user))
        if "pagePermissions" in payload:
            expected_user_fact["page_permissions"] = list(payload["pagePermissions"])
            db_user.page_permissions = json.dumps(expected_user_fact["page_permissions"])
        if "brandPermissions" in payload:
            allowed_brands = None if _is_platform_admin(user) else _user_brand_permissions(user)
            expected_user_fact["brand_permissions"] = _normalize_brand_permissions(
                payload["brandPermissions"],
                allowed_brands,
            )
            db_user.brand_permissions = json.dumps(expected_user_fact["brand_permissions"])
        if "isActive" in payload:
            expected_user_fact["is_active"] = 1 if bool(payload["isActive"]) else 0
            db_user.is_active = expected_user_fact["is_active"]
        if _is_platform_admin(user):
            next_role = db_user.role
            if "role" in payload:
                next_role = str(payload["role"] or BRAND_MEMBER_ROLE).strip() or BRAND_MEMBER_ROLE
                expected_user_fact["role"] = next_role
                expected_user_fact["is_admin"] = 1 if next_role == PLATFORM_ADMIN_ROLE else 0
                db_user.role = next_role
                db_user.is_admin = expected_user_fact["is_admin"]
            if "actionPermissions" in payload:
                expected_user_fact["action_permissions"] = _normalize_action_permissions(
                    payload["actionPermissions"],
                    next_role,
                )
                db_user.action_permissions = json.dumps(expected_user_fact["action_permissions"], ensure_ascii=False)
            elif "role" in payload:
                expected_user_fact["action_permissions"] = _normalize_action_permissions(
                    _json_list(db_user.action_permissions),
                    next_role,
                )
                db_user.action_permissions = json.dumps(expected_user_fact["action_permissions"], ensure_ascii=False)
        else:
            expected_user_fact.update({
                "role": BRAND_MEMBER_ROLE,
                "is_admin": 0,
                "action_permissions": [],
            })
            db_user.role = BRAND_MEMBER_ROLE
            db_user.is_admin = 0
            db_user.action_permissions = json.dumps([], ensure_ascii=False)
        expected_user_fact["updated_at"] = _next_user_authorization_version(
            expected_user_fact.get("updated_at")
        )
        db_user.updated_at = expected_user_fact["updated_at"]
        db.flush()
        return {"status": "success", "msg": "User updated", "userId": user_id}

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        db.expire_all()
        db_user = db.get(User, user_id)
        if db_user is None:
            return {"status": "failed", "userId": user_id}
        matches = _authoritative_user_fact(db_user) == expected_user_fact
        return {
            "status": "success" if matches else "failed",
            "userId": user_id,
        }

    result, _request_id = _run_admin_action(
        user=user,
        action_type="admin.user.update",
        permission=ACTION_MANAGE_BRAND_USERS,
        payload=payload,
        target=str(user_id),
        executor=execute,
        readback=readback,
    )
    result["msg"] = "User updated"
    return result

@router.post("/api/admin/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: Dict[str, Any] = Body(default={}),
    user: User = Depends(get_current_user),
):
    if not _is_platform_admin(user):
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_USERS, "Only authorized brand admins can access this")
        if not _is_brand_admin(user):
            raise HTTPException(status_code=403, detail="Only authorized brand admins can access this")

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        db_user = _managed_user_or_fail(db, user, user_id)
        temporary_credential = _temporary_credential_for_request(
            context.external_request_id,
            "reset-password",
        )
        db_user.password_hash = get_password_hash(temporary_credential)
        db.flush()
        return {
            "status": "success",
            "msg": "Password reset",
            "temporaryPassword": temporary_credential,
            "userId": user_id,
        }

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        db_user = db.get(User, user_id)
        return {
            "status": "success"
            if db_user is not None
            and verify_password(
                _temporary_credential_for_request(context.external_request_id, "reset-password"),
                db_user.password_hash,
            )
            else "failed",
            "userId": user_id,
        }

    result, request_id = _run_admin_action(
        user=user,
        action_type="admin.user.reset_password",
        permission=ACTION_MANAGE_BRAND_USERS,
        payload=payload,
        target=str(user_id),
        executor=execute,
        readback=readback,
    )
    if result.get("status") == "success":
        result["msg"] = "Password reset"
        result["temporaryPassword"] = _temporary_credential_for_request(
            request_id,
            "reset-password",
        )
    return result


@router.post("/api/admin/users/{user_id}/revoke-sessions")
def revoke_user_sessions(
    user_id: int,
    payload: Dict[str, Any] = Body(default={}),
    user: User = Depends(get_current_user),
):
    if not _is_platform_admin(user):
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_USERS, "Only authorized brand admins can access this")
        if not _is_brand_admin(user):
            raise HTTPException(status_code=403, detail="Only authorized brand admins can access this")

    next_version_holder: Dict[str, datetime.datetime] = {}

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        _managed_user_or_fail(db, user, user_id)
        next_version_holder["value"] = _stage_revoke_user_sessions(db, user_id)
        return {
            "status": "success",
            "msg": "All sessions revoked",
            "userId": user_id,
        }

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        target = db.get(User, user_id)
        expected = next_version_holder.get("value")
        return {
            "status": "success"
            if target is not None
            and expected is not None
            and target.updated_at == expected
            else "failed",
            "userId": user_id,
        }

    result, _request_id = _run_admin_action(
        user=user,
        action_type="admin.user.revoke_sessions",
        permission=ACTION_MANAGE_BRAND_USERS,
        payload=payload,
        target=str(user_id),
        executor=execute,
        readback=readback,
    )
    result["msg"] = "All sessions revoked"
    return result

@router.delete("/api/admin/users/{user_id}")
def delete_user(
    user_id: int,
    payload: Dict[str, Any] = Body(default={}),
    user: User = Depends(get_current_user),
):
    if not _is_platform_admin(user):
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_USERS, "Only authorized brand admins can access this")
        if not _is_brand_admin(user):
            raise HTTPException(status_code=403, detail="Only authorized brand admins can access this")

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        db_user = _managed_user_or_fail(db, user, user_id)
        if db_user.username == "admin":
            raise _GovernedActionFailure("cannot_delete_admin", 400)
        db.delete(db_user)
        db.flush()
        return {"status": "success", "msg": "User deleted", "userId": user_id}

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        return {
            "status": "success" if context.transaction.get(User, user_id) is None else "failed",
            "userId": user_id,
        }

    result, _request_id = _run_admin_action(
        user=user,
        action_type="admin.user.delete",
        permission=ACTION_MANAGE_BRAND_USERS,
        payload=payload,
        target=str(user_id),
        executor=execute,
        readback=readback,
    )
    result["msg"] = "User deleted"
    return result


def _serialize_assistant_brand_space(space: AssistantBrandSpace) -> Dict[str, Any]:
    return {
        "id": space.id,
        "brand": space.brand,
        "displayName": space.display_name,
        "defaultAgentBindingId": space.default_agent_binding_id,
        "enabled": bool(space.enabled),
    }


def _serialize_assistant_agent_binding(binding: AssistantAgentBinding) -> Dict[str, Any]:
    runtime_model_name = PiAgentAssistantService().runtime_info().get("modelName")
    return {
        "id": binding.id,
        "brandSpaceId": binding.brand_space_id,
        "brand": binding.brand_space.brand if binding.brand_space else None,
        "displayName": binding.display_name,
        "modelDisplayName": binding.model_display_name,
        "modelName": binding.model_name or runtime_model_name,
        "availableModels": _json_list(binding.available_models_json),
        "supportsModelSwitch": bool(binding.supports_model_switch),
        "bindingType": binding.binding_type,
        "enabled": bool(binding.enabled),
    }


def _serialize_assistant_user_binding(binding: AssistantUserBinding) -> Dict[str, Any]:
    return {
        "id": binding.id,
        "userId": binding.user_id,
        "brandSpaceId": binding.brand_space_id,
        "agentBindingId": binding.agent_binding_id,
    }


@router.get("/api/admin/assistant/access-config")
def get_assistant_access_config(user: User = Depends(get_current_user)):
    _ensure_action_permission(user, ACTION_MANAGE_ASSISTANT_ACCESS, "Only authorized users can access assistant config")
    allowed_brands = None if _is_platform_admin(user) else set(_user_brand_permissions(user))
    db = SessionLocal()
    try:
        brand_spaces = db.query(AssistantBrandSpace).order_by(AssistantBrandSpace.brand.asc()).all()
        if allowed_brands is not None:
            brand_spaces = [item for item in brand_spaces if item.brand in allowed_brands]
        brand_space_ids = {item.id for item in brand_spaces if item.id is not None}
        agent_bindings = db.query(AssistantAgentBinding).order_by(AssistantAgentBinding.id.asc()).all()
        agent_bindings = [item for item in agent_bindings if item.brand_space_id in brand_space_ids]
        agent_binding_ids = {item.id for item in agent_bindings if item.id is not None}
        user_bindings = db.query(AssistantUserBinding).order_by(AssistantUserBinding.user_id.asc(), AssistantUserBinding.id.asc()).all()
        user_bindings = [
            item for item in user_bindings
            if item.brand_space_id in brand_space_ids and (item.agent_binding_id is None or item.agent_binding_id in agent_binding_ids)
        ]
        users = db.query(User).order_by(User.username.asc()).all()
        if allowed_brands is not None:
            users = [
                item for item in users
                if _user_role(item) == BRAND_MEMBER_ROLE and set(_user_brand_permissions(item)).issubset(allowed_brands)
            ]
        return {
            "brandSpaces": [_serialize_assistant_brand_space(space) for space in brand_spaces],
            "agentBindings": [_serialize_assistant_agent_binding(binding) for binding in agent_bindings],
            "userBindings": [_serialize_assistant_user_binding(binding) for binding in user_bindings],
            "users": [{
                "id": item.id,
                "username": item.username,
                "isAdmin": bool(item.is_admin),
                "role": _user_role(item),
            } for item in users],
        }
    finally:
        db.close()


@router.post("/api/admin/assistant/access-config")
async def update_assistant_access_config(
    payload: Dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
):
    _ensure_action_permission(user, ACTION_MANAGE_ASSISTANT_ACCESS, "Only authorized users can update assistant config")
    allowed_brands = None if _is_platform_admin(user) else set(_user_brand_permissions(user))
    replacement_keys = ("brandSpaces", "agentBindings", "userBindings")
    if any(key not in payload or not isinstance(payload[key], list) for key in replacement_keys):
        raise HTTPException(
            status_code=422,
            detail="brandSpaces, agentBindings, and userBindings must all be lists",
        )
    if any(
        not isinstance(item, dict)
        for key in replacement_keys
        for item in payload[key]
    ):
        raise HTTPException(
            status_code=422,
            detail="Assistant config collection items must be objects",
        )
    brand_spaces_payload = payload["brandSpaces"]
    agent_bindings_payload = payload["agentBindings"]
    user_bindings_payload = payload["userBindings"]
    expected_brand_spaces: List[Dict[str, Any]] = []
    expected_agent_bindings: List[Dict[str, Any]] = []
    expected_user_bindings: List[tuple[int, int, int | None]] = []
    managed_brand_space_ids: set[int] = set()
    brand_space_id_map: Dict[int, int] = {}
    agent_binding_id_map: Dict[int, int] = {}
    seen_brand_space_source_ids: set[int] = set()
    seen_agent_binding_source_ids: set[int] = set()

    def reference_id(value: Any) -> int:
        if isinstance(value, bool):
            raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
        if isinstance(value, int):
            normalized = value
        elif isinstance(value, str):
            digits = value[1:] if value.startswith("-") else value
            if not digits or any(char < "0" or char > "9" for char in digits):
                raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
            try:
                normalized = int(value)
            except ValueError as exc:
                raise _GovernedActionFailure("assistant_config_reference_invalid", 400) from exc
        else:
            raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
        if normalized == 0:
            raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
        return normalized

    def reject_brand_space_reference(db: Any, raw_id: int) -> None:
        referenced = db.get(AssistantBrandSpace, raw_id) if raw_id > 0 else None
        if (
            referenced is not None
            and allowed_brands is not None
            and referenced.brand not in allowed_brands
        ):
            raise _GovernedActionFailure("assistant_config_scope_denied", 403)
        raise _GovernedActionFailure("assistant_config_reference_invalid", 400)

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        expected_brand_spaces.clear()
        expected_agent_bindings.clear()
        expected_user_bindings.clear()
        managed_brand_space_ids.clear()
        brand_space_id_map.clear()
        agent_binding_id_map.clear()
        seen_brand_space_source_ids.clear()
        seen_agent_binding_source_ids.clear()
        scoped_brand_query = db.query(AssistantBrandSpace)
        if allowed_brands is not None:
            scoped_brand_query = scoped_brand_query.filter(
                AssistantBrandSpace.brand.in_(allowed_brands)
            )
        managed_brand_space_ids.update(
            item.id for item in scoped_brand_query.all() if item.id is not None
        )
        replacing_with_empty_scope = not (
            brand_spaces_payload or agent_bindings_payload or user_bindings_payload
        )
        if (
            replacing_with_empty_scope
            and managed_brand_space_ids
            and payload.get("confirmClear") is not True
        ):
            raise _GovernedActionFailure(
                "assistant_config_clear_confirmation_required",
                409,
            )
        brand_records: List[tuple[Dict[str, Any], AssistantBrandSpace, int | None, int | None]] = []
        for item in brand_spaces_payload:
            brand_key = str(item.get("brand") or "").strip()
            if allowed_brands is not None and brand_key not in allowed_brands:
                raise _GovernedActionFailure("assistant_config_scope_denied", 403)
            source_id = reference_id(item["id"]) if item.get("id") is not None else None
            if source_id is not None and source_id > 0:
                record = db.get(AssistantBrandSpace, source_id)
            elif source_id is not None:
                record = AssistantBrandSpace()
            else:
                record = db.query(AssistantBrandSpace).filter(
                    AssistantBrandSpace.brand == brand_key
                ).first()
                if record is None:
                    record = AssistantBrandSpace()
            if record is None:
                raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
            if allowed_brands is not None and record.id and record.brand not in allowed_brands:
                raise _GovernedActionFailure("assistant_config_scope_denied", 403)
            if source_id is not None:
                if source_id in seen_brand_space_source_ids:
                    raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
                seen_brand_space_source_ids.add(source_id)
            expected_brand = brand_key or str(record.brand or "").strip()
            expected_display_name = str(
                item.get("displayName") or record.display_name or expected_brand or ""
            ).strip()
            expected_enabled = 1 if item.get("enabled", True) else 0
            raw_default_binding_id = (
                reference_id(item["defaultAgentBindingId"])
                if item.get("defaultAgentBindingId") is not None
                else None
            )
            preserve_default = "defaultAgentBindingId" not in item
            if preserve_default and record.default_agent_binding_id is not None:
                raw_default_binding_id = int(record.default_agent_binding_id)
            record.brand = expected_brand
            record.display_name = expected_display_name
            record.enabled = expected_enabled
            db.add(record)
            expected = {
                "id": None,
                "brand": expected_brand,
                "display_name": expected_display_name,
                "default_agent_binding_id": None,
                "enabled": expected_enabled,
            }
            expected_brand_spaces.append(expected)
            brand_records.append((expected, record, source_id, raw_default_binding_id))
        db.flush()
        for expected, record, source_id, _raw_default_binding_id in brand_records:
            expected["id"] = record.id
            if source_id is not None:
                brand_space_id_map[source_id] = int(record.id)
        expected_brand_space_ids = {
            int(item["id"])
            for item in expected_brand_spaces
            if item.get("id") is not None
        }
        managed_brand_space_ids.update(expected_brand_space_ids)

        agent_records: List[tuple[Dict[str, Any], AssistantAgentBinding, int | None]] = []
        for item in agent_bindings_payload:
            raw_brand_space_id = reference_id(item.get("brandSpaceId"))
            brand_space_id = brand_space_id_map.get(raw_brand_space_id)
            if brand_space_id is None:
                reject_brand_space_reference(db, raw_brand_space_id)
            source_id = reference_id(item["id"]) if item.get("id") is not None else None
            if source_id is not None and source_id > 0:
                record = db.get(AssistantAgentBinding, source_id)
            else:
                record = AssistantAgentBinding()
            if record is None:
                raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
            if allowed_brands is not None and record.id and record.brand_space_id not in managed_brand_space_ids:
                raise _GovernedActionFailure("assistant_config_scope_denied", 403)
            if source_id is not None:
                if source_id in seen_agent_binding_source_ids:
                    raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
                seen_agent_binding_source_ids.add(source_id)
            expected = {
                "id": None,
                "brand_space_id": brand_space_id,
                "display_name": str(item.get("displayName") or ""),
                "model_display_name": str(item.get("modelDisplayName") or "PI Agent"),
                "model_name": str(item.get("modelName") or ""),
                "available_models": item.get("availableModels") or [],
                "supports_model_switch": 1 if item.get("supportsModelSwitch") else 0,
                "binding_type": str(item.get("bindingType") or "agent"),
                "enabled": 1 if item.get("enabled", True) else 0,
            }
            record.brand_space_id = expected["brand_space_id"]
            record.display_name = expected["display_name"]
            record.model_display_name = expected["model_display_name"]
            record.model_name = expected["model_name"]
            record.available_models_json = json.dumps(expected["available_models"], ensure_ascii=False)
            record.supports_model_switch = expected["supports_model_switch"]
            record.binding_type = expected["binding_type"]
            record.enabled = expected["enabled"]
            db.add(record)
            expected_agent_bindings.append(expected)
            agent_records.append((expected, record, source_id))
        db.flush()
        for expected, record, source_id in agent_records:
            expected["id"] = record.id
            if source_id is not None:
                agent_binding_id_map[source_id] = int(record.id)
        expected_agent_binding_ids = {
            int(item["id"])
            for item in expected_agent_bindings
            if item.get("id") is not None
        }

        expected_agents_by_id = {
            int(item["id"]): item
            for item in expected_agent_bindings
            if item.get("id") is not None
        }
        for expected, record, _source_id, raw_default_binding_id in brand_records:
            resolved_default_binding_id = None
            if raw_default_binding_id is not None:
                resolved_default_binding_id = agent_binding_id_map.get(raw_default_binding_id)
                if resolved_default_binding_id is None:
                    referenced = (
                        db.get(AssistantAgentBinding, raw_default_binding_id)
                        if raw_default_binding_id > 0
                        else None
                    )
                    if referenced is not None:
                        reject_brand_space_reference(db, int(referenced.brand_space_id or 0))
                    raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
                expected_agent = expected_agents_by_id[resolved_default_binding_id]
                if expected_agent["brand_space_id"] != record.id:
                    raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
            record.default_agent_binding_id = resolved_default_binding_id
            expected["default_agent_binding_id"] = resolved_default_binding_id
        db.flush()

        binding_query = db.query(AssistantUserBinding)
        if managed_brand_space_ids:
            binding_query = binding_query.filter(
                AssistantUserBinding.brand_space_id.in_(managed_brand_space_ids)
            )
        elif allowed_brands is not None:
            binding_query = binding_query.filter(False)
        binding_query.delete(synchronize_session=False)

        agent_delete_query = db.query(AssistantAgentBinding)
        if managed_brand_space_ids:
            agent_delete_query = agent_delete_query.filter(
                AssistantAgentBinding.brand_space_id.in_(managed_brand_space_ids)
            )
        elif allowed_brands is not None:
            agent_delete_query = agent_delete_query.filter(False)
        if expected_agent_binding_ids:
            agent_delete_query = agent_delete_query.filter(
                ~AssistantAgentBinding.id.in_(expected_agent_binding_ids)
            )
        try:
            agent_delete_query.delete(synchronize_session=False)
        except IntegrityError as exc:
            raise _GovernedActionFailure(
                "assistant_config_historical_reference",
                409,
            ) from exc

        brand_delete_query = db.query(AssistantBrandSpace)
        if allowed_brands is not None:
            brand_delete_query = brand_delete_query.filter(
                AssistantBrandSpace.brand.in_(allowed_brands)
            )
        if expected_brand_space_ids:
            brand_delete_query = brand_delete_query.filter(
                ~AssistantBrandSpace.id.in_(expected_brand_space_ids)
            )
        brand_delete_query.delete(synchronize_session=False)

        for item in user_bindings_payload:
            raw_brand_space_id = reference_id(item.get("brandSpaceId"))
            brand_space_id = brand_space_id_map.get(raw_brand_space_id)
            if brand_space_id is None:
                reject_brand_space_reference(db, raw_brand_space_id)
            user_id = reference_id(item.get("userId"))
            target_user = db.get(User, user_id)
            if target_user is None:
                raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
            target_brand_space = db.get(AssistantBrandSpace, brand_space_id)
            if target_brand_space is None:
                raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
            target_brand = str(target_brand_space.brand or "").strip()
            if (
                (not _is_platform_admin(user) and not _can_manage_user(user, target_user))
                or target_brand not in set(_user_brand_permissions(target_user))
            ):
                raise _GovernedActionFailure("assistant_config_scope_denied", 403)
            agent_binding_id = None
            if item.get("agentBindingId") is not None:
                raw_agent_binding_id = reference_id(item["agentBindingId"])
                agent_binding_id = agent_binding_id_map.get(raw_agent_binding_id)
                if agent_binding_id is None:
                    referenced = (
                        db.get(AssistantAgentBinding, raw_agent_binding_id)
                        if raw_agent_binding_id > 0
                        else None
                    )
                    if referenced is not None:
                        reject_brand_space_reference(db, int(referenced.brand_space_id or 0))
                    raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
                if expected_agents_by_id[agent_binding_id]["brand_space_id"] != brand_space_id:
                    raise _GovernedActionFailure("assistant_config_reference_invalid", 400)
            db.add(AssistantUserBinding(
                user_id=user_id,
                brand_space_id=brand_space_id,
                agent_binding_id=agent_binding_id,
            ))
            expected_user_bindings.append((user_id, brand_space_id, agent_binding_id))
        db.flush()
        return {
            "status": "success",
            "msg": "Assistant access config updated",
            "brand_spaces": len(expected_brand_spaces),
            "agent_bindings": len(expected_agent_bindings),
            "user_bindings": len(expected_user_bindings),
        }

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        db = context.transaction
        db.expire_all()
        brand_query = db.query(AssistantBrandSpace)
        if allowed_brands is not None:
            brand_query = brand_query.filter(
                AssistantBrandSpace.brand.in_(allowed_brands)
            )
        actual_brand_spaces = sorted(({
                "id": record.id,
                "brand": record.brand,
                "display_name": record.display_name,
                "default_agent_binding_id": record.default_agent_binding_id,
                "enabled": record.enabled,
            } for record in brand_query.all()), key=lambda item: item["id"])
        if actual_brand_spaces != sorted(expected_brand_spaces, key=lambda item: item["id"]):
            return {"status": "failed", "reason": "brand_space_readback_mismatch"}

        agent_query = db.query(AssistantAgentBinding)
        if managed_brand_space_ids:
            agent_query = agent_query.filter(
                AssistantAgentBinding.brand_space_id.in_(managed_brand_space_ids)
            )
        elif allowed_brands is not None:
            agent_query = agent_query.filter(False)
        actual_agent_bindings = sorted(({
                "id": record.id,
                "brand_space_id": record.brand_space_id,
                "display_name": record.display_name,
                "model_display_name": record.model_display_name,
                "model_name": record.model_name,
                "available_models": _json_list(record.available_models_json),
                "supports_model_switch": record.supports_model_switch,
                "binding_type": record.binding_type,
                "enabled": record.enabled,
            } for record in agent_query.all()), key=lambda item: item["id"])
        if actual_agent_bindings != sorted(expected_agent_bindings, key=lambda item: item["id"]):
            return {"status": "failed", "reason": "agent_binding_readback_mismatch"}

        binding_query = db.query(AssistantUserBinding)
        if managed_brand_space_ids:
            binding_query = binding_query.filter(
                AssistantUserBinding.brand_space_id.in_(managed_brand_space_ids)
            )
        elif allowed_brands is not None:
            binding_query = binding_query.filter(False)
        actual_user_bindings = sorted(
            (item.user_id, item.brand_space_id, item.agent_binding_id)
            for item in binding_query.all()
        )
        if actual_user_bindings != sorted(expected_user_bindings):
            return {"status": "failed", "reason": "user_binding_readback_mismatch"}
        return {
            "status": "success",
            "brand_spaces": len(expected_brand_spaces),
            "agent_bindings": len(expected_agent_bindings),
            "user_bindings": len(expected_user_bindings),
        }

    _result, _request_id = _run_admin_action(
        user=user,
        action_type="admin.assistant.access_config",
        permission=ACTION_MANAGE_ASSISTANT_ACCESS,
        payload=payload,
        target="assistant-access-config",
        executor=execute,
        readback=readback,
    )
    return get_assistant_access_config(user)

# --- 配置管理接口 ---

def _node_model_config_nodes(snapshot: Any) -> List[Dict[str, Any]]:
    return [
        {
            "nodeKey": node.node_key,
            "channel": node.channel,
            "primaryModel": node.primary_model,
            "fallbackModel": node.fallback_model,
            "primarySource": node.primary_source,
            "fallbackSource": node.fallback_source,
            "requiredCapabilities": sorted(
                NODE_REGISTRY[node.node_key].required_capabilities
            ),
        }
        for node in snapshot.nodes.values()
    ]


def _node_model_config_record_nodes(record: NodeModelConfigVersion) -> List[Dict[str, Any]]:
    return [
        {
            "nodeKey": entry.node_key,
            "channel": entry.channel,
            "primaryModel": entry.primary_model,
            "fallbackModel": entry.fallback_model,
            "requiredCapabilities": sorted(
                NODE_REGISTRY[entry.node_key].required_capabilities
            ),
        }
        for entry in record.entries
    ]


def _active_node_model_config_record(db: Any) -> NodeModelConfigVersion | None:
    return (
        db.query(NodeModelConfigVersion)
        .filter_by(status="published", active_slot="active")
        .one_or_none()
    )


def _raise_node_model_config_failure(exc: Exception) -> None:
    if isinstance(exc, NodeModelConfigConflict):
        raise _GovernedActionFailure("node_model_config_conflict", 409) from exc
    if isinstance(exc, NodeModelConfigNotFound):
        raise _GovernedActionFailure("node_model_config_not_found", 404) from exc
    if isinstance(exc, NodeModelConfigValidationError):
        raise _GovernedActionFailure("node_model_config_invalid", 400) from exc
    raise exc


def _require_platform_model_config_admin(user: User) -> None:
    if not _is_platform_admin(user):
        raise HTTPException(status_code=403, detail="Only platform admins can manage node model config")
    _ensure_action_permission(
        user,
        ACTION_MANAGE_PLATFORM_CONFIG,
        "No permission to manage node model config",
    )


@router.get("/api/admin/node-model-config")
def get_node_model_config(user: User = Depends(get_current_user)):
    _require_platform_model_config_admin(user)
    db = SessionLocal()
    try:
        try:
            bundle = get_active_node_model_config_bundle(db)
        except NodeModelConfigNotFound:
            bundle = None
        except NodeModelConfigValidationError as exc:
            raise HTTPException(status_code=409, detail="Active node model config contains a retired runtime channel") from exc
        snapshot = bundle.snapshot if bundle is not None else capture_snapshot()
        return {
            "activeVersion": bundle.version if bundle is not None else None,
            "nodes": _node_model_config_nodes(snapshot),
        }
    finally:
        db.close()


@router.post("/api/admin/node-model-config/drafts")
def create_node_model_config_draft_api(
    payload: Dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
):
    _require_platform_model_config_admin(user)
    expected: Dict[str, Any] = {}

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        try:
            record = create_node_model_config_draft(
                context.transaction,
                payload.get("entries"),
                change_note=payload.get("changeNote"),
                commit=False,
            )
        except Exception as exc:
            _raise_node_model_config_failure(exc)
        expected.update(
            {
                "version": record.version,
                "state": record.status,
                "nodes": _node_model_config_record_nodes(record),
            }
        )
        return {"status": "success", **expected}

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        context.transaction.expire_all()
        record = (
            context.transaction.query(NodeModelConfigVersion)
            .filter_by(version=expected.get("version"))
            .one_or_none()
        )
        actual = None if record is None else {
            "version": record.version,
            "state": record.status,
            "nodes": _node_model_config_record_nodes(record),
        }
        return {
            "status": "success" if actual == expected else "failed",
            "version": expected.get("version"),
        }

    result, _request_id = _run_admin_action(
        user=user,
        action_type="admin.node_model_config.create_draft",
        permission=ACTION_MANAGE_PLATFORM_CONFIG,
        payload=payload,
        target="node-model-config",
        executor=execute,
        readback=readback,
    )
    return result


@router.post("/api/admin/node-model-config/versions/{version}/publish")
def publish_node_model_config_api(
    version: int,
    payload: Dict[str, Any] = Body(default={}),
    user: User = Depends(get_current_user),
):
    _require_platform_model_config_admin(user)
    expected_active: Dict[str, Any] = {}

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        try:
            record = publish_node_model_config_draft(
                context.transaction,
                version,
                expected_version=payload.get("expectedVersion"),
                commit=False,
            )
        except Exception as exc:
            _raise_node_model_config_failure(exc)
        expected_active.update(
            {
                "activeVersion": record.version,
                "nodes": _node_model_config_record_nodes(record),
            }
        )
        return {
            "status": "success",
            **expected_active,
        }

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        context.transaction.expire_all()
        record = _active_node_model_config_record(context.transaction)
        actual = {
            "activeVersion": record.version if record is not None else None,
            "nodes": _node_model_config_record_nodes(record) if record is not None else [],
        }
        return {
            "status": "success" if actual == expected_active else "failed",
            **actual,
        }

    result, _request_id = _run_admin_action(
        user=user,
        action_type="admin.node_model_config.publish",
        permission=ACTION_MANAGE_PLATFORM_CONFIG,
        payload={**payload, "version": version},
        target=str(version),
        executor=execute,
        readback=readback,
    )
    return result


@router.post("/api/admin/node-model-config/rollback")
def rollback_node_model_config_api(
    payload: Dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
):
    _require_platform_model_config_admin(user)
    target_version = payload.get("targetVersion")
    expected_active: Dict[str, Any] = {}
    rollback_from: Dict[str, int] = {}

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        previous = _active_node_model_config_record(context.transaction)
        if previous is not None:
            rollback_from["version"] = previous.version
        try:
            record = rollback_node_model_config_active(
                context.transaction,
                target_version=target_version,
                expected_version=payload.get("expectedVersion"),
                commit=False,
            )
        except Exception as exc:
            _raise_node_model_config_failure(exc)
        expected_active.update(
            {
                "activeVersion": record.version,
                "nodes": _node_model_config_record_nodes(record),
            }
        )
        return {
            "status": "success",
            **expected_active,
            "rolledBackFromVersion": rollback_from.get("version"),
            "rolledBackToVersion": target_version,
        }

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        context.transaction.expire_all()
        record = _active_node_model_config_record(context.transaction)
        actual = {
            "activeVersion": record.version if record is not None else None,
            "nodes": _node_model_config_record_nodes(record) if record is not None else [],
        }
        return {
            "status": "success" if actual == expected_active else "failed",
            **actual,
        }

    result, _request_id = _run_admin_action(
        user=user,
        action_type="admin.node_model_config.rollback",
        permission=ACTION_MANAGE_PLATFORM_CONFIG,
        payload=payload,
        target=str(target_version),
        executor=execute,
        readback=readback,
    )
    return result

@router.get("/api/admin/config")
def get_dashboard_config(user: User = Depends(get_current_user)):
    reload_config()
    if _is_platform_admin(user):
        allowed_brands = list(BRAND_CONFIGS.keys())
        include_safety = True
    else:
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG, "Only authorized users can access config")
        allowed_brands = _user_brand_permissions(user)
        include_safety = False
    filtered_config: Dict[str, Any] = {"brand_configs": {}}
    if include_safety:
        filtered_config["safety_config"] = {
            key: GLOBAL_SAFETY_CONFIG.get(key)
            for key in SAFETY_CONFIG_EDITABLE_KEYS
            if key in GLOBAL_SAFETY_CONFIG
        }

    for brand in allowed_brands:
        if brand not in BRAND_CONFIGS:
            continue
        business_config = get_brand_business_config(brand)
        filtered_config["brand_configs"][brand] = {
            key: business_config.get(key)
            for key in BRAND_CONFIG_EDITABLE_KEYS
        }
        filtered_config["brand_configs"][brand].update({
            "query_governance_kg_reference_updated_by": business_config.get("query_governance_kg_reference_updated_by", ""),
            "query_governance_kg_reference_updated_at": business_config.get("query_governance_kg_reference_updated_at", ""),
        })

    return filtered_config


@router.get("/api/admin/page-config")
def get_page_config(user: User = Depends(get_current_user)):
    """Read effective presentation configuration for all authenticated users."""
    reload_config()
    return page_config_payload(_raw_config)


@router.post("/api/admin/page-config")
def update_page_config(
    payload: Dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
):
    if not _is_platform_admin(user):
        raise HTTPException(status_code=403, detail="Only platform admins can update page config")
    try:
        values = validate_page_config_values(payload.get("values"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    expected_version = payload.get("expectedVersion")
    if expected_version is not None and (
        isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0
    ):
        raise HTTPException(status_code=400, detail="expectedVersion must be a non-negative integer")

    candidate_holder: Dict[str, Any] = {}

    def execute(_context: ExecutionContext) -> Dict[str, Any]:
        reload_config()
        current_version = page_config_version(_raw_config)
        if expected_version is not None and expected_version != current_version:
            raise _GovernedActionFailure("page_config_conflict", 409)
        candidate = apply_page_config_update(_raw_config, values, version=current_version + 1)
        candidate_holder["value"] = candidate
        try:
            save_config(candidate)
        except Exception as exc:
            raise _GovernedExternalWriteUnknown("page config persistence outcome unknown") from exc
        return {"status": "success", **page_config_payload(candidate)}

    def readback(_context: ExecutionContext) -> Dict[str, Any]:
        candidate = candidate_holder.get("value")
        if not isinstance(candidate, dict):
            return {"status": "failed", "reason": "page_config_candidate_missing"}
        actual = page_config_payload(_raw_config)
        expected = page_config_payload(candidate)
        return {"status": "success" if actual == expected else "failed", **actual}

    result, _request_id = _run_admin_action(
        user=user,
        action_type="admin.page_config.update",
        permission=ACTION_MANAGE_PLATFORM_CONFIG,
        payload=payload,
        target="page-config",
        executor=execute,
        readback=readback,
        atomic_local_transaction=False,
    )
    return result

@router.post("/api/admin/config")
def update_dashboard_config(payload: Dict = Body(...), user: User = Depends(get_current_user)):
    can_manage_platform = _is_platform_admin(user)
    if not can_manage_platform:
        _ensure_action_permission(user, ACTION_MANAGE_BRAND_CONFIG, "Only authorized users can update config")
    allowed_brands = list(BRAND_CONFIGS.keys()) if can_manage_platform else _user_brand_permissions(user)
    requested_brand_configs = payload.get("brand_configs") or {}
    if not can_manage_platform and isinstance(requested_brand_configs, dict):
        unauthorized_brands = sorted(set(requested_brand_configs) - set(allowed_brands))
        if unauthorized_brands:
            raise HTTPException(status_code=403, detail="No permission for requested brand config")

    for brand, cfg in requested_brand_configs.items():
        if brand not in allowed_brands or brand not in BRAND_CONFIGS or not isinstance(cfg, dict):
            continue
        if "query_governance_kg_reference" in cfg:
            reference = cfg["query_governance_kg_reference"]
            if not isinstance(reference, str) or len(reference) > 20000:
                raise HTTPException(status_code=400, detail="query_governance_kg_reference must be a string no longer than 20000 characters")
        if "top_query_token_limit" in cfg:
            value = cfg["top_query_token_limit"]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise HTTPException(status_code=400, detail="top_query_token_limit must be a positive integer")
        if "query_governance_reorder_enabled" in cfg and not isinstance(
            cfg["query_governance_reorder_enabled"], bool
        ):
            raise HTTPException(
                status_code=400,
                detail="query_governance_reorder_enabled must be boolean",
            )
        if "query_governance_relevance_enabled" in cfg and not isinstance(
            cfg["query_governance_relevance_enabled"], bool
        ):
            raise HTTPException(
                status_code=400,
                detail="query_governance_relevance_enabled must be boolean",
            )
        if "query_governance_recall_rate_enabled" in cfg and not isinstance(
            cfg["query_governance_recall_rate_enabled"], bool
        ):
            raise HTTPException(
                status_code=400,
                detail="query_governance_recall_rate_enabled must be boolean",
            )
        if "query_governance_relevance_buckets" in cfg:
            buckets = cfg["query_governance_relevance_buckets"]
            if (
                not isinstance(buckets, list)
                or any(not isinstance(bucket, str) or bucket not in KNOWN_RELEVANCE_BUCKETS for bucket in buckets)
            ):
                raise HTTPException(
                    status_code=400,
                    detail="query_governance_relevance_buckets must contain known bucket IDs",
                )
        if "query_governance_query_min_metric_type" in cfg and cfg["query_governance_query_min_metric_type"] not in {"uv", "pv"}:
            raise HTTPException(status_code=400, detail="query_governance_query_min_metric_type must be uv or pv")
        if "query_governance_excluded_keywords" in cfg:
            excluded_keywords = cfg["query_governance_excluded_keywords"]
            if (
                not isinstance(excluded_keywords, list)
                or any(not isinstance(keyword, str) for keyword in excluded_keywords)
            ):
                raise HTTPException(
                    status_code=400,
                    detail="query_governance_excluded_keywords must be a list of strings",
                )
        query_selection_integer_limits = {
            "query_governance_query_concurrency": (1, 64),
            "query_governance_extract_limit": (1, 5000),
            "query_governance_candidate_high_pv_limit": (0, 5000),
            "query_governance_candidate_history_days": (1, 365),
            "query_governance_preference_top_purchase_product_limit": (1, 5000),
            "query_governance_preference_top_click_product_limit": (1, 5000),
            "query_governance_preference_generation_batch_query_limit": (1, 500),
            "query_governance_preference_generation_concurrency": (1, 32),
            "query_governance_preference_generation_timeout_seconds": (1, 3600),
            "query_governance_kg_agent_timeout_seconds": (1, 3600),
            "query_governance_vector_reorder_concurrency": (1, 32),
            "query_governance_vector_reorder_retry_attempts": (1, 5),
            "query_governance_query_min_metric_value": (0, None),
            "query_governance_recall_rate_product_batch_size": (1, 1000),
            "top_query_inventory_max_cycles": (1, 100),
        }
        query_selection_number_limits = {
            "query_governance_vector_reorder_retry_backoff_seconds": (0, 5),
            "query_governance_candidate_alert_ctr_gap_pp": (0, 100),
            "query_governance_candidate_alert_cvr_gap_pp": (0, 100),
            "query_governance_candidate_new_query_pv_threshold": (0, None),
            "query_governance_candidate_pv_growth_ratio_floor": (0, None),
            "query_governance_candidate_pv_growth_sqrt_multiplier": (0, None),
            "query_governance_candidate_pv_growth_abs_floor": (0, None),
            "query_governance_recall_rate_score_threshold": (0, 100),
            "query_governance_recall_rate_jev_threshold": (0, 1),
        }
        preference_generation_integer_limits = {
        }
        for key, (minimum, maximum) in preference_generation_integer_limits.items():
            if key not in cfg:
                continue
            value = cfg[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
                raise HTTPException(status_code=400, detail=f"{key} must be a positive integer")
        relevance_runtime_integer_limits = {
            "query_governance_relevance_multimodal_concurrency",
            "query_governance_relevance_batch_size",
            "query_governance_relevance_model_timeout_seconds",
            "query_governance_relevance_image_download_timeout_seconds",
            "query_governance_relevance_max_rounds",
        }
        relevance_runtime_number_limits = {
            "query_governance_relevance_blacklist_ratio_threshold": (0, 1),
        }
        for key in relevance_runtime_integer_limits:
            if key not in cfg:
                continue
            value = cfg[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise HTTPException(status_code=400, detail=f"{key} must be a positive integer")
        for key, (minimum, maximum) in relevance_runtime_number_limits.items():
            if key not in cfg:
                continue
            value = cfg[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < minimum or value > maximum:
                raise HTTPException(status_code=400, detail=f"{key} must be a number in range")
        for key, (minimum, maximum) in query_selection_integer_limits.items():
            if key not in cfg:
                continue
            value = cfg[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
                raise HTTPException(status_code=400, detail=f"{key} must be an integer in range")
        for key, (minimum, maximum) in query_selection_number_limits.items():
            if key not in cfg:
                continue
            value = cfg[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < minimum or (maximum is not None and value > maximum):
                raise HTTPException(status_code=400, detail=f"{key} must be a finite number in range")
        if "top_query_world_knowledge_config" in cfg:
            world_knowledge_config = cfg["top_query_world_knowledge_config"]
            if not isinstance(world_knowledge_config, dict):
                raise HTTPException(status_code=400, detail="top_query_world_knowledge_config must be an object")
            retired_keys = {"weather_mcp", "festival_mcp"}.intersection(world_knowledge_config)
            if retired_keys:
                names = ", ".join(sorted(retired_keys))
                raise HTTPException(
                    status_code=400,
                    detail=f"MCP world knowledge configuration is retired: {names}",
                )

    candidate_holder: Dict[str, Any] = {}

    def execute(_context: ExecutionContext) -> Dict[str, Any]:
        candidate = copy.deepcopy(_raw_config)
        if can_manage_platform and "safety_config" in payload:
            if "safety_config" not in candidate:
                candidate["safety_config"] = {}
            candidate["safety_config"].update({
                key: value
                for key, value in (payload["safety_config"] or {}).items()
                if key in SAFETY_CONFIG_EDITABLE_KEYS
            })

        if "brand_configs" in payload:
            if "brand_configs" not in candidate:
                candidate["brand_configs"] = {}
            for brand, cfg in payload["brand_configs"].items():
                if brand in allowed_brands and brand in candidate["brand_configs"]:
                    for key, value in cfg.items():
                        if key not in BRAND_CONFIG_EDITABLE_KEYS:
                            continue
                        if key == "top_query_world_knowledge_config" and isinstance(value, dict):
                            existing = candidate["brand_configs"][brand].get(key)
                            if not isinstance(existing, dict):
                                existing = {}
                                candidate["brand_configs"][brand][key] = existing
                            for world_key, world_value in value.items():
                                existing[world_key] = world_value
                        else:
                            candidate["brand_configs"][brand][key] = value
                    if "query_governance_kg_reference" in cfg:
                        current_reference = str(
                            _raw_config.get("brand_configs", {}).get(brand, {}).get(
                                "query_governance_kg_reference", ""
                            )
                            or ""
                        )
                        next_reference = str(cfg.get("query_governance_kg_reference") or "")
                        if next_reference != current_reference:
                            candidate["brand_configs"][brand]["query_governance_kg_reference_updated_by"] = str(user.username)
                            candidate["brand_configs"][brand]["query_governance_kg_reference_updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            record_check_mode_changes(candidate, _raw_config, changed_on=governance_today())
        candidate_holder["value"] = candidate
        try:
            save_config(candidate)
        except Exception as exc:
            raise _GovernedExternalWriteUnknown("config persistence outcome unknown") from exc
        return {"status": "success", "msg": "Config updated and reloaded"}

    def readback(_context: ExecutionContext) -> Dict[str, Any]:
        candidate = candidate_holder.get("value")
        if not isinstance(candidate, dict):
            return {"status": "failed", "reason": "config_candidate_missing"}
        reload_config()
        actual = _raw_config
        if actual.get("query_governance_check_history") != candidate.get("query_governance_check_history"):
            return {"status": "failed", "reason": "check_history_readback_mismatch"}
        if can_manage_platform and "safety_config" in payload:
            if actual.get("safety_config") != candidate.get("safety_config"):
                return {"status": "failed", "reason": "safety_config_readback_mismatch"}
        for brand, cfg in requested_brand_configs.items():
            if brand not in allowed_brands or brand not in candidate.get("brand_configs", {}):
                continue
            current = actual.get("brand_configs", {}).get(brand, {})
            expected = candidate.get("brand_configs", {}).get(brand, {})
            for key in cfg:
                if key in BRAND_CONFIG_EDITABLE_KEYS and current.get(key) != expected.get(key):
                    return {"status": "failed", "reason": "brand_config_readback_mismatch"}
            if "query_governance_kg_reference" in cfg:
                for key in ("query_governance_kg_reference_updated_by", "query_governance_kg_reference_updated_at"):
                    if current.get(key) != expected.get(key):
                        return {"status": "failed", "reason": "kg_reference_metadata_readback_mismatch"}
        return {"status": "success", "brands": sorted(requested_brand_configs)}

    _result, _request_id = _run_admin_action(
        user=user,
        action_type="admin.config.update",
        permission=ACTION_MANAGE_BRAND_CONFIG,
        payload=payload,
        target="dashboard-config",
        executor=execute,
        readback=readback,
        atomic_local_transaction=False,
    )
    return {"msg": "Config updated and reloaded"}

__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
