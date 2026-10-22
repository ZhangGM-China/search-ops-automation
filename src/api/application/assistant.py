"""Assistant application handlers and session support."""

from __future__ import annotations

import os
import hmac
from fastapi import Body, HTTPException, Depends, Header, status, File, UploadFile, Query
from fastapi.responses import StreamingResponse
from typing import List, Optional, Dict, Any
import datetime
import json
import uuid
from sqlalchemy import func
from src.persistence import SessionLocal, User, ActionLog, DailyAbnormalQuery, InventoryRecoveryTask, NoResultGovernanceAction, NoResultQuery, TopQueryActionPlan, TopQueryTask, AssistantBrandSpace, AssistantAgentBinding, AssistantSessionMeta, AssistantActionRequest, ACTION_EXECUTE_GOVERNANCE_ACTIONS, ACTION_CANCEL_GOVERNANCE_ACTIONS, save_action_log, mark_inventory_task_recovered_by_details
from src.persistence.inventory_recovery_dual_write import sync_inventory_recovery_task
from src.persistence.inventory_recovery_dual_write import read_inventory_recovery_tasks
from src.governance.action_gateway import ActionExecutionFailedError, ActionGateway, ActionInProgressError, ActionIntent, ExecutionContext
from src.runtime.release_info import get_release_info
from src.llm.node_config import ResolvedNodeConfig, get_snapshot_node_config
from src.llm.config_store import NodeModelConfigNotFound, get_active_bundle as get_active_node_model_config_bundle
from src.no_result.governance import NoResultGovernanceService
from src.assistant.service import PiAgentAssistantService
from src.assistant.tools import AssistantToolService
from src.assistant.workspace import WorkspaceAccessError, configured_workspace_roots, list_workspace, preview_workspace_file, save_workspace_file
from src.assistant.terminal import TerminalManager, TerminalSecurityError
from src.top_query.service import TopQueryGovernanceService
from src.governance.query_policy import ExperimentValidationError, assert_query_governance_allowed
from fastapi import APIRouter
import logging
from ..dependencies import *  # noqa: F401,F403
from ..shared import *  # noqa: F401,F403
from .query_governance_support import *  # noqa: F401,F403

router = APIRouter()
logger = logging.getLogger(__name__)
_terminal_manager = TerminalManager()

def _assistant_title_from_message(message: str) -> str:
    normalized = " ".join(str(message or "").split()).strip().rstrip("。！？!?")
    if not normalized:
        return "新对话"
    return normalized[:32] + ("..." if len(normalized) > 32 else "")


def _assistant_session_preview(messages: List[Dict[str, Any]]) -> str:
    for item in reversed(messages):
        content = _repair_mojibake_text(str(item.get("content") or "")).strip()
        if item.get("role") == "operator" and content:
            return content[:120]
    for item in reversed(messages):
        content = _repair_mojibake_text(str(item.get("content") or "")).strip()
        if content:
            return content[:120]
    return ""


def _repair_mojibake_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    suspicious_markers = ("å", "æ", "ä", "ç", "é", "ï", "ð", "ã", "â", "€", "™", "œ", "�")
    if not any(marker in text for marker in suspicious_markers):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if any(ord(char) > 127 for char in repaired):
        return repaired
    return text


def _normalize_assistant_versions(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_versions = message.get("versions")
    versions = raw_versions if isinstance(raw_versions, list) else []
    normalized_versions: List[Dict[str, Any]] = []
    for index, item in enumerate(versions):
        if not isinstance(item, dict):
            continue
        normalized_versions.append({
            "id": str(item.get("id") or f"{message.get('id', 'message')}-version-{index + 1}"),
            "title": _repair_mojibake_text(item.get("title") or message.get("title") or ""),
            "content": _repair_mojibake_text(item.get("content") or ""),
            "createdAt": str(item.get("createdAt") or message.get("createdAt") or ""),
            "toolCalls": item.get("toolCalls") if isinstance(item.get("toolCalls"), list) else [],
            "toolResults": item.get("toolResults") if isinstance(item.get("toolResults"), list) else [],
        })
    return normalized_versions


def _normalize_assistant_message(message: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(message)
    normalized["title"] = _repair_mojibake_text(normalized.get("title") or "")
    normalized["content"] = _repair_mojibake_text(normalized.get("content") or "")
    normalized["toolCalls"] = normalized.get("toolCalls") if isinstance(normalized.get("toolCalls"), list) else []
    normalized["toolResults"] = normalized.get("toolResults") if isinstance(normalized.get("toolResults"), list) else []
    versions = _normalize_assistant_versions(normalized)
    if versions:
        normalized["versions"] = versions
        raw_index = normalized.get("activeVersionIndex")
        try:
            active_index = int(raw_index)
        except (TypeError, ValueError):
            active_index = len(versions) - 1
        normalized["activeVersionIndex"] = min(max(active_index, 0), len(versions) - 1)
    return normalized


def _load_assistant_messages(raw_log: Any) -> List[Dict[str, Any]]:
    if isinstance(raw_log, list):
        return [_normalize_assistant_message(item) for item in raw_log if isinstance(item, dict)]
    if not raw_log:
        return []
    try:
        parsed = json.loads(raw_log)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [_normalize_assistant_message(item) for item in parsed if isinstance(item, dict)]


def _load_assistant_actions(raw_log: Any) -> List[Dict[str, Any]]:
    if isinstance(raw_log, list):
        return raw_log
    if not raw_log:
        return []
    try:
        parsed = json.loads(raw_log)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _list_accessible_assistant_bindings(db, user: User) -> List[AssistantAgentBinding]:
    query = db.query(AssistantAgentBinding).join(
        AssistantBrandSpace,
        AssistantBrandSpace.id == AssistantAgentBinding.brand_space_id,
    ).filter(
        AssistantAgentBinding.enabled == 1,
        AssistantBrandSpace.enabled == 1,
    )
    if _is_platform_admin(user):
        return query.all()

    legacy_brands = _user_brand_permissions(user)
    if not legacy_brands:
        return []
    return query.filter(AssistantBrandSpace.brand.in_(legacy_brands)).all()


def _resolve_assistant_scope(db, user: User) -> Dict[str, Any]:
    bindings = _list_accessible_assistant_bindings(db, user)
    if not _is_platform_admin(user):
        legacy_brands = _user_brand_permissions(user)
        brand_map: Dict[str, List[AssistantAgentBinding]] = {}
        for binding in bindings:
            brand_name = str(binding.brand_space.brand if binding.brand_space else "")
            if brand_name:
                brand_map.setdefault(brand_name, []).append(binding)
        return {
            "brands": legacy_brands,
            "bindings": bindings,
            "brandMap": brand_map,
            "usesBindings": bool(bindings),
        }

    if bindings:
        brand_map: Dict[str, List[AssistantAgentBinding]] = {}
        for binding in bindings:
            brand_name = str(binding.brand_space.brand if binding.brand_space else "")
            if not brand_name:
                continue
            brand_map.setdefault(brand_name, []).append(binding)
        return {"brands": sorted(set(_user_brand_permissions(user)) | set(brand_map)), "bindings": bindings, "brandMap": brand_map, "usesBindings": True}

    legacy_brands = _user_brand_permissions(user)
    return {"brands": legacy_brands, "bindings": [], "brandMap": {}, "usesBindings": False}


def _resolve_brand_space(db, brand: str) -> Optional[AssistantBrandSpace]:
    return db.query(AssistantBrandSpace).filter(
        AssistantBrandSpace.brand == brand,
        AssistantBrandSpace.enabled == 1,
    ).first()


def _resolve_assistant_binding_for_request(
    db,
    user: User,
    scope: Dict[str, Any],
    brand: Optional[str],
    agent_binding_id: Optional[int],
) -> tuple[Optional[str], Optional[AssistantAgentBinding]]:
    selected_brand = str(brand or "").strip() or None
    binding = None

    if scope["usesBindings"]:
        if agent_binding_id is not None:
            binding = next((item for item in scope["bindings"] if item.id == agent_binding_id), None)
            if binding is None:
                raise HTTPException(status_code=403, detail="Assistant agent binding is not accessible")
            selected_brand = binding.brand_space.brand if binding.brand_space else selected_brand
        elif selected_brand:
            brand_bindings = scope["brandMap"].get(selected_brand) or []
            if brand_bindings:
                binding = brand_bindings[0]
        elif len(scope["brands"]) == 1:
            selected_brand = scope["brands"][0]
            brand_bindings = scope["brandMap"].get(selected_brand) or []
            if brand_bindings:
                binding = brand_bindings[0]
        else:
            # The assistant UI is account-scoped. When the client does not
            # provide a brand, keep the complete authorization scope and let
            # the agent choose the relevant brand through governed tools.
            selected_brand = None
            binding = scope["bindings"][0] if len(scope["bindings"]) == 1 else None

        if selected_brand and selected_brand not in scope["brands"]:
            raise HTTPException(status_code=403, detail="Assistant brand is not accessible")
        return selected_brand, binding

    if selected_brand:
        if selected_brand not in scope["brands"] and not _is_platform_admin(user):
            raise HTTPException(status_code=403, detail="Assistant brand is not accessible")
        return selected_brand, None
    return (scope["brands"][0] if len(scope["brands"]) == 1 else None), None


def _active_assistant_model_config(db) -> Optional[ResolvedNodeConfig]:
    try:
        bundle = get_active_node_model_config_bundle(db)
    except NodeModelConfigNotFound:
        return None
    return get_snapshot_node_config(
        bundle.snapshot,
        "assistant.chat",
        required_channel="pi_agent",
    )


def _build_assistant_service(
    binding: Optional[AssistantAgentBinding],
    model_config: Optional[ResolvedNodeConfig] = None,
) -> PiAgentAssistantService:
    if model_config is not None:
        available_models = [model_config.primary_model]
        if model_config.fallback_model:
            available_models.append(model_config.fallback_model)
        return PiAgentAssistantService(
            model_label=str(binding.model_display_name or "PI Agent") if binding else None,
            model_name=model_config.primary_model,
            available_models=available_models,
            supports_model_switch=bool(model_config.fallback_model),
            model_config=model_config,
        )
    if not binding:
        return PiAgentAssistantService()
    available_models = _json_list(binding.available_models_json)
    return PiAgentAssistantService(
        model_label=str(binding.model_display_name or "PI Agent"),
        model_name=str(binding.model_name or os.getenv("PI_AGENT_MODEL") or "") or None,
        available_models=[str(item) for item in available_models],
        supports_model_switch=bool(binding.supports_model_switch),
    )


def _get_assistant_session_by_session_id(db, *, session_id: str, user_id: int) -> Optional[AssistantSessionMeta]:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        return None
    return db.query(AssistantSessionMeta).filter(
        AssistantSessionMeta.session_id == normalized_session_id,
        AssistantSessionMeta.user_id == user_id,
    ).first()


def _ensure_assistant_session_brand_locked(
    existing_session: Optional[AssistantSessionMeta],
    requested_brand: Optional[str],
) -> Optional[str]:
    normalized_requested_brand = str(requested_brand or "").strip() or None
    if not existing_session:
        return normalized_requested_brand
    existing_brand = str(existing_session.brand or "").strip() or None
    if existing_brand and normalized_requested_brand and existing_brand != normalized_requested_brand:
        raise HTTPException(
            status_code=409,
            detail=f"assistant session brand mismatch: session brand is '{existing_brand}'",
        )
    return existing_brand or normalized_requested_brand


def _ensure_assistant_session_access(
    db,
    *,
    session_id: str,
    user: User,
    expected_brand: Optional[str] = None,
    detail_prefix: str = "Assistant session",
) -> AssistantSessionMeta:
    record = _get_assistant_session_by_session_id(db, session_id=session_id, user_id=int(user.id or 0))
    if not record:
        raise HTTPException(status_code=404, detail=f"{detail_prefix} not found")
    if record.agent_binding_id:
        binding = db.get(AssistantAgentBinding, int(record.agent_binding_id))
        brand_space = db.get(AssistantBrandSpace, int(binding.brand_space_id or 0)) if binding else None
        binding_accessible = bool(
            binding
            and bool(binding.enabled)
            and brand_space
            and bool(brand_space.enabled)
            and str(brand_space.brand or "").strip() == str(record.brand or "").strip()
        )
        if not binding_accessible:
            _log_assistant_authorization_denial(
                "assistant_binding_revoked",
                user_id=int(user.id or 0),
                session_id=record.session_id,
            )
            raise HTTPException(status_code=403, detail=f"{detail_prefix} is not accessible")
    if not _assistant_brand_allowed(user, record.brand):
        raise HTTPException(status_code=403, detail=f"{detail_prefix} is not accessible")
    locked_brand = _ensure_assistant_session_brand_locked(record, expected_brand)
    if locked_brand and locked_brand != (str(record.brand or "").strip() or None):
        raise HTTPException(status_code=409, detail=f"{detail_prefix} brand mismatch")
    return record


def _upsert_assistant_session_meta(
    db,
    *,
    user: User,
    session_id: str,
    brand: Optional[str],
    agent_binding: Optional[AssistantAgentBinding],
    operator_message: str,
    assistant_message: Optional[str] = None,
    proposed_actions: Optional[List[Dict[str, Any]]] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Dict[str, Any]]] = None,
    retry_of_message_id: Optional[str] = None,
) -> AssistantSessionMeta:
    record = db.query(AssistantSessionMeta).filter(
        AssistantSessionMeta.user_id == user.id,
        AssistantSessionMeta.session_id == session_id,
    ).first()
    if not record:
        record = AssistantSessionMeta(
            session_id=session_id,
            user_id=user.id,
            brand=brand or "",
            agent_binding_id=agent_binding.id if agent_binding else None,
            generated_title=_assistant_title_from_message(operator_message),
            last_opened_at=datetime.datetime.now(),
        )
        db.add(record)
        db.flush()

    messages = _load_assistant_messages(record.message_log)
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if retry_of_message_id and assistant_message is not None:
        retry_target = next(
            (item for item in messages if str(item.get("id") or "") == str(retry_of_message_id) and item.get("role") == "assistant"),
            None,
        )
        if retry_target is not None:
            existing_versions = _normalize_assistant_versions(retry_target)
            if not existing_versions:
                existing_versions = [{
                    "id": f"{retry_target.get('id', retry_of_message_id)}-version-1",
                    "title": _repair_mojibake_text(retry_target.get("title") or ""),
                    "content": _repair_mojibake_text(retry_target.get("content") or ""),
                    "createdAt": str(retry_target.get("createdAt") or current_time),
                    "toolCalls": retry_target.get("toolCalls") if isinstance(retry_target.get("toolCalls"), list) else [],
                    "toolResults": retry_target.get("toolResults") if isinstance(retry_target.get("toolResults"), list) else [],
                }]
            next_version = {
                "id": f"{retry_target.get('id', retry_of_message_id)}-version-{len(existing_versions) + 1}",
                "title": _repair_mojibake_text(retry_target.get("title") or ""),
                "content": assistant_message,
                "createdAt": current_time,
                "toolCalls": tool_calls or [],
                "toolResults": tool_results or [],
            }
            existing_versions.append(next_version)
            retry_target.update({
                "title": next_version["title"],
                "content": assistant_message,
                "createdAt": current_time,
                "toolCalls": tool_calls or [],
                "toolResults": tool_results or [],
                "versions": existing_versions,
                "activeVersionIndex": len(existing_versions) - 1,
            })
        else:
            retry_of_message_id = None
    if not retry_of_message_id:
        messages.append({
            "id": f"{session_id}-operator-{len(messages) + 1}",
            "role": "operator",
            "content": operator_message,
            "createdAt": current_time,
        })
        if assistant_message is not None:
            messages.append({
                "id": f"{session_id}-assistant-{len(messages) + 1}",
                "role": "assistant",
                "content": assistant_message,
                "createdAt": current_time,
                "toolCalls": tool_calls or [],
                "toolResults": tool_results or [],
            })
    record.brand = brand or record.brand or ""
    record.agent_binding_id = agent_binding.id if agent_binding else record.agent_binding_id
    record.generated_title = record.generated_title or _assistant_title_from_message(operator_message)
    record.last_message_preview = _assistant_session_preview(messages)
    record.message_log = json.dumps(messages, ensure_ascii=False)
    record.proposed_actions_snapshot = json.dumps(proposed_actions or [], ensure_ascii=False)
    record.last_opened_at = datetime.datetime.now()
    db.commit()
    db.refresh(record)
    return record


def _serialize_assistant_session(record: AssistantSessionMeta) -> Dict[str, Any]:
    messages = _load_assistant_messages(record.message_log)
    actions = _load_assistant_actions(record.proposed_actions_snapshot)
    db = SessionLocal()
    try:
        action_rows = db.query(AssistantActionRequest).filter(
            AssistantActionRequest.session_id == record.session_id,
            AssistantActionRequest.user_id == record.user_id,
        ).order_by(AssistantActionRequest.updated_at.desc(), AssistantActionRequest.id.desc()).all()
    finally:
        db.close()
    if action_rows:
        actions = [_serialize_assistant_action_request(item) for item in action_rows]
    binding = record.agent_binding
    available_models = _json_list(binding.available_models_json) if binding else []
    runtime_model_name = PiAgentAssistantService().runtime_info().get("modelName")
    runtime = {
        "provider": "PI Agent",
        "modelLabel": (binding.model_display_name if binding else None) or "PI Agent",
        "modelName": (binding.model_name if binding else None) or runtime_model_name,
        "availableModels": available_models,
        "supportsModelSwitch": bool(binding.supports_model_switch) if binding else len(available_models) > 1,
        "agentId": None,
        "teamId": None,
        "channel": "pi_agent",
    }
    return {
        "id": record.id,
        "sessionId": record.session_id,
        "brand": record.brand,
        "agentBindingId": record.agent_binding_id,
        "title": _repair_mojibake_text(record.manual_title or record.generated_title or "新对话"),
        "manualTitle": record.manual_title,
        "generatedTitle": record.generated_title,
        "preview": _repair_mojibake_text(record.last_message_preview) or _assistant_session_preview(messages),
        "pinned": bool(record.pinned),
        "updatedAt": record.updated_at.isoformat() if record.updated_at else None,
        "lastOpenedAt": record.last_opened_at.isoformat() if record.last_opened_at else None,
        "messages": messages,
        "proposedActions": actions,
        "runtime": runtime,
        "parentSessionId": record.parent_session_id,
    }


def _serialize_assistant_action_request(record: AssistantActionRequest) -> Dict[str, Any]:
    payload = _json_object(record.payload_json)
    payload.pop("_authorization", None)
    return {
        "id": record.id,
        "actionType": record.action_type,
        "brand": record.brand,
        "query": record.query_text,
        "target": record.target,
        "reason": record.reason,
        "riskLevel": record.risk_level,
        "status": record.status,
        "payload": payload,
        "resultSummary": record.result_summary,
    }

@router.get("/api/dashboard/assistant-context")
def get_assistant_context(
    date: Optional[str] = None,
    brand: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        scope = _resolve_assistant_scope(db, user)
        user_brands = scope["brands"] or _user_brand_permissions(user)
        if brand:
            if brand not in user_brands:
                raise HTTPException(status_code=403, detail="无权限访问该品牌智能助理上下文")
            user_brands = [brand]
        assistant_model_config = _active_assistant_model_config(db)
        assistant_runtime = _build_assistant_service(None, assistant_model_config).runtime_info()
        available_bindings = [{
            "agentBindingId": binding.id,
            "brand": binding.brand_space.brand if binding.brand_space else None,
            "displayName": binding.display_name,
            "modelDisplayName": binding.model_display_name,
            "modelName": (
                assistant_model_config.primary_model
                if assistant_model_config is not None
                else binding.model_name or assistant_runtime.get("modelName")
            ),
            "availableModels": (
                [
                    model_name
                    for model_name in (
                        assistant_model_config.primary_model,
                        assistant_model_config.fallback_model,
                    )
                    if model_name
                ]
                if assistant_model_config is not None
                else _json_list(binding.available_models_json)
            ),
            "supportsModelSwitch": (
                bool(assistant_model_config.fallback_model)
                if assistant_model_config is not None
                else bool(binding.supports_model_switch)
            ),
            "bindingType": binding.binding_type,
        } for binding in scope["bindings"]]
    finally:
        db.close()
    target_date = _parse_dashboard_date(date)
    db = SessionLocal()
    try:
        query_rows = db.query(DailyAbnormalQuery).filter(
            func.date(DailyAbnormalQuery.data_date) == target_date,
            DailyAbnormalQuery.brand.in_(user_brands),
        ).order_by(DailyAbnormalQuery.updated_at.desc(), DailyAbnormalQuery.created_at.desc()).all()
        action_rows = db.query(ActionLog).filter(
            func.date(ActionLog.created_at) == target_date,
            ActionLog.brand.in_(user_brands),
        ).order_by(ActionLog.created_at.desc()).all()
        inventory_rows = [
            row
            for brand_name in user_brands
            for row in read_inventory_recovery_tasks(db, brand=brand_name)
            if row.updated_at and row.updated_at.date() == target_date
        ]
        inventory_rows.sort(key=lambda row: row.updated_at, reverse=True)
        existing_query_keys = {(row.brand, row.query_text) for row in query_rows}
        inventory_groups: Dict[tuple[str, str], List[InventoryRecoveryTask]] = {}
        for row in inventory_rows:
            if not row.original_query:
                continue
            key = (row.brand, row.original_query)
            if key in existing_query_keys:
                continue
            inventory_groups.setdefault(key, []).append(row)
        pending_count = len([row for row in query_rows if _query_status(row) in {"pending", "reviewing"}])
        messages = []
        for row in query_rows:
            reason = row.reason_analysis or row.remark or ""
            suggestion = row.action_suggestions or ""
            if not reason and not suggestion:
                continue
            created_at = row.updated_at or row.created_at
            messages.append({
                "id": f"query-analysis-{row.id}",
                "role": "assistant",
                "title": row.query_text,
                "content": "，".join([item for item in [reason, suggestion] if item]),
                "createdAt": created_at.strftime("%Y-%m-%d %H:%M") if created_at else "",
            })
        for (task_brand, task_query), group_rows in inventory_groups.items():
            latest = group_rows[0]
            messages.append({
                "id": f"inventory-context-{task_brand}-{task_query}",
                "role": "assistant",
                "title": task_query,
                "content": f"断码任务 {len(group_rows)} 个商品，最近覆盖率 {latest.last_check_stock or 0}%，状态 {latest.status}。",
                "createdAt": latest.updated_at.strftime("%Y-%m-%d %H:%M") if latest.updated_at else "",
            })
        if not messages:
            messages.append({
                "id": "assistant-context-empty",
                "role": "assistant",
                "title": "今日任务上下文",
                "content": f"已载入 {target_date.strftime('%Y-%m-%d')} 的任务、治理动作和断码监测数据。",
                "createdAt": "",
            })
        first_ai_query = next(
            (row.query_text for row in query_rows if row.reason_analysis or row.action_suggestions),
            None,
        )
        first_inventory_query = next(iter(inventory_groups.keys()))[1] if inventory_groups else None
        first_query = first_ai_query or (query_rows[0].query_text if query_rows else first_inventory_query or "今日任务")
        suggested_prompts = [
            f"分析「{first_query}」的最近 AI 原因和治理建议",
            "汇总今日待处理 Query 的共同原因",
            "检查今日拉黑和降桶动作是否可能误伤",
            "整理断码监测中的恢复关注点",
        ]
        return {
            "summary": {
                "taskCount": len(query_rows) + len(inventory_groups),
                "pendingCount": pending_count,
                "actionCount": len(action_rows),
                "monitoringCount": len(inventory_rows),
            },
            "suggestedPrompts": suggested_prompts,
            "messages": messages[:8],
            "capabilities": [
                {
                    "key": "read_task_context",
                    "title": "读取真实任务上下文",
                    "description": "读取本地异常 Query、治理动作、监测任务和已落库 AI 分析。",
                    "status": "ready",
                },
                {
                    "key": "summarize_ai_analysis",
                    "title": "汇总 AI 分析结果",
                    "description": "基于 daily_abnormal_query 中的原因分析和动作建议生成运营上下文。",
                    "status": "ready",
                },
                {
                    "key": "execute_governance_action",
                    "title": "执行治理写操作",
                    "description": "拉黑、降桶、任务状态写入仍需接入真实确认和写接口。",
                    "status": "planned",
                },
            ],
            "runtime": assistant_runtime,
            "availableBindings": available_bindings,
            "availableBrands": user_brands,
            "availableTools": AssistantToolService().available_tools(),
        }
    finally:
        db.close()


@router.get("/api/assistant/runtime-inspector")
def get_assistant_runtime_inspector(user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        scope = _resolve_assistant_scope(db, user)
        runtime = _build_assistant_service(None, _active_assistant_model_config(db)).runtime_info()
    finally:
        db.close()
    tools = AssistantToolService().available_tools()
    return {
        "runtime": runtime,
        "permissions": {
            "accountId": int(user.id or 0),
            "brands": scope["brands"] or _user_brand_permissions(user),
            "isAdmin": _is_platform_admin(user),
            "workspaceScoped": True,
        },
        "workspace": {"roots": [str(root) for root in configured_workspace_roots()]},
        "tools": tools,
        "events": [],
        "context": {"sessionId": None, "summary": None},
    }


@router.get("/api/assistant/sessions")
def list_assistant_sessions(user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        scope = _resolve_assistant_scope(db, user)
        query = db.query(AssistantSessionMeta).filter(AssistantSessionMeta.user_id == user.id)
        if not _is_platform_admin(user):
            query = query.filter(AssistantSessionMeta.brand.in_(scope["brands"]))
        records = query.order_by(
            AssistantSessionMeta.pinned.desc(),
            AssistantSessionMeta.last_opened_at.desc(),
            AssistantSessionMeta.updated_at.desc(),
        ).all()
        return {"items": [_serialize_assistant_session(record) for record in records]}
    finally:
        db.close()


@router.get("/api/assistant/workspace")
def get_assistant_workspace(
    path: str = Query(default="."),
    user: User = Depends(get_current_user),
):
    try:
        return list_workspace(path)
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/assistant/workspace/file")
def preview_assistant_workspace_file(
    path: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
):
    try:
        return preview_workspace_file(path)
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/assistant/workspace/file")
async def upload_assistant_workspace_file(
    path: str = Query(..., min_length=1),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    try:
        payload = await file.read()
        if len(payload) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="workspace upload exceeds 10 MB")
        return save_workspace_file(path, payload)
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/api/assistant/terminal/sessions")
def create_assistant_terminal_session(payload: Dict[str, Any] = Body(default={}), user: User = Depends(get_current_user)):
    try:
        return _terminal_manager.create_session(str(payload.get("cwd") or "."), owner_id=int(user.id or 0))
    except (TerminalSecurityError, FileNotFoundError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/api/assistant/terminal/sessions/{terminal_session_id}/execute")
def execute_assistant_terminal(terminal_session_id: str, payload: Dict[str, Any] = Body(...), user: User = Depends(get_current_user)):
    try:
        return _terminal_manager.execute(str(payload.get("command") or ""), terminal_session_id=terminal_session_id, owner_id=int(user.id or 0))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TerminalSecurityError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/assistant/terminal/sessions/{terminal_session_id}")
def get_assistant_terminal_session(terminal_session_id: str, user: User = Depends(get_current_user)):
    try:
        return _terminal_manager.get_session(terminal_session_id, owner_id=int(user.id or 0))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/assistant/terminal/sessions/{terminal_session_id}/stop")
def stop_assistant_terminal(terminal_session_id: str, user: User = Depends(get_current_user)):
    try:
        return _terminal_manager.stop_session(terminal_session_id, owner_id=int(user.id or 0))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/assistant/sessions/{session_meta_id}/branches")
def branch_assistant_session(session_meta_id: int, payload: Dict[str, Any] = Body(default={}), user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        source = db.get(AssistantSessionMeta, session_meta_id)
        if not source or int(source.user_id or 0) != int(user.id or 0):
            raise HTTPException(status_code=404, detail="Assistant session not found")
        if not _assistant_brand_allowed(user, source.brand):
            raise HTTPException(status_code=403, detail="Assistant session is not accessible")
        messages = _load_assistant_messages(source.message_log)
        message_id = str(payload.get("messageId") or "").strip()
        if message_id:
            index = next((i for i, item in enumerate(messages) if str(item.get("id")) == message_id), None)
            if index is None:
                raise HTTPException(status_code=404, detail="Assistant message not found")
            messages = messages[: index + 1]
        branch_id = f"{source.session_id}-branch-{uuid.uuid4().hex[:8]}"
        branch = AssistantSessionMeta(session_id=branch_id, parent_session_id=source.session_id, user_id=source.user_id, brand=source.brand, agent_binding_id=source.agent_binding_id, generated_title=f"{source.generated_title or '新对话'} · 分支", message_log=json.dumps(messages, ensure_ascii=False), proposed_actions_snapshot=source.proposed_actions_snapshot, last_message_preview=_assistant_session_preview(messages), last_opened_at=datetime.datetime.now())
        db.add(branch); db.commit(); db.refresh(branch)
        return _serialize_assistant_session(branch)
    finally:
        db.close()


@router.post("/api/internal/assistant/tools/{tool_name}")
def execute_internal_assistant_tool(
    tool_name: str,
    payload: Dict[str, Any] = Body(...),
    x_pi_internal_token: Optional[str] = Header(default=None, alias="X-PI-Internal-Token"),
):
    """Execute an account-scoped business tool for the embedded PI runtime.

    This endpoint is intentionally separate from browser APIs. The Node SDK
    runtime calls it with a shared local-service token; the account scope is
    resolved again from the database using ``userId`` and is never trusted from
    the browser or from the runtime payload.
    """
    expected_token = os.getenv("PI_AGENT_INTERNAL_TOKEN", "local-pi-runtime")
    if not x_pi_internal_token or not hmac.compare_digest(str(x_pi_internal_token), str(expected_token)):
        raise HTTPException(status_code=401, detail="PI runtime authentication failed")
    try:
        user_id = int(payload.get("userId"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="userId is required")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id, User.is_active == 1).first()
        if not user:
            raise HTTPException(status_code=404, detail="Assistant operator not found")
        scope = _resolve_assistant_scope(db, user)
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        result = AssistantToolService().execute_tool(
            tool_name,
            payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
            allowed_brands=list(scope["brands"]),
            context=context,
        )
        return result
    finally:
        db.close()


@router.patch("/api/assistant/sessions/{session_meta_id}")
def update_assistant_session(
    session_meta_id: int,
    payload: Dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        record = db.get(AssistantSessionMeta, session_meta_id)
        if not record or record.user_id != user.id:
            raise HTTPException(status_code=404, detail="Assistant session not found")
        scope = _resolve_assistant_scope(db, user)
        if not _is_platform_admin(user) and record.brand not in scope["brands"]:
            raise HTTPException(status_code=403, detail="Assistant session is not accessible")
        if "title" in payload:
            record.manual_title = str(payload.get("title") or "").strip() or None
        if "pinned" in payload:
            record.pinned = 1 if payload.get("pinned") else 0
        record.last_opened_at = datetime.datetime.now()
        db.commit()
        db.refresh(record)
        return _serialize_assistant_session(record)
    finally:
        db.close()


@router.delete("/api/assistant/sessions/{session_meta_id}")
def delete_assistant_session(
    session_meta_id: int,
    user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        record = db.get(AssistantSessionMeta, session_meta_id)
        if not record or record.user_id != user.id:
            raise HTTPException(status_code=404, detail="Assistant session not found")
        scope = _resolve_assistant_scope(db, user)
        if not _is_platform_admin(user) and record.brand not in scope["brands"]:
            raise HTTPException(status_code=403, detail="Assistant session is not accessible")
        db.delete(record)
        db.commit()
        return {"status": "deleted", "sessionId": record.session_id}
    finally:
        db.close()


def _assistant_brand_allowed(user: User, brand: Optional[str]) -> bool:
    if not brand:
        return False
    if user.is_admin:
        return True
    return brand in _user_brand_permissions(user)


_ASSISTANT_ACTION_AUTHORIZATION_KEY = "_authorization"
_ASSISTANT_ACTION_ALLOWED_TOP_QUERY_STATUSES = {
    "approved",
    "waiting_human_approval",
    "pending_confirmation",
}


def _log_assistant_authorization_denial(
    category: str,
    *,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    action_request_id: Optional[int] = None,
    action_id: Optional[int] = None,
    task_id: Optional[int] = None,
) -> None:
    logger.warning(
        "assistant authorization denied category=%s userId=%s sessionId=%s actionRequestId=%s actionId=%s taskId=%s",
        str(category or "unknown"),
        user_id,
        str(session_id or "") or None,
        action_request_id,
        action_id,
        task_id,
    )


def _assistant_action_rejection(
    category: str,
    *,
    status_code: int,
    detail: str,
    user: User,
    session_id: Optional[str] = None,
    action_request_id: Optional[int] = None,
    action_id: Optional[int] = None,
    task_id: Optional[int] = None,
) -> None:
    _log_assistant_authorization_denial(
        category,
        user_id=int(user.id or 0),
        session_id=session_id,
        action_request_id=action_request_id,
        action_id=action_id,
        task_id=task_id,
    )
    raise HTTPException(status_code=status_code, detail=detail)


def _assistant_prepare_ttl_seconds() -> int:
    try:
        return max(1, int(os.getenv("ASSISTANT_ACTION_PREPARE_TTL_SECONDS", "900")))
    except (TypeError, ValueError):
        return 900


def _assistant_action_live_identity(
    db,
    *,
    action_type: str,
    payload: Dict[str, Any],
    user: User,
    session_id: str,
    action_request_id: Optional[int] = None,
) -> Dict[str, Any]:
    if action_type in {"cancel_blacklist", "cancel_demotion"}:
        return {}

    def reject(
        category: str,
        status_code: int,
        detail: str,
        *,
        action_id: Optional[int] = None,
        task_id: Optional[int] = None,
    ) -> None:
        _assistant_action_rejection(
            category,
            status_code=status_code,
            detail=detail,
            user=user,
            session_id=session_id,
            action_request_id=action_request_id,
            action_id=action_id,
            task_id=task_id,
        )

    try:
        action_id = int(payload.get("actionId") or 0)
    except (TypeError, ValueError):
        action_id = 0
    if not action_id:
        reject("missing_action_id", 400, "缺少治理 actionId")

    if action_type == "execute_top_query_action":
        action = db.get(TopQueryActionPlan, action_id)
        task_model = TopQueryTask
        task_id = int(action.task_id or 0) if action else 0
        allowed_statuses = _ASSISTANT_ACTION_ALLOWED_TOP_QUERY_STATUSES
        category_prefix = "top_query"
    elif action_type == "execute_no_result_action":
        action = db.get(NoResultGovernanceAction, action_id)
        task_model = NoResultQuery
        task_id = int(action.query_id or 0) if action else 0
        allowed_statuses = {"pending_execute"}
        category_prefix = "no_result"
    else:
        reject("unsupported_action_type", 400, f"Unsupported assistant action: {action_type}", action_id=action_id)

    if not action:
        reject(f"{category_prefix}_action_not_found", 404, "Assistant action object not found", action_id=action_id)
    task = db.get(task_model, task_id) if task_id else None
    if not task:
        reject(
            f"{category_prefix}_task_not_found",
            409,
            "Assistant action target is unavailable",
            action_id=action_id,
            task_id=task_id or None,
        )
    task_brand = str(task.brand or "").strip()
    task_query = str(task.query_text or "").strip()
    action_brand = str(action.brand or task_brand).strip()
    action_query = str(action.query_text or task_query).strip()
    if (
        not task_brand
        or not task_query
        or action_brand != task_brand
        or action_query != task_query
    ):
        reject(
            f"{category_prefix}_object_identity_conflict",
            409,
            "Assistant action object identity changed",
            action_id=action_id,
            task_id=task_id,
        )
    if not _assistant_brand_allowed(user, task_brand):
        reject(
            f"{category_prefix}_object_brand_denied",
            403,
            "Assistant action object is not accessible",
            action_id=action_id,
            task_id=task_id,
        )
    if str(action.status or "").strip().lower() not in allowed_statuses:
        reject(
            f"{category_prefix}_action_state_changed",
            409,
            "Assistant action is no longer confirmable",
            action_id=action_id,
            task_id=task_id,
        )
    return {
        "actionId": action_id,
        "taskId": task_id,
        "brand": task_brand,
        "query": task_query,
    }


def _prepare_assistant_action_payload(
    db,
    *,
    action_payload: Dict[str, Any],
    user: User,
    session_id: str,
) -> Dict[str, Any]:
    action_type = str(action_payload.get("actionType") or "").strip()
    brand = str(action_payload.get("brand") or "").strip()
    query = str(action_payload.get("query") or "").strip()
    target = str(action_payload.get("target") or "").strip()
    stored_payload = dict(action_payload.get("payload") or {})
    live_identity = _assistant_action_live_identity(
        db,
        action_type=action_type,
        payload=stored_payload,
        user=user,
        session_id=session_id,
    )
    if live_identity:
        if live_identity["brand"] != brand:
            _assistant_action_rejection(
                "prepared_brand_mismatch",
                status_code=403,
                detail="Assistant action object is not accessible",
                user=user,
                session_id=session_id,
                action_id=live_identity["actionId"],
                task_id=live_identity["taskId"],
            )
        if live_identity["query"] != query:
            _assistant_action_rejection(
                "prepared_query_mismatch",
                status_code=409,
                detail="Assistant action query does not match its object",
                user=user,
                session_id=session_id,
                action_id=live_identity["actionId"],
                task_id=live_identity["taskId"],
            )
        requested_task_id = stored_payload.get("taskId", stored_payload.get("queryId"))
        if requested_task_id is not None:
            try:
                requested_task_id = int(requested_task_id)
            except (TypeError, ValueError):
                requested_task_id = 0
            if requested_task_id != live_identity["taskId"]:
                _assistant_action_rejection(
                    "prepared_task_mismatch",
                    status_code=409,
                    detail="Assistant action task does not match its object",
                    user=user,
                    session_id=session_id,
                    action_id=live_identity["actionId"],
                    task_id=live_identity["taskId"],
                )
        stored_payload["actionId"] = live_identity["actionId"]
        stored_payload["taskId"] = live_identity["taskId"]
    stored_payload[_ASSISTANT_ACTION_AUTHORIZATION_KEY] = {
        "version": 1,
        "sessionId": session_id,
        "actionType": action_type,
        "brand": brand,
        "query": query,
        "target": target,
        "actionId": live_identity.get("actionId"),
        "taskId": live_identity.get("taskId"),
    }
    return stored_payload


def _validate_assistant_action_execution(
    db,
    record: AssistantActionRequest,
    user: User,
) -> None:
    payload = _json_object(record.payload_json)
    action_type = str(record.action_type or "")
    supported_actions = {
        "cancel_blacklist",
        "cancel_demotion",
        "execute_top_query_action",
        "execute_no_result_action",
    }
    if action_type not in supported_actions:
        raise HTTPException(status_code=400, detail=f"Unsupported assistant action: {action_type}")
    if record.created_at and (
        datetime.datetime.now() - record.created_at
    ).total_seconds() > _assistant_prepare_ttl_seconds():
        _assistant_action_rejection(
            "prepared_action_expired",
            status_code=409,
            detail="Assistant action confirmation expired",
            user=user,
            session_id=record.session_id,
            action_request_id=int(record.id or 0),
        )
    authorization = payload.get(_ASSISTANT_ACTION_AUTHORIZATION_KEY)
    if not isinstance(authorization, dict):
        _assistant_action_rejection(
            "authorization_snapshot_missing",
            status_code=409,
            detail="Assistant action authorization snapshot is missing",
            user=user,
            session_id=record.session_id,
            action_request_id=int(record.id or 0),
        )
    expected_identity = {
        "sessionId": str(record.session_id or ""),
        "actionType": action_type,
        "brand": str(record.brand or ""),
        "query": str(record.query_text or ""),
        "target": str(record.target or ""),
        "actionId": payload.get("actionId"),
        "taskId": payload.get("taskId"),
    }
    if any(authorization.get(key) != value for key, value in expected_identity.items()):
        _assistant_action_rejection(
            "prepared_action_identity_changed",
            status_code=409,
            detail="Assistant action identity changed after preparation",
            user=user,
            session_id=record.session_id,
            action_request_id=int(record.id or 0),
            action_id=payload.get("actionId") if isinstance(payload.get("actionId"), int) else None,
            task_id=payload.get("taskId") if isinstance(payload.get("taskId"), int) else None,
        )
    if action_type in {"cancel_blacklist", "cancel_demotion"}:
        try:
            assert_query_governance_allowed(record.brand, record.query_text or "")
        except ExperimentValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return
    live_identity = _assistant_action_live_identity(
        db,
        action_type=action_type,
        payload=payload,
        user=user,
        session_id=str(record.session_id or ""),
        action_request_id=int(record.id or 0),
    )
    if any(
        live_identity.get(key) != expected_identity.get(key)
        for key in ("actionId", "taskId", "brand", "query")
    ):
        _assistant_action_rejection(
            "live_action_identity_changed",
            status_code=409,
            detail="Assistant action object changed after preparation",
            user=user,
            session_id=record.session_id,
            action_request_id=int(record.id or 0),
            action_id=live_identity.get("actionId"),
            task_id=live_identity.get("taskId"),
        )


def _assistant_execute_action(
    record: AssistantActionRequest,
    user: User,
    external_request_id: str,
) -> str:
    payload = _json_object(record.payload_json)
    action_type = record.action_type
    if action_type == "cancel_blacklist":
        try:
            assert_query_governance_allowed(record.brand, record.query_text or "")
        except ExperimentValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        success = service.remove_from_blacklist(
            record.brand,
            record.target or "",
            [record.query_text or ""],
            external_request_id=external_request_id,
        )
        if not success:
            raise HTTPException(status_code=500, detail="取消拉黑失败")
        save_action_log({
            "brand": record.brand,
            "query": record.query_text,
            "spu": record.target,
            "name": "手动取消拉黑",
            "type": 5,
            "reason": f"助理确认执行({user.username})",
        })
        return "已执行取消拉黑"
    if action_type == "cancel_demotion":
        try:
            assert_query_governance_allowed(record.brand, record.query_text or "")
        except ExperimentValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        success = service.sync_broken_code_to_redis(
            record.brand,
            remove_items={record.query_text or "": [record.target or ""]},
            external_request_id=external_request_id,
            allow_prod_recovery=True,
        )
        if not success:
            raise HTTPException(status_code=500, detail="取消降桶失败")
        save_action_log({
            "brand": record.brand,
            "query": record.query_text,
            "spu": record.target,
            "name": "手动取消降桶",
            "type": 3,
            "reason": f"助理确认执行({user.username})",
        })
        mark_inventory_task_recovered_by_details(record.brand, record.query_text or "", record.target or "", -1)
        return "已执行取消降桶"
    if action_type == "execute_top_query_action":
        action_id = int(payload.get("actionId") or 0)
        if not action_id:
            raise HTTPException(status_code=400, detail="缺少 top query actionId")
        TopQueryGovernanceService().execute_approved_action(
            action_id,
            dry_run=bool(payload.get("dryRun", False)),
            external_request_id=external_request_id,
        )
        return "已执行 Query 治理人工动作"
    if action_type == "execute_no_result_action":
        action_id = int(payload.get("actionId") or 0)
        if not action_id:
            raise HTTPException(status_code=400, detail="缺少 no result actionId")
        NoResultGovernanceService().execute_pending_action(
            action_id,
            external_request_id=external_request_id,
        )
        return "已执行 No Result 人工动作"
    raise HTTPException(status_code=400, detail=f"Unsupported assistant action: {action_type}")


def _confirm_cancel_demotion_via_gateway(
    record: AssistantActionRequest,
    user: User,
) -> tuple[str, Dict[str, Any]]:
    """Execute the legacy recovery only after a durable action-fact claim."""
    request_id = f"assistant-confirm-cancel-demotion:{int(record.id or 0)}"
    if not record.id:
        raise HTTPException(status_code=409, detail="Assistant action request identity is unavailable")

    intent = ActionIntent(
        action_type="assistant_cancel_demotion",
        idempotency_key=request_id,
        payload={
            "assistantActionRequestId": int(record.id),
            "sessionId": str(record.session_id or ""),
            "brand": str(record.brand or ""),
            "query": str(record.query_text or ""),
            "target": str(record.target or ""),
        },
        operator=str(user.username or user.id),
        brand=str(record.brand or ""),
        release=get_release_info()["releaseSha"],
        permission=ACTION_CANCEL_GOVERNANCE_ACTIONS,
        query_text=record.query_text,
        target=record.target,
    )

    def execute(context: ExecutionContext) -> Dict[str, Any]:
        success = service.sync_broken_code_to_redis(
            record.brand,
            remove_items={record.query_text or "": [record.target or ""]},
            external_request_id=context.external_request_id,
            allow_prod_recovery=True,
        )
        if not success:
            raise RuntimeError("assistant_cancel_demotion_executor_failed")
        return {
            "status": "success",
            "summary": "已执行取消降桶",
            "externalRequestId": context.external_request_id,
        }

    def readback(context: ExecutionContext) -> Dict[str, Any]:
        observed = service.get_ops_low_weight_config(
            record.brand,
            record.query_text or "",
        )
        if not isinstance(observed, dict) or observed.get("success") is not True:
            return {"status": "inconclusive", "reason": "authoritative_readback_unavailable"}
        product_ids = observed.get("product_ids")
        if not isinstance(product_ids, list):
            return {"status": "inconclusive", "reason": "authoritative_readback_invalid"}
        normalized_ids = {str(product_id).strip() for product_id in product_ids}
        if str(record.target or "").strip() in normalized_ids:
            return {"status": "failed", "reason": "target_remains_demoted"}
        if context.transaction is None:
            raise RuntimeError("assistant_cancel_demotion_local_transaction_missing")

        context.transaction.add(ActionLog(
            brand=record.brand,
            query_text=record.query_text,
            product_id=record.target,
            product_name="手动取消降桶",
            action_type=3,
            reason=f"助理确认执行({user.username})",
        ))
        recovered_tasks = context.transaction.query(InventoryRecoveryTask).filter(
            InventoryRecoveryTask.brand == record.brand,
            InventoryRecoveryTask.original_query == (record.query_text or ""),
            InventoryRecoveryTask.product_id == (record.target or ""),
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

    result = ActionGateway(
        authorizer=lambda candidate: candidate.operator == str(user.username or user.id),
    ).execute(
        intent,
        execute,
        readback=readback,
        atomic_local_transaction=True,
        authoritative_readback_on_success=True,
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=409, detail="Assistant action gateway returned an invalid terminal result")
    external_request_id = str(result.get("externalRequestId") or request_id)
    status = str(result.get("status") or "").strip().lower()
    return status, {**result, "externalRequestId": external_request_id}


@router.post("/api/assistant/actions/prepare")
def prepare_assistant_action(
    payload: Dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
):
    action_payload = payload.get("action") or {}
    session_id = str(payload.get("sessionId") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="sessionId is required")
    brand = str(action_payload.get("brand") or "").strip()
    if not _assistant_brand_allowed(user, brand):
        raise HTTPException(status_code=403, detail="Assistant brand is not accessible")
    db = SessionLocal()
    try:
        _ensure_assistant_session_access(
            db,
            session_id=session_id,
            user=user,
            expected_brand=brand,
            detail_prefix="Assistant session",
        )
        action_type = str(action_payload.get("actionType") or "").strip()
        if action_type in {"cancel_blacklist", "cancel_demotion"}:
            _ensure_action_permission(user, ACTION_CANCEL_GOVERNANCE_ACTIONS)
        else:
            _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        stored_action_payload = _prepare_assistant_action_payload(
            db,
            action_payload=action_payload,
            user=user,
            session_id=session_id,
        )
        record = AssistantActionRequest(
            session_id=session_id,
            user_id=user.id,
            brand=brand,
            action_type=str(action_payload.get("actionType") or ""),
            query_text=str(action_payload.get("query") or ""),
            target=str(action_payload.get("target") or ""),
            reason=str(action_payload.get("reason") or ""),
            risk_level=str(action_payload.get("riskLevel") or ""),
            payload_json=json.dumps(stored_action_payload, ensure_ascii=False),
            status="prepared",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return _serialize_assistant_action_request(record)
    finally:
        db.close()


@router.post("/api/assistant/actions/{action_request_id}/confirm")
def confirm_assistant_action(
    action_request_id: int,
    user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        record = db.get(AssistantActionRequest, action_request_id)
        if not record or record.user_id != user.id:
            raise HTTPException(status_code=404, detail="Assistant action not found")
        if not _assistant_brand_allowed(user, record.brand):
            raise HTTPException(status_code=403, detail="Assistant brand is not accessible")
        if record.status == "executed":
            return _serialize_assistant_action_request(record)
        if record.status != "prepared" and not (
            record.action_type == "cancel_demotion"
            and record.status == "execution_unknown"
        ):
            raise HTTPException(status_code=409, detail=f"Assistant action is not confirmable from status: {record.status}")
        _ensure_assistant_session_access(
            db,
            session_id=str(record.session_id or ""),
            user=user,
            expected_brand=record.brand,
            detail_prefix="Assistant session",
        )
        if record.action_type in {"cancel_blacklist", "cancel_demotion"}:
            _ensure_action_permission(user, ACTION_CANCEL_GOVERNANCE_ACTIONS)
        else:
            _ensure_action_permission(user, ACTION_EXECUTE_GOVERNANCE_ACTIONS)
        try:
            _validate_assistant_action_execution(db, record, user)
        except HTTPException as exc:
            claimed = db.query(AssistantActionRequest).filter(
                AssistantActionRequest.id == action_request_id,
                AssistantActionRequest.user_id == user.id,
                AssistantActionRequest.status == "prepared",
            ).update({
                AssistantActionRequest.status: "failed",
                AssistantActionRequest.result_summary: str(exc.detail),
                AssistantActionRequest.updated_at: datetime.datetime.now(),
            }, synchronize_session=False)
            if claimed != 1:
                db.rollback()
                raise HTTPException(status_code=409, detail="Assistant action was claimed concurrently")
            db.commit()
            raise
        if record.action_type == "cancel_demotion":
            try:
                terminal_status, gateway_result = _confirm_cancel_demotion_via_gateway(record, user)
            except ActionInProgressError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            except ActionExecutionFailedError:
                record.status = "failed"
                record.result_summary = "取消降桶执行失败"
                record.updated_at = datetime.datetime.now()
                db.commit()
                raise HTTPException(status_code=409, detail="取消降桶执行失败") from None

            action_payload = _json_object(record.payload_json)
            action_payload.pop("external_request_id", None)
            action_payload["externalRequestId"] = gateway_result["externalRequestId"]
            record.payload_json = json.dumps(action_payload, ensure_ascii=False)
            record.updated_at = datetime.datetime.now()
            if terminal_status == "success":
                record.status = "executed"
                record.result_summary = str(gateway_result.get("summary") or "已执行取消降桶")
                db.commit()
                db.refresh(record)
                return _serialize_assistant_action_request(record)

            record.status = "execution_unknown"
            record.result_summary = "取消降桶等待权威回读"
            db.commit()
            raise HTTPException(status_code=409, detail="取消降桶结果尚未确认")
        action_payload = _json_object(record.payload_json)
        external_request_id = str(
            action_payload.get("externalRequestId")
            or action_payload.get("external_request_id")
            or uuid.uuid4()
        )
        action_payload.pop("external_request_id", None)
        action_payload["externalRequestId"] = external_request_id
        claimed = db.query(AssistantActionRequest).filter(
            AssistantActionRequest.id == action_request_id,
            AssistantActionRequest.user_id == user.id,
            AssistantActionRequest.status == "prepared",
        ).update({
            AssistantActionRequest.payload_json: json.dumps(action_payload, ensure_ascii=False),
            AssistantActionRequest.status: "executing",
            AssistantActionRequest.result_summary: None,
            AssistantActionRequest.updated_at: datetime.datetime.now(),
        }, synchronize_session=False)
        if claimed != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="Assistant action was claimed concurrently")
        db.commit()
        db.refresh(record)
        try:
            summary = _assistant_execute_action(record, user, external_request_id)
        except Exception as exc:
            record.status = "execution_unknown"
            record.result_summary = str(exc)
            record.updated_at = datetime.datetime.now()
            db.commit()
            raise
        record.status = "executed"
        record.result_summary = summary
        record.updated_at = datetime.datetime.now()
        db.commit()
        db.refresh(record)
        return _serialize_assistant_action_request(record)
    finally:
        db.close()


@router.post("/api/assistant/actions/{action_request_id}/cancel")
def cancel_assistant_action(
    action_request_id: int,
    user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        record = db.get(AssistantActionRequest, action_request_id)
        if not record or record.user_id != user.id:
            raise HTTPException(status_code=404, detail="Assistant action not found")
        if not _assistant_brand_allowed(user, record.brand):
            raise HTTPException(status_code=403, detail="Assistant brand is not accessible")
        if record.status != "prepared":
            raise HTTPException(status_code=409, detail=f"Assistant action is not cancellable from status: {record.status}")
        _ensure_assistant_session_access(
            db,
            session_id=str(record.session_id or ""),
            user=user,
            expected_brand=record.brand,
            detail_prefix="Assistant session",
        )
        claimed = db.query(AssistantActionRequest).filter(
            AssistantActionRequest.id == action_request_id,
            AssistantActionRequest.user_id == user.id,
            AssistantActionRequest.status == "prepared",
        ).update({
            AssistantActionRequest.status: "cancelled",
            AssistantActionRequest.result_summary: "已取消",
            AssistantActionRequest.updated_at: datetime.datetime.now(),
        }, synchronize_session=False)
        if claimed != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="Assistant action was claimed concurrently")
        db.commit()
        db.refresh(record)
        return _serialize_assistant_action_request(record)
    finally:
        db.close()

def _build_assistant_chat_context(target_date: datetime.date, user_brands: List[str]) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        query_rows = db.query(DailyAbnormalQuery).filter(
            func.date(DailyAbnormalQuery.data_date) == target_date,
            DailyAbnormalQuery.brand.in_(user_brands),
        ).order_by(DailyAbnormalQuery.updated_at.desc(), DailyAbnormalQuery.created_at.desc()).limit(20).all()
        action_rows = db.query(ActionLog).filter(
            func.date(ActionLog.created_at) == target_date,
            ActionLog.brand.in_(user_brands),
        ).order_by(ActionLog.created_at.desc()).limit(30).all()
        inventory_rows = [
            row
            for brand_name in user_brands
            for row in read_inventory_recovery_tasks(db, brand=brand_name)
            if row.updated_at and row.updated_at.date() == target_date
        ]
        inventory_rows.sort(key=lambda row: row.updated_at, reverse=True)
        inventory_rows = inventory_rows[:20]
        pending_count = len([row for row in query_rows if _query_status(row) in {"pending", "reviewing"}])
        return {
            "date": target_date.strftime("%Y-%m-%d"),
            "summary": {
                "taskCount": len(query_rows),
                "pendingCount": pending_count,
                "actionCount": len(action_rows),
                "monitoringCount": len(inventory_rows),
            },
            "queries": [
                {
                    "id": row.id,
                    "brand": row.brand,
                    "query": row.query_text,
                    "anomalyType": row.anomaly_type,
                    "status": _query_status(row),
                    "reasonAnalysis": row.reason_analysis or "",
                    "actionSuggestions": row.action_suggestions or "",
                    "ctr": row.ctr,
                    "cvr": row.cvr,
                }
                for row in query_rows
            ],
            "recentActions": [
                {
                    "id": row.id,
                    "brand": row.brand,
                    "query": row.query_text,
                    "productId": row.product_id,
                    "actionType": row.action_type,
                    "reason": row.reason or "",
                }
                for row in action_rows
            ],
            "inventoryMonitoring": [
                {
                    "id": row.id,
                    "brand": row.brand,
                    "query": row.original_query,
                    "productId": row.product_id,
                    "status": row.status,
                    "lastBrokenRate": row.last_check_stock,
                }
                for row in inventory_rows
            ],
        }
    finally:
        db.close()


def _merge_assistant_page_context(
    context: Dict[str, Any],
    payload: Dict[str, Any],
    selected_brand: Optional[str],
    user: User,
) -> Dict[str, Any]:
    page_context = payload.get("pageContext")
    if not isinstance(page_context, dict):
        return context
    merged_page_context = dict(page_context)
    if selected_brand:
        merged_page_context["brand"] = selected_brand
    merged_context = {
        **context,
        "pageContext": merged_page_context,
    }
    selected_task_id = merged_page_context.get("selectedTaskId")
    if selected_task_id is not None:
        try:
            task_id = int(selected_task_id)
            db = SessionLocal()
            try:
                task = db.get(TopQueryTask, task_id)
                task_authorized = bool(
                    task
                    and str(task.brand or "").strip() == str(selected_brand or "").strip()
                    and _assistant_brand_allowed(user, task.brand)
                    and str(task.query_source or "top_query") == "top_query"
                )
                if task_authorized:
                    active_actions = db.query(TopQueryActionPlan).filter(
                        TopQueryActionPlan.task_id == task_id,
                    ).order_by(TopQueryActionPlan.id.desc()).limit(10).all()
                else:
                    active_actions = []
            finally:
                db.close()
            if not task_authorized:
                merged_page_context.pop("selectedTaskId", None)
                _log_assistant_authorization_denial(
                    "page_context_task_denied",
                    user_id=int(user.id or 0),
                    session_id=str(payload.get("sessionId") or "") or None,
                    task_id=task_id,
                )
                return merged_context
            merged_context["selectedTopQueryTask"] = {
                "taskId": task_id,
                "actions": _top_query_action_payloads(active_actions),
            }
        except (TypeError, ValueError):
            pass
    return merged_context

@router.post("/api/assistant/chat")
def assistant_chat(
    payload: Dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
):
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    db = SessionLocal()
    try:
        scope = _resolve_assistant_scope(db, user)
        selected_brand, selected_binding = _resolve_assistant_binding_for_request(
            db,
            user,
            scope,
            payload.get("brand"),
            int(payload["agentBindingId"]) if payload.get("agentBindingId") is not None else None,
        )
        allowed_brands = [selected_brand] if selected_brand else scope["brands"]
        target_date = _parse_dashboard_date(payload.get("date"))
        session_id = str(payload.get("sessionId") or f"assistant-{user.id}-{target_date.strftime('%Y%m%d')}")
        existing_session = _get_assistant_session_by_session_id(db, session_id=session_id, user_id=int(user.id or 0))
        if payload.get("brand"):
            selected_brand = _ensure_assistant_session_brand_locked(existing_session, selected_brand)
        allowed_brands = [selected_brand] if selected_brand else scope["brands"]
        context = _build_assistant_chat_context(target_date, allowed_brands)
        context = _merge_assistant_page_context(context, payload, selected_brand, user)
        service = _build_assistant_service(
            selected_binding,
            _active_assistant_model_config(db),
        )
    finally:
        db.close()
    # 模型调用可能持续数十秒；期间不占用数据库连接，落库时再开新会话。
    result = service.chat(
        message=message,
        user_id=int(user.id or 0),
        username=str(user.username),
        allowed_brands=allowed_brands,
        context=context,
        session_id=session_id,
        selected_brand=selected_brand,
    )
    db = SessionLocal()
    try:
        _upsert_assistant_session_meta(
            db,
            user=user,
            session_id=session_id,
            brand=selected_brand,
            agent_binding=selected_binding,
            operator_message=message,
            assistant_message=str(result.get("message") or ""),
            proposed_actions=result.get("proposedActions") or [],
            tool_calls=result.get("toolCalls") or [],
            tool_results=result.get("toolResults") or [],
            retry_of_message_id=str(payload.get("retryOfMessageId") or "") or None,
        )
    finally:
        db.close()
    return result

@router.post("/api/assistant/chat/stream")
def assistant_chat_stream(
    payload: Dict[str, Any] = Body(...),
    user: User = Depends(get_current_user),
):
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    db = SessionLocal()
    try:
        scope = _resolve_assistant_scope(db, user)
        selected_brand, selected_binding = _resolve_assistant_binding_for_request(
            db,
            user,
            scope,
            payload.get("brand"),
            int(payload["agentBindingId"]) if payload.get("agentBindingId") is not None else None,
        )
        allowed_brands = [selected_brand] if selected_brand else scope["brands"]
        target_date = _parse_dashboard_date(payload.get("date"))
        session_id = str(payload.get("sessionId") or f"assistant-{user.id}-{target_date.strftime('%Y%m%d')}")
        existing_session = _get_assistant_session_by_session_id(db, session_id=session_id, user_id=int(user.id or 0))
        if payload.get("brand"):
            selected_brand = _ensure_assistant_session_brand_locked(existing_session, selected_brand)
        allowed_brands = [selected_brand] if selected_brand else scope["brands"]
        context = _build_assistant_chat_context(target_date, allowed_brands)
        context = _merge_assistant_page_context(context, payload, selected_brand, user)
        service = _build_assistant_service(
            selected_binding,
            _active_assistant_model_config(db),
        )
    finally:
        db.close()

    def event_stream():
        chunks: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        for event in service.stream_chat(
            message=message,
            user_id=int(user.id or 0),
            username=str(user.username),
            allowed_brands=allowed_brands,
            context=context,
            session_id=session_id,
            selected_brand=selected_brand,
        ):
            event_payload = dict(event)
            if event_payload.get("type") == "delta" and event_payload.get("content"):
                chunks.append(str(event_payload.get("content") or ""))
            if event_payload.get("type") == "tool_call":
                tool_calls.append({
                    "id": str(event_payload.get("id") or ""),
                    "name": str(event_payload.get("name") or ""),
                    "arguments": str(event_payload.get("arguments") or ""),
                })
            if event_payload.get("type") == "tool_result":
                tool_results.append({
                    "toolCallId": str(event_payload.get("id") or ""),
                    "toolName": str(event_payload.get("toolName") or ""),
                    "summary": str(event_payload.get("summary") or ""),
                    "status": str(event_payload.get("status") or "completed"),
                    "output": str(event_payload.get("output") or ""),
                    "error": str(event_payload.get("error") or ""),
                    "exitCode": event_payload.get("exitCode"),
                    "durationMs": event_payload.get("durationMs"),
                })
            if event_payload.get("type") == "done":
                db_inner = SessionLocal()
                try:
                    _upsert_assistant_session_meta(
                        db_inner,
                        user=user,
                        session_id=session_id,
                        brand=selected_brand,
                        agent_binding=selected_binding,
                        operator_message=message,
                        assistant_message="".join(chunks),
                        proposed_actions=event_payload.get("proposedActions") or [],
                        tool_calls=tool_calls,
                        tool_results=tool_results,
                        retry_of_message_id=str(payload.get("retryOfMessageId") or "") or None,
                    )
                finally:
                    db_inner.close()
                event_payload.update({
                    "sessionId": session_id,
                    "proposedActions": event_payload.get("proposedActions") or [],
                    "toolResults": event_payload.get("toolResults") or tool_results,
                    "runtime": service.runtime_info(),
                })
            yield f"data: {json.dumps(event_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
