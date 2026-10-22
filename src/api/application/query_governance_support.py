"""Query Governance application helpers and presentation payloads."""

from __future__ import annotations

import copy
import math
import sqlite3
from fastapi import HTTPException, status
from typing import List, Optional, Dict, Any
import datetime
import json
from pathlib import Path
from sqlalchemy import func
from sqlalchemy.exc import DatabaseError
from src.persistence import SessionLocal, User, DailyAbnormalQuery, NoResultAgentEvent, NoResultGovernanceAction, NoResultQuery, OperationLog, BrandDailyMetric, TopQueryActionPlan, TopQueryAgentEvent, TopQueryContextSnapshot, TopQueryExperiment, TopQueryExperimentSample, TopQueryStepStatus, TopQueryTask
from src.config import get_env
from src.top_query.service import sanitize_action_error_details
from src.top_query.query_demotion_contract import LEGACY_QUERY_DEMOTION_BLOCK_REASON, should_block_legacy_query_demotion, uses_authoritative_query_demotion
from src.governance.query_identity import normalize_governance_query_key, normalize_query_key
from src.top_query.schemas import TOP_QUERY_GOVERNANCE_EVENT_TYPES
import logging
from ..dependencies import *  # noqa: F401,F403
logger = logging.getLogger(__name__)

# --- Query Governance 多 Agent 治理接口 ---


def _ensure_top_query_brand_permission(brand: str, user: User) -> None:
    if brand not in _user_brand_permissions(user):
        raise HTTPException(status_code=403, detail="No permission for brand")


def _ensure_top_query_experiment_permission(experiment_id: int, user: User) -> TopQueryExperiment:
    db = SessionLocal()
    try:
        experiment = db.get(TopQueryExperiment, experiment_id)
        if not experiment:
            raise HTTPException(status_code=404, detail="Query Governance experiment not found")
        _ensure_top_query_brand_permission(str(experiment.brand), user)
        db.expunge(experiment)
        return experiment
    finally:
        db.close()

def _top_query_step_payload(step: TopQueryStepStatus) -> Dict[str, Any]:
    evidence = _safe_json_loads(step.evidence)
    summary = _operator_safe_world_knowledge_summary(step.summary, evidence)
    status = step.status
    display_status = step.display_status
    if step.check_key == "open_factor" and _requires_world_knowledge_manual_attention(step.summary, evidence):
        status, display_status = "warning", "waiting_human_approval"
    return {
        "id": step.id,
        "checkKey": step.check_key,
        "status": status,
        "displayStatus": display_status,
        "summary": summary,
        "evidence": evidence,
        "beforeSnapshot": _safe_json_loads(step.before_snapshot),
        "afterSnapshot": _safe_json_loads(step.after_snapshot),
    }


TOP_QUERY_STEP_ORDER = ["recall", "relevance", "open_factor", "inventory"]


def _is_legacy_world_knowledge_audit_text(value: Any) -> bool:
    text = str(value or "").strip()
    if text.startswith("世界知识综合评估结构不完整，需人工复核:"):
        return True
    schema_tokens = [".status", "query_relevance", ".evidence", "expected_impact", "candidate_tags"]
    factor_tokens = ["season", "weather", "festival", "social_trend", "factor_assessments"]
    return (
        sum(token in text for token in schema_tokens) >= 2
        and any(token in text for token in factor_tokens)
        and ("缺失" in text or "必须是" in text)
    )


def _operator_safe_world_knowledge_summary(summary: Any, evidence: Any) -> str:
    decision = evidence.get("world_knowledge_decision") if isinstance(evidence, dict) else None
    if isinstance(decision, dict):
        audit_status = str(decision.get("audit_status") or "").strip().lower()
        if audit_status == "invalid":
            return "世界知识评估信息不完整，需人工复核后再决定是否执行标签权重调整。"
        if audit_status == "unavailable":
            return "世界知识评估暂不可用，需人工复核后再决定是否执行标签权重调整。"
        if _is_legacy_world_knowledge_audit_text(decision.get("decision_reason")):
            return "世界知识评估信息不完整，需人工复核后再决定是否执行标签权重调整。"
    if _is_legacy_world_knowledge_audit_text(summary):
        return "世界知识评估信息不完整，需人工复核后再决定是否执行标签权重调整。"
    return str(summary or "")


def _requires_world_knowledge_manual_attention(summary: Any, evidence: Any) -> bool:
    decision = evidence.get("world_knowledge_decision") if isinstance(evidence, dict) else None
    if isinstance(decision, dict):
        if str(decision.get("audit_status") or "").strip().lower() in {"invalid", "unavailable"}:
            return True
        if _is_legacy_world_knowledge_audit_text(decision.get("decision_reason")):
            return True
    return _is_legacy_world_knowledge_audit_text(summary)


def _normalize_dimension_detail_for_actions(
    check_key: str,
    detail: Dict[str, Any],
    actions: Optional[List[TopQueryActionPlan]] = None,
) -> Dict[str, Any]:
    normalized = dict(detail or {})
    if not actions:
        return normalized
    check_key = str(check_key or "")
    relevant_actions = [
        action for action in actions
        if str(action.check_key or "") == check_key
        and str(action.execution_mode or "") == "human_approval"
    ]
    has_pending_action = any(
        str(action.status or "").lower() in {"pending", "approved", "waiting_human_approval"}
        for action in relevant_actions
    )
    if has_pending_action:
        normalized["status"] = "waiting_human_approval"
        normalized["summary"] = str(normalized.get("summary") or "当前治理动作仍待人工确认。")
        normalized["remainingIssue"] = str(normalized.get("remainingIssue") or "治理动作仍待人工确认")
        return normalized

    has_executed_action = any(
        str(action.status or "").lower() in {"executed", "completed"}
        for action in actions
        if str(action.check_key or "") == check_key
        and str(action.execution_mode or "") == "human_approval"
    )
    if has_executed_action:
        normalized["status"] = "improved"
        normalized["summary"] = str(normalized.get("summary") or "治理动作已执行，待观察后续结果。")
        normalized["remainingIssue"] = ""
    return normalized


def _merged_top_query_step_payloads(
    steps: Optional[List[TopQueryStepStatus]],
) -> List[Dict[str, Any]]:
    return [_top_query_step_payload(step) for step in (steps or [])]

def _latest_steps_by_task(steps: List[TopQueryStepStatus]) -> Dict[int, List[TopQueryStepStatus]]:
    history: Dict[int, Dict[str, List[TopQueryStepStatus]]] = {}
    latest: Dict[int, Dict[str, TopQueryStepStatus]] = {}
    for step in steps:
        if not step.task_id or not step.check_key:
            continue
        history.setdefault(step.task_id, {}).setdefault(step.check_key, []).append(step)
        task_steps = latest.setdefault(step.task_id, {})
        current = task_steps.get(step.check_key)
        if current is None or (step.id or 0) >= (current.id or 0):
            task_steps[step.check_key] = step
    normalized: Dict[int, List[TopQueryStepStatus]] = {}
    for task_id, task_steps in latest.items():
        task_result: List[TopQueryStepStatus] = []
        for check_key, step in task_steps.items():
            effective_step = step
            prior_steps = history.get(task_id, {}).get(check_key, [])
            has_fixed_history = any(
                str(item.status or "").lower() in {"fixed", "completed", "improved"}
                or str(item.display_status or "").lower() in {"fixed", "completed", "improved"}
                for item in prior_steps
            )
            normalized_status = str(step.status or "").lower()
            normalized_display_status = str(step.display_status or "").lower()
            if has_fixed_history and normalized_status in {"pass", "normal"} and normalized_display_status in {"pass", "normal"}:
                effective_step = copy.copy(step)
                effective_step.status = "fixed" # type: ignore[attr-defined]
                effective_step.display_status = "fixed" # type: ignore[attr-defined]
            task_result.append(effective_step)
        normalized[task_id] = task_result
    return normalized


def _safe_optional_query_all(query, *, context: str) -> List[Any]:
    try:
        return query.all()
    except DatabaseError as exc:
        print(f"读取可选治理数据失败，已降级为空结果: {context}: {exc}")
        return []


def _safe_optional_query_one(query, *, context: str, default: Any) -> Any:
    try:
        return query.one()
    except DatabaseError as exc:
        print(f"读取可选治理数据失败，已降级为默认值: {context}: {exc}")
        return default


def _safe_optional_query_scalar(query, *, context: str, default: Any = None) -> Any:
    try:
        return query.scalar()
    except DatabaseError as exc:
        print(f"读取可选治理数据失败，已降级为默认值: {context}: {exc}")
        return default


def _sqlite_db_path_for_session(db) -> Optional[Path]:
    bind = db.get_bind()
    if bind.dialect.name != "sqlite":
        return None
    database = getattr(bind.url, "database", None)
    if not database or database == ":memory:":
        return None
    return Path(database)


def _parse_sqlite_datetime(value: Any) -> Optional[datetime.datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _raw_sqlite_connect(db) -> Optional[sqlite3.Connection]:
    db_path = _sqlite_db_path_for_session(db)
    if not db_path:
        return None
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _raw_top_query_steps_by_task(db, task_ids: List[int]) -> Dict[int, List[TopQueryStepStatus]]:
    steps_by_task: Dict[int, List[TopQueryStepStatus]] = {}
    if not task_ids:
        return steps_by_task
    connection = _raw_sqlite_connect(db)
    if connection is None:
        return steps_by_task
    try:
        for task_id in task_ids:
            rows = []
            try:
                cursor = connection.execute(
                    """
                    SELECT id, task_id, brand, query_text, check_key, status,
                           display_status, summary, evidence, created_at
                    FROM top_query_step_status
                    WHERE task_id = ?
                    ORDER BY id ASC
                    """,
                    (task_id,),
                )
                rows = []
                while True:
                    chunk = cursor.fetchmany(16)
                    if not chunk:
                        break
                    rows.extend(chunk)
            except Exception as exc:
                print(f"sqlite 原始读取 TopQueryStepStatus 失败，task_id={task_id}: {exc}")
            if not rows:
                continue
            items = [
                TopQueryStepStatus(
                    id=row["id"],
                    task_id=row["task_id"],
                    brand=row["brand"],
                    query_text=row["query_text"],
                    check_key=row["check_key"],
                    status=row["status"],
                    display_status=row["display_status"],
                    summary=row["summary"],
                    evidence=row["evidence"],
                    created_at=_parse_sqlite_datetime(row["created_at"]),
                )
                for row in rows
            ]
            steps_by_task[task_id] = list(_latest_steps_by_task(items).get(task_id, items))
    finally:
        connection.close()
    return steps_by_task


def _raw_top_query_events_for_task(db, task_id: int) -> List[TopQueryAgentEvent]:
    connection = _raw_sqlite_connect(db)
    if connection is None:
        return []
    rows = []
    try:
        cursor = connection.execute(
            """
            SELECT id, created_at, task_id, brand, query_text, agent_name, stage,
                   event_type, summary, output_payload, duration, token_usage
            FROM top_query_agent_event
            WHERE task_id = ?
            ORDER BY id ASC
            """,
            (task_id,),
        )
        rows = []
        while True:
            chunk = cursor.fetchmany(16)
            if not chunk:
                break
            rows.extend(chunk)
    except Exception as exc:
        print(f"sqlite 原始读取 TopQueryAgentEvent 失败，task_id={task_id}: {exc}")
    finally:
        connection.close()
    return [
        TopQueryAgentEvent(
            id=row["id"],
            created_at=_parse_sqlite_datetime(row["created_at"]),
            task_id=row["task_id"],
            brand=row["brand"],
            query_text=row["query_text"],
            agent_name=row["agent_name"],
            stage=row["stage"],
            event_type=row["event_type"],
            summary=row["summary"],
            output_payload=row["output_payload"],
            duration=row["duration"],
            token_usage=row["token_usage"],
        )
        for row in rows
    ]


def _safe_top_query_step_rows(query, *, context: str) -> List[TopQueryStepStatus]:
    rows = _safe_optional_query_all(query, context=context)
    return [row for row in rows if isinstance(row, TopQueryStepStatus)]


def _safe_latest_steps_by_task(db, task_ids: List[int], *, context: str) -> Dict[int, List[TopQueryStepStatus]]:
    if not task_ids:
        return {}
    try:
        rows = db.query(TopQueryStepStatus).filter(TopQueryStepStatus.task_id.in_(task_ids)).all()
        return _latest_steps_by_task(rows)
    except DatabaseError as exc:
        print(f"读取可选治理数据失败，尝试 sqlite 原始兜底: {context}: {exc}")
        return _raw_top_query_steps_by_task(db, task_ids)


def _safe_steps_for_task_detail(db, task_id: int, *, context: str) -> List[TopQueryStepStatus]:
    try:
        return db.query(TopQueryStepStatus).filter(TopQueryStepStatus.task_id == task_id).order_by(TopQueryStepStatus.id.asc()).all()
    except DatabaseError as exc:
        print(f"读取可选治理数据失败，尝试 sqlite 原始兜底: {context}: {exc}")
        return _raw_top_query_steps_by_task(db, [task_id]).get(task_id, [])


def _safe_top_query_actions_by_task(db, task_ids: List[int], *, context: str) -> Dict[int, List[TopQueryActionPlan]]:
    actions_by_task: Dict[int, List[TopQueryActionPlan]] = {}
    if not task_ids:
        return actions_by_task
    for action in _safe_optional_query_all(
        db.query(TopQueryActionPlan).filter(TopQueryActionPlan.task_id.in_(task_ids)).order_by(TopQueryActionPlan.id.asc()),
        context=context,
    ):
        if isinstance(action, TopQueryActionPlan):
            actions_by_task.setdefault(action.task_id, []).append(action)
    return actions_by_task


def _safe_top_query_event_totals(db, task_ids: List[int], *, context: str) -> tuple[float, int]:
    if not task_ids:
        return 0.0, 0
    try:
        total_duration, total_tokens = db.query(
            func.coalesce(func.sum(TopQueryAgentEvent.duration), 0),
            func.coalesce(func.sum(TopQueryAgentEvent.token_usage), 0),
        ).filter(TopQueryAgentEvent.task_id.in_(task_ids)).one()
        return float(total_duration or 0), int(total_tokens or 0)
    except DatabaseError as exc:
        print(f"读取可选治理数据失败，尝试 sqlite 原始兜底: {context}: {exc}")
        total_duration = 0.0
        total_tokens = 0
        for task_id in task_ids:
            for event in _raw_top_query_events_for_task(db, task_id):
                total_duration += float(event.duration or 0)
                total_tokens += int(event.token_usage or 0)
        return total_duration, total_tokens


def _safe_top_query_latest_event_time(db, task_ids: List[int], *, context: str):
    if not task_ids:
        return None
    try:
        return db.query(func.max(TopQueryAgentEvent.created_at)).filter(TopQueryAgentEvent.task_id.in_(task_ids)).scalar()
    except DatabaseError as exc:
        print(f"读取可选治理数据失败，尝试 sqlite 原始兜底: {context}: {exc}")
        latest: Optional[datetime.datetime] = None
        for task_id in task_ids:
            for event in _raw_top_query_events_for_task(db, task_id):
                if event.created_at and (latest is None or event.created_at > latest):
                    latest = event.created_at
        return latest


def _safe_top_query_events(db, task_id: int, *, context: str) -> List[TopQueryAgentEvent]:
    try:
        rows = db.query(TopQueryAgentEvent).filter(
            TopQueryAgentEvent.task_id == task_id,
            TopQueryAgentEvent.event_type.in_(TOP_QUERY_GOVERNANCE_EVENT_TYPES),
        ).order_by(TopQueryAgentEvent.id.asc()).all()
        return [row for row in rows if isinstance(row, TopQueryAgentEvent)]
    except DatabaseError as exc:
        print(f"读取可选治理数据失败，尝试 sqlite 原始兜底: {context}: {exc}")
        return [
            event for event in _raw_top_query_events_for_task(db, task_id)
            if event.event_type in TOP_QUERY_GOVERNANCE_EVENT_TYPES
        ]


def _safe_latest_planner_event(db, task_id: int, *, context: str) -> Optional[TopQueryAgentEvent]:
    try:
        row = db.query(TopQueryAgentEvent).filter(
            TopQueryAgentEvent.task_id == task_id,
            TopQueryAgentEvent.agent_name == "PlannerAgent",
            TopQueryAgentEvent.event_type == "plan_created",
        ).order_by(TopQueryAgentEvent.id.desc()).first()
        return row if isinstance(row, TopQueryAgentEvent) else None
    except DatabaseError as exc:
        print(f"读取可选治理数据失败，尝试 sqlite 原始兜底: {context}: {exc}")
        for event in reversed(_raw_top_query_events_for_task(db, task_id)):
            if event.agent_name == "PlannerAgent" and event.event_type == "plan_created":
                return event
        return None


def _resolved_top_query_display_status(task: TopQueryTask, steps: Optional[List[TopQueryStepStatus]] = None) -> str:
    if not steps:
        return str(task.display_status or "normal")
    normalized_statuses = [
        str(step.display_status or step.status or "").lower()
        for step in steps
        if step is not None
    ]
    if any(status_value in {"waiting_human_approval", "manual_required", "warning"} for status_value in normalized_statuses):
        return "waiting_human_approval"
    if any(status_value in {"still_problematic", "failed", "check_failed"} for status_value in normalized_statuses):
        return "still_problematic"
    if any(status_value in {"fixed", "completed", "improved"} for status_value in normalized_statuses):
        return "fixed"
    return "normal"

def _snapshot_body(snapshot: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return None
    body = snapshot.get("after") or snapshot.get("before") or snapshot
    return body if isinstance(body, dict) else None


def _snapshot_branch(snapshot: Any, preferred_branch: str) -> Optional[Dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return None
    preferred = snapshot.get(preferred_branch)
    if isinstance(preferred, dict):
        return preferred
    fallback_branch = "before" if preferred_branch == "after" else "after"
    fallback = snapshot.get(fallback_branch)
    if isinstance(fallback, dict):
        return fallback
    return snapshot if isinstance(snapshot, dict) else None


def _context_snapshot_payload(db, task_id: int, context_type: str) -> Optional[Dict[str, Any]]:
    record = db.query(TopQueryContextSnapshot).filter(
        TopQueryContextSnapshot.task_id == task_id,
        TopQueryContextSnapshot.context_type == context_type,
    ).order_by(TopQueryContextSnapshot.id.desc()).first()
    payload = _safe_json_loads(record.payload, {}) if record else {}
    return payload if isinstance(payload, dict) else None


def _merge_snapshot_with_context(snapshot: Any, context_snapshot: Any, branch: str) -> Any:
    context_body = _snapshot_branch(context_snapshot, branch)
    if context_body is None:
        return snapshot
    existing_snapshot = snapshot if isinstance(snapshot, dict) else {}
    existing_body = _snapshot_branch(existing_snapshot, branch) or {}
    merged_body = dict(context_body)
    products = context_body.get("products") or context_body.get("items") or context_body.get("results")
    if isinstance(products, list):
        merged_products: List[Dict[str, Any]] = []
        existing_by_id: Dict[str, Dict[str, Any]] = {}
        for item in products:
            if not isinstance(item, dict):
                continue
            product_id = str(item.get("productId") or item.get("product_id") or item.get("spuId") or item.get("skuId") or "")
            if product_id:
                existing_item = next((
                    product for product in (existing_body.get("products") or [])
                    if isinstance(product, dict)
                    and str(product.get("productId") or product.get("product_id") or product.get("spuId") or product.get("skuId") or "") == product_id
                ), None)
                if isinstance(existing_item, dict):
                    existing_by_id[product_id] = existing_item
            merged_products.append({**item, **existing_by_id.get(product_id, {})})
        merged_body["products"] = merged_products
    for key in ["result_count", "previous_result_count", "relevance_diagnostics"]:
        if key not in merged_body and key in existing_body:
            merged_body[key] = existing_body.get(key)
    if branch in existing_snapshot and isinstance(existing_snapshot.get(branch), dict):
        merged_snapshot = dict(existing_snapshot)
        merged_snapshot[branch] = merged_body
        return merged_snapshot
    return {branch: merged_body}


def _normalize_snapshot_price_fields(snapshot: Any) -> Any:
    def _is_valid_price(value: Any) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and float(value) > 0
        )

    def _consistent_size_row_prices(item: Dict[str, Any]) -> Optional[tuple[float, float]]:
        size_inventory = item.get("size_inventory")
        rows = size_inventory.get("size_rows") if isinstance(size_inventory, dict) else None
        pairs = set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            original_price = row.get("oldPrice")
            discount_price = row.get("price")
            if not _is_valid_price(original_price) or not _is_valid_price(discount_price):
                continue
            if float(original_price) <= float(discount_price):
                return None
            pairs.add((float(original_price), float(discount_price)))
        if len(pairs) != 1:
            return None
        original_price, discount_price = next(iter(pairs))
        return original_price / 100, discount_price / 100

    def _replace_body(normalized_body: Dict[str, Any]) -> Any:
        normalized_snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
        if "after" in normalized_snapshot and isinstance(normalized_snapshot["after"], dict):
            normalized_snapshot["after"] = normalized_body
            return normalized_snapshot
        if "before" in normalized_snapshot and isinstance(normalized_snapshot["before"], dict):
            normalized_snapshot["before"] = normalized_body
            return normalized_snapshot
        return normalized_body

    body = _snapshot_body(snapshot)
    if body is None:
        return snapshot
    products = body.get("products") or body.get("items") or body.get("results") or []
    if not isinstance(products, list) or not products:
        return snapshot
    enriched_products = []
    for product in products:
        if not isinstance(product, dict):
            enriched_products.append(product)
            continue
        item = dict(product)
        size_row_prices = _consistent_size_row_prices(item)
        if size_row_prices:
            original_price, discount_price = size_row_prices
            existing_original_price = item.get("originalPrice")
            existing_discount_price = item.get("discountPrice")
            if (
                _is_valid_price(existing_original_price)
                and not math.isclose(float(existing_original_price), original_price)
            ) or (
                _is_valid_price(existing_discount_price)
                and not math.isclose(float(existing_discount_price), discount_price)
            ):
                enriched_products.append(item)
                continue
            if not _is_valid_price(item.get("originalPrice")):
                item["originalPrice"] = original_price
            if not _is_valid_price(item.get("discountPrice")):
                item["discountPrice"] = discount_price
        enriched_products.append(item)
    normalized_body = dict(body)
    normalized_body["products"] = enriched_products
    return _replace_body(normalized_body)


def _override_snapshot_result_count(snapshot: Any, result_count: Optional[int]) -> Any:
    if result_count is None:
        return snapshot
    body = _snapshot_body(snapshot)
    if body is None:
        return snapshot
    normalized_body = dict(body)
    normalized_body["result_count"] = int(result_count)
    normalized_snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
    if "after" in normalized_snapshot and isinstance(normalized_snapshot["after"], dict):
        normalized_snapshot["after"] = normalized_body
        return normalized_snapshot
    if "before" in normalized_snapshot and isinstance(normalized_snapshot["before"], dict):
        normalized_snapshot["before"] = normalized_body
        return normalized_snapshot
    return normalized_body

def _top_query_task_raw_payload(task: TopQueryTask) -> Dict[str, Any]:
    raw_payload = _safe_json_loads(task.raw_payload)
    return raw_payload if isinstance(raw_payload, dict) else {}


def _is_top_query_invalidated(task: TopQueryTask) -> bool:
    return (
        str(task.status or "").lower() in {"cancelled", "superseded"}
        or str(task.display_status or "").lower() == "superseded"
    )

def _top_query_task_payload(
    task: TopQueryTask,
    steps: Optional[List[TopQueryStepStatus]] = None,
    actions: Optional[List[TopQueryActionPlan]] = None,
) -> Dict[str, Any]:
    step_payloads = _merged_top_query_step_payloads(steps) if steps is not None else None
    resolved_display_status = _resolved_top_query_display_status(
        task,
        steps if steps is not None else None,
    )
    if step_payloads:
        synthetic_steps: List[TopQueryStepStatus] = []
        for payload in step_payloads:
            synthetic_steps.append(TopQueryStepStatus(
                check_key=str(payload.get("checkKey") or ""),
                status=str(payload.get("status") or ""),
                display_status=str(payload.get("displayStatus") or ""),
            ))
        resolved_display_status = _resolved_top_query_display_status(task, synthetic_steps)
    payload = {
        "id": task.id,
        "brand": task.brand,
        "dataDate": task.data_date.isoformat() if task.data_date else None,
        "queryText": task.query_text,
        "queryRank": task.query_rank,
        "searchUv": task.search_uv,
        "searchPv": task.search_pv,
        "ctr": task.ctr,
        "cvr": task.cvr,
        "avgCtr": task.avg_ctr,
        "avgCvr": task.avg_cvr,
        "queryPvShare": task.query_pv_share,
        "status": task.status,
        "displayStatus": resolved_display_status,
        "resultCount": task.result_count,
        "querySource": task.query_source or "top_query",
        "hotWordType": task.hot_word_type,
        "hotStatus": task.hot_status,
        "invalidated": _is_top_query_invalidated(task),
    }
    if step_payloads is not None:
        payload["stepStatuses"] = step_payloads
    return payload

def _top_query_query_trend(db, task: TopQueryTask) -> List[Dict[str, Any]]:
    max_points = 30
    rows = db.query(TopQueryTask).filter(
        TopQueryTask.brand == task.brand,
        TopQueryTask.query_text == task.query_text,
        func.coalesce(TopQueryTask.query_source, "top_query") == str(task.query_source or "top_query"),
        TopQueryTask.status != "superseded",
    ).order_by(TopQueryTask.data_date.desc(), TopQueryTask.id.desc()).limit(max_points * 3).all()
    latest_by_date: Dict[datetime.date, TopQueryTask] = {}
    for row in rows:
        if not row.data_date or row.data_date in latest_by_date:
            continue
        latest_by_date[row.data_date] = row
        if len(latest_by_date) >= max_points:
            break

    trend: List[Dict[str, Any]] = []
    for row in sorted(latest_by_date.values(), key=lambda item: (item.data_date or datetime.date.min, item.id or 0)):
        raw_payload = _top_query_task_raw_payload(row)
        search_uv = float(row.search_uv or 0)
        ctr = float(row.ctr or 0)
        cvr = float(row.cvr or 0)
        click_uv = raw_payload.get("click_uv")
        if click_uv is None:
            click_uv = round(search_uv * ctr)
        add_to_cart_uv = raw_payload.get("add_to_cart_uv")
        purchase_uv = raw_payload.get("purchase_uv")
        if purchase_uv is None:
            purchase_uv = round(search_uv * cvr)
        atc = raw_payload.get("atc")
        trend.append({
            "date": row.data_date.strftime("%m-%d") if row.data_date else "",
            "uv": int(search_uv),
            "clickUv": int(float(click_uv or 0)),
            "addToCartUv": int(float(add_to_cart_uv or 0)) if add_to_cart_uv is not None else None,
            "purchaseUv": int(float(purchase_uv or 0)),
            "ctr": ctr,
            "atc": float(atc) if atc is not None else None,
            "cvr": cvr,
        })
    return trend

def _top_query_action_evidence(snapshot: Any, key: str) -> Optional[Dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return None
    raw = snapshot.get(key)
    if not isinstance(raw, dict):
        return None
    expected_version = raw.get("expectedVersion")
    actual_version = raw.get("actualVersion")
    checked_at = raw.get("checkedAt")
    error_reason = raw.get("errorReason")
    return {
        **raw,
        "version": actual_version or expected_version or raw.get("version"),
        "reason": error_reason if error_reason is not None else raw.get("reason"),
        "verifiedAt": checked_at or raw.get("verifiedAt"),
    }


def _top_query_action_restore_links(
    actions: List[TopQueryActionPlan],
) -> Dict[int, TopQueryActionPlan]:
    actions_by_id = {
        int(action.id): action
        for action in actions
        if action.id is not None
    }
    links: Dict[int, TopQueryActionPlan] = {}
    status_priority = {
        "rollback_failed": 1,
        "rollback_pending": 2,
        "rolled_back": 3,
    }
    for restore in actions:
        request_payload = _safe_json_loads(restore.request_payload)
        request_record = request_payload if isinstance(request_payload, dict) else {}
        restore_before = _safe_json_loads(restore.before_snapshot)
        restore_before = restore_before if isinstance(restore_before, dict) else {}
        status = str(restore.status or "")
        raw_source_ids = request_record.get("rollbackOfActionIds")
        source_group_request_id = str(
            request_record.get("sourceGroupExternalRequestId") or ""
        ).strip()
        if (
            str(restore.action_type or "") != "query_demote"
            or request_record.get("operation") != "restore"
            or not uses_authoritative_query_demotion(request_record)
            or status not in status_priority
            or not isinstance(raw_source_ids, list)
            or not raw_source_ids
            or any(
                isinstance(action_id, bool) or not isinstance(action_id, int)
                for action_id in raw_source_ids
            )
            or not source_group_request_id
        ):
            continue
        source_ids = sorted(set(raw_source_ids))
        sources = [actions_by_id.get(action_id) for action_id in source_ids]
        if any(source is None for source in sources):
            continue
        valid_group = True
        group_identity: Optional[Dict[str, Any]] = None
        for source in sources:
            assert source is not None
            source_request = _safe_json_loads(source.request_payload)
            source_before = _safe_json_loads(source.before_snapshot)
            raw_group_ids = source_before.get("actionIds") if isinstance(source_before, dict) else None
            raw_before_ids = source_before.get("beforeIds") if isinstance(source_before, dict) else None
            raw_desired_ids = source_before.get("desiredIds") if isinstance(source_before, dict) else None
            brand = str(source_before.get("brand") or "").strip() if isinstance(source_before, dict) else ""
            normalized_query = str(source_before.get("normalizedQuery") or "").strip() if isinstance(source_before, dict) else ""
            leader_action_id = source_before.get("leaderActionId") if isinstance(source_before, dict) else None
            before_ids = _top_query_action_id_values(raw_before_ids)
            desired_ids = _top_query_action_id_values(raw_desired_ids)
            if (
                int(source.task_id) != int(restore.task_id)
                or str(source.action_type or "") != "query_demote"
                or not isinstance(source_request, dict)
                or not uses_authoritative_query_demotion(source_request)
                or source_request.get("operation") == "restore"
                or not isinstance(source_before, dict)
                or source_before.get("complete") is not True
                or not brand
                or not normalized_query
                or normalize_query_key(source.brand) != normalize_query_key(brand)
                or normalize_governance_query_key(brand, source.query_text)
                != normalize_governance_query_key(brand, normalized_query)
                or before_ids is None
                or desired_ids is None
                or not isinstance(raw_group_ids, list)
                or any(
                    isinstance(action_id, bool) or not isinstance(action_id, int)
                    for action_id in raw_group_ids
                )
                or sorted(set(raw_group_ids)) != source_ids
                or not isinstance(leader_action_id, int)
                or isinstance(leader_action_id, bool)
                or leader_action_id not in source_ids
                or str(source_before.get("groupExternalRequestId") or "").strip()
                != source_group_request_id
            ):
                valid_group = False
                break
            source_identity = {
                "brand": normalize_query_key(brand),
                "normalizedQuery": normalize_governance_query_key(brand, normalized_query),
                "beforeIds": before_ids,
                "desiredIds": desired_ids,
                "leaderActionId": leader_action_id,
                "actionIds": source_ids,
                "groupExternalRequestId": source_group_request_id,
            }
            if group_identity is None:
                group_identity = source_identity
            elif group_identity != source_identity:
                valid_group = False
                break
        if not valid_group:
            continue
        assert group_identity is not None
        restore_request_before_ids = _top_query_action_id_values(request_record.get("beforeIds"))
        restore_request_expected_ids = _top_query_action_id_values(request_record.get("expectedIds"))
        restore_before_ids = _top_query_action_id_values(restore_before.get("beforeIds"))
        restore_desired_ids = _top_query_action_id_values(restore_before.get("desiredIds"))
        restore_source_ids = restore_before.get("sourceActionIds")
        if (
            restore_before.get("complete") is not True
            or normalize_query_key(request_record.get("brand")) != group_identity["brand"]
            or normalize_governance_query_key(
                request_record.get("brand"),
                request_record.get("query"),
            ) != group_identity["normalizedQuery"]
            or normalize_query_key(restore.brand) != group_identity["brand"]
            or normalize_governance_query_key(restore.brand, restore.query_text)
            != group_identity["normalizedQuery"]
            or normalize_query_key(restore_before.get("brand")) != group_identity["brand"]
            or normalize_governance_query_key(
                restore_before.get("brand"),
                restore_before.get("normalizedQuery"),
            ) != group_identity["normalizedQuery"]
            or restore_request_before_ids != group_identity["beforeIds"]
            or restore_request_expected_ids != group_identity["beforeIds"]
            or restore_before_ids != group_identity["desiredIds"]
            or restore_desired_ids != group_identity["beforeIds"]
            or not isinstance(restore_source_ids, list)
            or any(
                isinstance(action_id, bool) or not isinstance(action_id, int)
                for action_id in restore_source_ids
            )
            or sorted(set(restore_source_ids)) != source_ids
            or str(
                restore_before.get("sourceGroupExternalRequestId") or ""
            ).strip() != source_group_request_id
        ):
            continue
        for source_id in source_ids:
            current = links.get(source_id)
            current_priority = status_priority.get(str(current.status or ""), 0) if current else 0
            is_newer_same_status = bool(
                current
                and status_priority[status] == current_priority
                and int(restore.id or 0) > int(current.id or 0)
            )
            if status_priority[status] > current_priority or is_newer_same_status:
                links[source_id] = restore
    return links


def _top_query_action_id_values(value: Any) -> Optional[List[str]]:
    if not isinstance(value, list):
        return None
    normalized: List[str] = []
    for item in value:
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            return None
        item_value = str(item).strip()
        if not item_value:
            return None
        normalized.append(item_value)
    return sorted(set(normalized))


def _top_query_action_payload(
    action: TopQueryActionPlan,
    rollback_action: Optional[TopQueryActionPlan] = None,
) -> Dict[str, Any]:
    request_payload = _safe_json_loads(action.request_payload)
    response_payload = _safe_json_loads(action.response_payload)
    request_record = request_payload if isinstance(request_payload, dict) else {}
    response_record = response_payload if isinstance(response_payload, dict) else {}
    before_snapshot = sanitize_action_error_details(_safe_json_loads(action.before_snapshot))
    after_snapshot = sanitize_action_error_details(_safe_json_loads(action.after_snapshot))
    kg_plan = _top_query_kg_plan_payload(action, request_payload)
    raw_lifecycle = response_record.get("lifecycle") if isinstance(response_record.get("lifecycle"), dict) else {}
    verification = response_record.get("world_knowledge_verification")
    verification = verification if isinstance(verification, dict) else {}
    proposed_at = raw_lifecycle.get("proposedAt") or (action.created_at.isoformat() if action.created_at else None)
    approved_at = (
        raw_lifecycle.get("approvedAt")
        or response_record.get("approvedAt")
        or response_record.get("approved_at")
    )
    executed_at = (
        raw_lifecycle.get("executionFinishedAt")
        or response_record.get("executed_at")
        or response_record.get("executedAt")
    )
    if action.status in {"waiting_human_approval", "pending_confirmation", "approved"}:
        executed_at = None
    elif not executed_at and action.status in {
        "executed", "verifying", "execution_unknown", "verified",
        "verification_failed", "verification_inconclusive", "rollback_pending",
        "rolled_back", "rollback_failed", "completed", "dry_run_executed", "failed",
    }:
        executed_at = action.updated_at.isoformat() if action.updated_at else None
    verified_at = raw_lifecycle.get("verifiedAt") or verification.get("verifiedAt") or (
        after_snapshot.get("verifiedAt") if isinstance(after_snapshot, dict) else None
    )
    duration = raw_lifecycle.get("durationSeconds")
    lifecycle = {
        **raw_lifecycle,
        "proposedAt": proposed_at,
        "approvedAt": approved_at,
        "executedAt": executed_at,
        "verifiedAt": verified_at,
        "executionStartedAt": raw_lifecycle.get("executionStartedAt"),
        "executionFinishedAt": executed_at,
        "durationSeconds": duration,
    }
    product_ids = (
        _top_query_action_product_ids(request_record or response_record)
        if action.action_type in {"query_demote", "product_blacklist"}
        else []
    )
    config_diff = None
    if action.action_type == "tag_bucket_config":
        decision = request_record.get("world_knowledge_decision")
        decision = decision if isinstance(decision, dict) else {}
        config_diff = {
            "configs": request_record.get("configs") or decision.get("config_plan") or [],
            "before": before_snapshot,
            "after": after_snapshot,
        }
        config_diff = sanitize_action_error_details(config_diff)
    config_verification = _top_query_action_evidence(after_snapshot, "configVerification")
    search_effect = _top_query_action_evidence(after_snapshot, "searchEffect")
    rollback_after_snapshot = sanitize_action_error_details(
        _safe_json_loads(rollback_action.after_snapshot)
        if rollback_action is not None
        else after_snapshot
    )
    rollback_verification = _top_query_action_evidence(
        rollback_after_snapshot,
        "rollbackVerification",
    )
    rollback_available = bool(
        action.action_type == "query_demote"
        and request_record.get("operation") != "restore"
        and isinstance(before_snapshot, dict)
        and before_snapshot.get("complete") is True
        and before_snapshot.get("rollbackAvailable") is True
        and isinstance(config_verification, dict)
        and config_verification.get("status") == "passed"
        and action.status in {
            "verifying", "verified", "verification_failed", "verification_inconclusive",
        }
    )
    rollback_status = str(rollback_action.status or "") if rollback_action else None
    if rollback_status in {"rollback_pending", "rolled_back"}:
        rollback_available = False
    outcome = _top_query_action_outcome(action, request_payload, response_payload, kg_plan)
    return {
        "id": action.id,
        "taskId": action.task_id,
        "checkKey": action.check_key,
        "toolKey": action.tool_key,
        "actionType": action.action_type,
        "executionMode": action.execution_mode,
        "status": action.status,
        "reason": action.reason,
        "productIds": product_ids,
        "request": request_payload,
        "response": response_payload,
        "before": before_snapshot,
        "after": after_snapshot,
        "beforeSnapshot": before_snapshot,
        "afterSnapshot": after_snapshot,
        "requestPayload": request_payload,
        "responsePayload": response_payload,
        "configDiff": config_diff,
        "kgPlan": kg_plan,
        "outcome": outcome,
        "statusLabel": outcome["statusLabel"],
        "statusTone": outcome["tone"],
        "configVerification": config_verification,
        "searchEffect": search_effect,
        "rollbackVerification": rollback_verification,
        "rollbackAvailable": rollback_available,
        "rollbackActionId": (
            int(rollback_action.id)
            if rollback_action is not None and rollback_action.id is not None
            else response_record.get("rollbackActionId")
        ),
        "rollbackStatus": rollback_status or (
            action.status if action.status in {
                "rollback_pending", "rolled_back", "rollback_failed",
            } else None
        ),
        "proposedAt": proposed_at,
        "approvedAt": approved_at,
        "executedAt": executed_at,
        "verifiedAt": verified_at,
        "lifecycle": lifecycle,
        "startedAt": proposed_at,
        "finishedAt": executed_at,
        "duration": duration,
    }


def _top_query_action_payloads(
    actions: List[TopQueryActionPlan],
) -> List[Dict[str, Any]]:
    restore_links = _top_query_action_restore_links(actions)
    return [
        _top_query_action_payload(
            action,
            rollback_action=restore_links.get(int(action.id)) if action.id is not None else None,
        )
        for action in actions
    ]

def _top_query_action_outcome(
    action: TopQueryActionPlan,
    request_payload: Any,
    response_payload: Any,
    kg_plan: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    action_label = {
        "kg_relation": "标签关系调整",
        "kg_tag": "标签调整",
        "query_demote": "Query 降桶",
        "product_blacklist": "商品拉黑",
        "tag_bucket_config": "标签权重调整",
    }.get(str(action.action_type), str(action.action_type or "治理动作"))
    status_label = {
        "waiting_human_approval": "待人工确认",
        "pending_confirmation": "待人工确认",
        "approved": "已确认",
        "executing": "执行中",
        "dry_run_executed": "已试运行",
        "executed": "已执行",
        "verifying": "已提交，配置验证中",
        "execution_unknown": "执行结果未知，请勿重复执行",
        "verified": "已验证",
        "verification_failed": "验证未通过",
        "verification_inconclusive": "验证未完成",
        "completed": "已完成",
        "failed": "执行失败",
        "rejected": "已驳回",
        "rollback_pending": "回滚结果待核查",
        "rolled_back": "已回滚并确认配置恢复",
        "rollback_failed": "回滚失败，需立即人工处理",
        "superseded": "已被新方案替代",
    }.get(str(action.status), str(action.status or "待处理"))
    tone = "neutral"
    if action.status in {"executed", "verified", "completed", "rolled_back"}:
        tone = "success"
    elif action.status == "dry_run_executed":
        tone = "warning"
    elif action.status in {"verifying", "execution_unknown", "verification_inconclusive", "rollback_pending"}:
        tone = "warning"
    elif action.status in {"failed", "verification_failed", "rejected", "rollback_failed"}:
        tone = "danger"
    elif action.status in {"waiting_human_approval", "pending_confirmation", "approved"}:
        tone = "warning"

    detail_lines: List[str] = []
    if kg_plan:
        for key in ("expectedImpact", "negativeImpact", "rollbackSuggestion"):
            value = kg_plan.get(key)
            if value:
                detail_lines.append(str(value))

    request_record = request_payload if isinstance(request_payload, dict) else {}
    response_record = response_payload if isinstance(response_payload, dict) else {}
    if action.action_type == "query_demote":
        product_ids = _top_query_action_product_ids(request_record or response_record)
        if product_ids:
            detail_lines.append(f"涉及商品 {len(product_ids)} 个")
    if action.action_type == "product_blacklist":
        product_ids = _top_query_action_product_ids(request_record or response_record)
        if product_ids:
            detail_lines.append(f"拉黑商品 {len(product_ids)} 个")
    if action.action_type == "tag_bucket_config":
        keyword = request_record.get("keyword") or request_record.get("query")
        configs = request_record.get("configs")
        if keyword:
            detail_lines.append(f"调整关键词：{keyword}")
        if isinstance(configs, list) and configs:
            detail_lines.append(f"标签权重配置 {len(configs)} 条")
    if response_record.get("error"):
        detail_lines.append(str(response_record.get("error")))
    if action.error_message:
        detail_lines.append(str(action.error_message))

    deduped_lines: List[str] = []
    for line in detail_lines:
        clean_line = line.strip()
        if clean_line and clean_line not in deduped_lines:
            deduped_lines.append(clean_line)
    separator = " " if action_label.startswith(("Query", "SKU", "ID")) else ""
    return {
        "summary": f"{status_label}{separator}{action_label}",
        "detailLines": deduped_lines[:4],
        "tone": tone,
        "statusLabel": status_label,
    }

def _top_query_action_product_ids(payload: Dict[str, Any]) -> List[str]:
    raw = (
        payload.get("product_ids")
        or payload.get("productIds")
        or payload.get("product_id")
        or payload.get("productId")
        or payload.get("affected_product_ids")
        or payload.get("sku")
        or payload.get("skus")
    )
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if raw:
        return [item.strip() for item in str(raw).replace(";", ",").split(",") if item.strip()]
    return []

def _top_query_kg_plan_payload(action: TopQueryActionPlan, request_payload: Any) -> Optional[Dict[str, Any]]:
    if action.action_type not in {"kg_tag", "kg_relation"} or not isinstance(request_payload, dict):
        return None
    nested_payload = request_payload.get("kg_action") or request_payload.get("action")
    record = nested_payload if isinstance(nested_payload, dict) else request_payload
    return {
        "actionType": record.get("actionType") or record.get("action_type"),
        "params": record.get("params") or {},
        "riskLevel": record.get("riskLevel") or record.get("risk_level") or request_payload.get("riskLevel") or request_payload.get("risk_level"),
        "evidence": record.get("evidence") or request_payload.get("evidence") or {},
        "expectedImpact": record.get("expectedImpact") or record.get("expected_impact") or request_payload.get("expectedImpact") or request_payload.get("expected_impact"),
        "negativeImpact": record.get("negativeImpact") or record.get("negative_impact") or request_payload.get("negativeImpact") or request_payload.get("negative_impact"),
        "rollbackSuggestion": record.get("rollbackSuggestion") or record.get("rollback_suggestion") or request_payload.get("rollbackSuggestion") or request_payload.get("rollback_suggestion"),
    }


def _value_from_candidates(record: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", []):
            return value
    return None


def _normalize_percent_value(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 1:
        return number * 100
    return number


def _format_percent_value(value: Any) -> Optional[str]:
    normalized = _normalize_percent_value(value)
    if normalized is None:
        return None
    return f"{normalized:.2f}%"


def _format_currency_value(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"¥{number:.2f}"


def _first_non_empty_value(*values: Any) -> Optional[str]:
    for value in values:
        if value in (None, ""):
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return None


def _top_query_governance_plan_step(index: int, title: str, description: str) -> Dict[str, Any]:
    return {"index": index, "title": title, "description": description}


def _top_query_governance_plan_evidence(label: str, value: Optional[str], tone: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    payload: Dict[str, Any] = {"label": label, "value": value}
    if tone:
        payload["tone"] = tone
    return payload


def _top_query_kg_step_description(action_type: str, params: Dict[str, Any]) -> Optional[str]:
    source = _first_non_empty_value(
        params.get("source"),
        params.get("sourceTag"),
        params.get("sourceTagName"),
        params.get("tagName"),
        params.get("name"),
        params.get("fromTag"),
    )
    target = _first_non_empty_value(
        params.get("target"),
        params.get("targetTag"),
        params.get("targetTagName"),
        params.get("responseTagName"),
        params.get("viewTag"),
        params.get("name"),
        params.get("toTag"),
    )
    relationship = _first_non_empty_value(
        params.get("relationship"),
        params.get("relationshipType"),
        params.get("relationType"),
        params.get("type"),
    )
    if action_type in {"add_relationship", "create_relationship"} and source and target:
        relation_text = f"，关系为“{relationship}”" if relationship else ""
        return f"建立“{source}”与“{target}”的关系{relation_text}。"
    if action_type in {"delete_relationship", "remove_relationship"} and source and target:
        relation_text = f"“{relationship}”" if relationship else "目标"
        return f"删除“{source}”与“{target}”之间的{relation_text}关系。"
    if action_type in {"update_relationship", "modify_relationship"} and source and target:
        relation_text = f"为“{relationship}”" if relationship else "到新的关系类型"
        return f"更新“{source}”与“{target}”的关系方向或类型，调整{relation_text}。"
    if action_type in {"add_tag", "create_tag"} and (target or source):
        return f"创建标签“{target or source}”。"
    if action_type in {"delete_tag", "remove_tag"} and (target or source):
        return f"删除标签“{target or source}”。"
    if action_type in {"update_tag", "modify_tag"} and (target or source):
        return f"更新标签“{target or source}”的属性或命名。"
    return None


def _top_query_governance_plan_from_kg_action(action: TopQueryActionPlan, request_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kg_payload = request_payload.get("kg_action") or request_payload.get("action") or request_payload
    if not isinstance(kg_payload, dict):
        return None
    params = kg_payload.get("params")
    if not isinstance(params, dict) or not params:
        return None
    kg_action_type = str(kg_payload.get("actionType") or kg_payload.get("action_type") or "")
    step_description = _top_query_kg_step_description(kg_action_type, params)
    if not step_description:
        return None
    evidence_items = [
        _top_query_governance_plan_evidence("当前 Query 意图", _first_non_empty_value(request_payload.get("query"), action.query_text)),
        _top_query_governance_plan_evidence("缺失标签", _first_non_empty_value(params.get("tagName"), params.get("targetTagName"))),
        _top_query_governance_plan_evidence("错误关系", _first_non_empty_value(params.get("relationship"), params.get("relationshipType"))),
        _top_query_governance_plan_evidence("世界知识命中因子", _first_non_empty_value(request_payload.get("reason"), action.reason)),
    ]
    evidence = [item for item in evidence_items if item]
    return {
        "id": str(action.id),
        "taskId": action.task_id,
        "actionId": action.id,
        "actionType": action.action_type,
        "title": "知识图谱调整方案",
        "objective": "修正标签或关系，提升相关性与世界知识命中。",
        "summary": action.reason,
        "steps": [
            _top_query_governance_plan_step(1, "执行知识图谱调整", step_description),
        ],
        "evidence": evidence,
        "impact": ["会影响该品牌下相关 Query 的召回或排序解释。"],
        "riskNotes": ["知识图谱写操作需人工确认后执行。"],
        "executionMode": action.execution_mode,
        "executable": action.status in {"waiting_human_approval", "pending_confirmation", "approved"},
        "status": action.status,
    }


def _top_query_snapshot_product_id(product: Dict[str, Any]) -> str:
    return str(
        product.get("productId")
        or product.get("product_id")
        or product.get("skuId")
        or product.get("sku_id")
        or product.get("spuId")
        or ""
    ).strip()


def _top_query_find_product_in_value(value: Any, product_id: str) -> Optional[Dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            found = _top_query_find_product_in_value(item, product_id)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None
    if _top_query_snapshot_product_id(value) == product_id:
        return value
    for key in (
        "products",
        "items",
        "results",
        "before",
        "after",
    ):
        if key in value:
            found = _top_query_find_product_in_value(value.get(key), product_id)
            if found:
                return found
    return None


def _top_query_snapshot_product_for_task(task_id: int, product_id: str) -> tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    db = SessionLocal()
    try:
        snapshots = db.query(TopQueryContextSnapshot).filter(
            TopQueryContextSnapshot.task_id == task_id,
        ).order_by(TopQueryContextSnapshot.id.desc()).limit(80).all()
        preferred_types = ("after_search", "before_search")
        snapshots.sort(key=lambda row: preferred_types.index(row.context_type) if row.context_type in preferred_types else len(preferred_types))
        for snapshot in snapshots:
            payload = _safe_json_loads(snapshot.payload, {})
            product = _top_query_find_product_in_value(payload, product_id)
            if product:
                captured_at = payload.get("capturedAt") if isinstance(payload, dict) else None
                return product, f"{snapshot.context_type}_snapshot", captured_at or (snapshot.created_at.isoformat() if snapshot.created_at else None)
        return None, None, None
    finally:
        db.close()


def _top_query_governance_plan_from_query_demote(
    action: TopQueryActionPlan,
    request_payload: Dict[str, Any],
    inventory_threshold_percent: float,
) -> Optional[Dict[str, Any]]:
    check_key = str(request_payload.get("check_key") or action.check_key or "")
    if check_key != "inventory":
        return None
    query = _first_non_empty_value(request_payload.get("query"), action.query_text)
    product_ids = _top_query_action_product_ids(request_payload)
    if not query or not product_ids:
        return None
    first_product_id = product_ids[0]
    product_name_map = request_payload.get("product_names") if isinstance(request_payload.get("product_names"), dict) else {}
    product_label = _first_non_empty_value(product_name_map.get(first_product_id), first_product_id) or first_product_id
    common_payload = {
        "id": str(action.id),
        "taskId": action.task_id,
        "actionId": action.id,
        "actionType": action.action_type,
        "summary": action.reason,
        "executionMode": action.execution_mode,
        "executable": action.status in {"waiting_human_approval", "pending_confirmation", "approved"},
        "status": action.status,
    }
    if should_block_legacy_query_demotion(
        env=get_env(),
        status=str(action.status or ""),
        payload=request_payload,
    ):
        common_payload.update({
            "blockedReason": LEGACY_QUERY_DEMOTION_BLOCK_REASON,
            "requiresRebuild": True,
            "executable": False,
        })
    broken_rate = _format_percent_value(_value_from_candidates(request_payload, "broken_rate_percent", "broken_size_rate"))
    no_stock_count = _first_non_empty_value(request_payload.get("no_stock_size_count"), request_payload.get("broken_size_count"))
    total_size_count = _first_non_empty_value(request_payload.get("total_size_count"), request_payload.get("total_sizes"))
    broken_size_text = None
    if no_stock_count and total_size_count:
        broken_size_text = f"{no_stock_count} / {total_size_count}"
    core_size_missing = request_payload.get("core_size_missing")
    if isinstance(core_size_missing, list):
        core_size_text = "、".join(str(item) for item in core_size_missing if str(item).strip())
    else:
        core_size_text = _first_non_empty_value(core_size_missing, request_payload.get("core_sizes_missing"))
    evidence = [
        _top_query_governance_plan_evidence("当前断码率", broken_rate, "danger" if broken_rate else None),
        _top_query_governance_plan_evidence("配置阈值", f"{inventory_threshold_percent:.2f}%"),
        _top_query_governance_plan_evidence("无库存尺码 / 总尺码", broken_size_text),
        _top_query_governance_plan_evidence("核心尺码缺失", core_size_text, "warning" if core_size_text else None),
    ]
    return {
        **common_payload,
        "title": "断码治理方案",
        "objective": "降低高断码商品对该 Query 转化的影响。",
        "steps": [
            _top_query_governance_plan_step(1, "执行 Query 降桶", f"将商品“{product_label}”在 Query「{query}」下执行降桶。"),
        ],
        "evidence": [item for item in evidence if item],
        "impact": ["仅影响该 Query 下排序，并进入恢复监测。"],
        "riskNotes": ["执行后需要继续观察恢复监测结果。"],
    }


def _top_query_governance_plan_payload(
    action: TopQueryActionPlan,
    inventory_threshold_percent: float,
) -> Optional[Dict[str, Any]]:
    if action.execution_mode != "human_approval":
        return None
    if action.status not in {"waiting_human_approval", "pending_confirmation", "approved"}:
        return None
    request_payload = _safe_json_loads(action.request_payload)
    if not isinstance(request_payload, dict):
        return None
    if action.action_type in {"kg_tag", "kg_relation"}:
        return _top_query_governance_plan_from_kg_action(action, request_payload)
    if action.action_type == "query_demote":
        return _top_query_governance_plan_from_query_demote(action, request_payload, inventory_threshold_percent)
    return None

def _safe_json_loads(value: str | None):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _top_query_event_payload(value: str | None) -> tuple[Any, Dict[str, Any] | None]:
    """Translate legacy event telemetry without inventing model usage."""

    payload = _safe_json_loads(value)
    if not isinstance(payload, dict):
        return payload, None

    normalized = dict(payload)
    normalized.pop("llmUsed", None)
    raw_metrics = normalized.pop("llmMetrics", None)
    if not isinstance(raw_metrics, dict):
        return normalized, None

    model_name = str(
        raw_metrics.get("modelName") or raw_metrics.get("model") or ""
    ).strip()
    if not model_name:
        return normalized, None

    fallback_used = bool(
        raw_metrics.get("fallbackUsed")
        or raw_metrics.get("usedFallbackModel")
    )
    raw_role = str(raw_metrics.get("modelRole") or "").strip().lower()
    model_role = raw_role if raw_role in {"primary", "fallback"} else (
        "fallback" if fallback_used else "primary"
    )
    metrics: Dict[str, Any] = {
        "modelName": model_name,
        "modelRole": model_role,
        "fallbackUsed": fallback_used,
    }
    token_usage = raw_metrics.get("tokenUsage")
    if token_usage is not None:
        try:
            metrics["tokenUsage"] = max(0, int(token_usage))
        except (TypeError, ValueError):
            pass
    duration_seconds = raw_metrics.get("durationSeconds")
    if duration_seconds is None:
        duration_seconds = raw_metrics.get("totalDurationSeconds")
    if duration_seconds is not None:
        try:
            metrics["durationSeconds"] = max(0.0, float(duration_seconds))
        except (TypeError, ValueError):
            pass
    error_code = raw_metrics.get("errorCode") or raw_metrics.get("errorType")
    if error_code:
        metrics["errorCode"] = str(error_code)
    return normalized, metrics

def _top_query_step_has_issue(step: TopQueryStepStatus) -> bool:
    return (step.status or step.display_status) not in {"pass", "normal"}

def _evidence_issue_types(step: TopQueryStepStatus) -> List[str]:
    evidence = _safe_json_loads(step.evidence)
    if not isinstance(evidence, dict):
        return []
    raw_types = evidence.get("issueTypes") or evidence.get("issue_types") or evidence.get("type") or evidence.get("reason")
    if isinstance(raw_types, list):
        return [str(item) for item in raw_types]
    if raw_types:
        return [str(raw_types)]
    return []

def _evidence_product_count(step: TopQueryStepStatus) -> int:
    evidence = _safe_json_loads(step.evidence)
    if not isinstance(evidence, dict):
        return 1 if _top_query_step_has_issue(step) else 0
    count = evidence.get("productCount") or evidence.get("product_count") or evidence.get("affectedProductCount")
    if isinstance(count, (int, float)):
        return int(count)
    for key in ("affectedProducts", "affected_products", "products", "items"):
        value = evidence.get(key)
        if isinstance(value, list):
            return len(value)
    return 1 if _top_query_step_has_issue(step) else 0

def _top_query_payload_step_status(payload: Dict[str, Any], check_key: str) -> str:
    for step in payload.get("stepStatuses") or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("checkKey") or "") != check_key:
            continue
        return str(step.get("displayStatus") or step.get("status") or "").lower()
    return ""


def _top_query_payload_dimension_status(payload: Dict[str, Any], check_key: str) -> str:
    return ""


def _top_query_payload_dimension_has_issue(payload: Dict[str, Any], check_key: str) -> bool:
    detail_status = _top_query_payload_dimension_status(payload, check_key)
    if detail_status:
        return detail_status not in {"healthy", "normal", "pass", "executed", "skipped", "unknown", "missing"}
    return _top_query_payload_step_status(payload, check_key) not in {
        "", "normal", "pass", "executed", "skipped", "unknown", "missing",
        "已执行", "未执行", "正常",
    }


def _top_query_payload_dimension_was_reordered(payload: Dict[str, Any]) -> bool:
    status = _top_query_payload_step_status(payload, "open_factor")
    return status in {
        "executed", "failed", "error", "fixed", "completed", "improved",
        "still_problematic", "check_failed", "已执行", "执行失败", "已改善",
    }


def _top_query_payload_has_unresolved_issue(payload: Dict[str, Any]) -> bool:
    for step in payload.get("stepStatuses") or []:
        if not isinstance(step, dict):
            continue
        status = str(step.get("displayStatus") or step.get("status") or "").lower()
        if status in {"", "normal", "pass"}:
            continue
        return True
    return False


def _top_query_payload_issue_types(payload: Dict[str, Any], check_key: str) -> List[str]:
    for step in payload.get("stepStatuses") or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("checkKey") or "") != check_key:
            continue
        evidence = step.get("evidence")
        if not isinstance(evidence, dict):
            return []
        raw_types = evidence.get("issueTypes") or evidence.get("issue_types") or evidence.get("type") or evidence.get("reason")
        if isinstance(raw_types, list):
            return [str(item) for item in raw_types]
        if raw_types:
            return [str(raw_types)]
        return []
    return []

def _top_query_planner_plan(event: Optional[TopQueryAgentEvent]) -> Dict[str, Any]:
    if event:
        payload = _safe_json_loads(event.output_payload)
        if isinstance(payload, dict):
            raw_strategy = payload.get("strategy") or payload.get("governanceStrategy")
            strategy = raw_strategy if isinstance(raw_strategy, dict) else {}
            stages = strategy.get("stages") if isinstance(strategy.get("stages"), list) else []
            strategy_tasks = [
                {
                    "executor": ",".join(str(key) for key in stage.get("executor_keys", stage.get("executorKeys", []))),
                    "stageKey": stage.get("stage_key") or stage.get("stageKey"),
                    "goal": stage.get("stage_goal") or stage.get("stageGoal") or stage.get("reason", ""),
                }
                for stage in stages
                if isinstance(stage, dict)
            ]
            stage_keys = [
                str(stage.get("stage_key") or stage.get("stageKey"))
                for stage in stages
                if isinstance(stage, dict) and (stage.get("stage_key") or stage.get("stageKey"))
            ]
            return {
                "governanceGoal": payload.get("governanceGoal") or payload.get("governance_goal") or strategy.get("objective"),
                "optimizationHypothesis": (
                    payload.get("optimizationHypothesis")
                    or payload.get("optimization_hypothesis")
                    or strategy.get("conversion_hypothesis")
                    or strategy.get("conversionHypothesis")
                    or (stages[0].get("reason") if stages and isinstance(stages[0], dict) else None)
                ),
                "executionStrategy": payload.get("executionStrategy") or payload.get("execution_strategy") or " -> ".join(stage_keys),
                "tasks": payload.get("tasks") or strategy_tasks,
                "successCriteria": (
                    payload.get("successCriteria")
                    or payload.get("success_criteria")
                    or strategy.get("success_criteria")
                    or strategy.get("successCriteria")
                ),
                "replanTriggers": (
                    payload.get("replanTriggers")
                    or payload.get("replan_triggers")
                    or strategy.get("replan_triggers")
                    or strategy.get("replanTriggers")
                ),
                "reviewReason": (
                    payload.get("reviewReason")
                    or payload.get("review_reason")
                    or strategy.get("strategy_reason")
                    or strategy.get("strategyReason")
                ),
                "riskLevel": strategy.get("risk_level") or strategy.get("riskLevel"),
            }
    return {
        "governanceGoal": None,
        "optimizationHypothesis": None,
        "executionStrategy": None,
        "tasks": [],
        "successCriteria": None,
        "replanTriggers": None,
        "reviewReason": None,
    }

def _get_top_query_task_or_404(task_id: int, user: User) -> TopQueryTask:
    db = SessionLocal()
    try:
        task = db.get(TopQueryTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Query Governance task not found")
        _ensure_top_query_brand_permission(task.brand, user)
        db.expunge(task)
        return task
    finally:
        db.close()

def _latest_top_query_data_date(
    db,
    brand: str,
    latest_metric: Optional[BrandDailyMetric] = None,
    query_source: str = "top_query",
):
    if query_source == "top_query" and latest_metric and latest_metric.data_date:
        return latest_metric.data_date
    return db.query(func.max(TopQueryTask.data_date)).filter(
        TopQueryTask.brand == brand,
        func.coalesce(TopQueryTask.query_source, "top_query") == query_source,
    ).scalar()

def _top_query_data_date_for_monitoring_date(monitoring_date: Optional[datetime.date]) -> Optional[datetime.date]:
    if not monitoring_date:
        return None
    return monitoring_date - datetime.timedelta(days=1)

def _latest_top_query_tasks(
    db,
    brand: str,
    latest_metric: Optional[BrandDailyMetric] = None,
    status_value: Optional[str] = None,
    data_date: Optional[datetime.date] = None,
    query_source: str = "top_query",
):
    latest_data_date = data_date or _latest_top_query_data_date(db, brand, latest_metric, query_source)
    query = db.query(TopQueryTask).filter(
        TopQueryTask.brand == brand,
        func.coalesce(TopQueryTask.query_source, "top_query") == query_source,
    )
    if latest_data_date:
        query = query.filter(TopQueryTask.data_date == latest_data_date)
    query = query.filter(TopQueryTask.status != "superseded")
    if status_value:
        query = query.filter(TopQueryTask.status == status_value)
    query = query.order_by(TopQueryTask.search_pv.desc(), TopQueryTask.id.asc())
    if query_source == "top_query" and latest_metric and latest_metric.top_query_count and latest_metric.top_query_count > 0:
        query = query.limit(latest_metric.top_query_count)
    return query.all()

def _brand_metric_raw_payload(metric: Optional[BrandDailyMetric]) -> Dict[str, Any]:
    if not metric:
        return {}
    try:
        raw_payload = json.loads(metric.raw_payload or "{}")
    except json.JSONDecodeError:
        raw_payload = {}
    return raw_payload if isinstance(raw_payload, dict) else {}

def _preferred_brand_metric(metrics: List[BrandDailyMetric]) -> Optional[BrandDailyMetric]:
    if not metrics:
        return None

    def metric_priority(metric: BrandDailyMetric) -> tuple[int, int, float, int]:
        raw_payload = _brand_metric_raw_payload(metric)
        return (
            1 if raw_payload.get("metric_source") == "user_behavior" else 0,
            int(metric.top_query_count or 0),
            float(metric.top_query_pv or 0),
            int(metric.id or 0),
        )

    return max(metrics, key=metric_priority)

def _query_brand_metric(db, brand: str, data_date: Optional[datetime.date] = None) -> Optional[BrandDailyMetric]:
    query = db.query(BrandDailyMetric).filter(BrandDailyMetric.brand == brand)
    if data_date:
        query = query.filter(BrandDailyMetric.data_date == data_date)
    candidates = query.order_by(BrandDailyMetric.data_date.desc(), BrandDailyMetric.id.desc()).limit(50).all()
    if not candidates:
        return None
    target_date = data_date or candidates[0].data_date
    same_date_candidates = [metric for metric in candidates if metric.data_date == target_date]
    return _preferred_brand_metric(same_date_candidates)


def _query_brand_metrics_by_brand(
    db,
    brands: List[str],
    data_date: Optional[datetime.date] = None,
) -> Dict[str, Optional[BrandDailyMetric]]:
    if not brands:
        return {}
    query = db.query(BrandDailyMetric).filter(BrandDailyMetric.brand.in_(brands))
    if data_date:
        query = query.filter(BrandDailyMetric.data_date == data_date)
    candidates = query.order_by(
        BrandDailyMetric.brand.asc(),
        BrandDailyMetric.data_date.desc(),
        BrandDailyMetric.id.desc(),
    ).all()
    grouped: Dict[str, Dict[datetime.date, List[BrandDailyMetric]]] = {}
    for metric in candidates:
        if not metric.data_date:
            continue
        grouped.setdefault(metric.brand, {}).setdefault(metric.data_date, []).append(metric)
    metrics_by_brand: Dict[str, Optional[BrandDailyMetric]] = {}
    for brand in brands:
        date_groups = grouped.get(brand) or {}
        if not date_groups:
            metrics_by_brand[brand] = None
            continue
        if data_date:
            metrics_by_brand[brand] = _preferred_brand_metric(date_groups.get(data_date) or [])
            continue
        latest_date = max(date_groups.keys())
        metrics_by_brand[brand] = _preferred_brand_metric(date_groups.get(latest_date) or [])
    return metrics_by_brand


def _latest_top_query_tasks_by_brand(
    db,
    brands: List[str],
    latest_metrics_by_brand: Dict[str, Optional[BrandDailyMetric]],
    *,
    data_date: Optional[datetime.date] = None,
    status_value: Optional[str] = None,
    query_source: str = "top_query",
) -> Dict[str, List[TopQueryTask]]:
    if not brands:
        return {}
    target_dates_by_brand = {
        brand: data_date or _latest_top_query_data_date(db, brand, latest_metrics_by_brand.get(brand), query_source)
        for brand in brands
    }
    relevant_dates = {value for value in target_dates_by_brand.values() if value is not None}
    query = db.query(TopQueryTask).filter(
        TopQueryTask.brand.in_(brands),
        func.coalesce(TopQueryTask.query_source, "top_query") == query_source,
        TopQueryTask.status != "superseded",
    )
    if relevant_dates:
        query = query.filter(TopQueryTask.data_date.in_(relevant_dates))
    if status_value:
        query = query.filter(TopQueryTask.status == status_value)
    rows = query.order_by(
        TopQueryTask.brand.asc(),
        TopQueryTask.search_pv.desc(),
        TopQueryTask.id.asc(),
    ).all()
    grouped: Dict[str, List[TopQueryTask]] = {brand: [] for brand in brands}
    for task in rows:
        target_date = target_dates_by_brand.get(task.brand)
        if target_date and task.data_date != target_date:
            continue
        grouped.setdefault(task.brand, []).append(task)
    for brand, tasks in grouped.items():
        metric = latest_metrics_by_brand.get(brand)
        if query_source == "top_query" and metric and metric.top_query_count and metric.top_query_count > 0:
            grouped[brand] = tasks[: int(metric.top_query_count)]
    return grouped

def _brand_metric_atc(metric: Optional[BrandDailyMetric]) -> float:
    raw_payload = _brand_metric_raw_payload(metric)
    return float(raw_payload.get("atc") or 0)

def _brand_metric_payload(metric: Optional[BrandDailyMetric]) -> Dict[str, Any]:
    if not metric:
        return {
            "brandUv": 0,
            "brandPv": 0,
            "clickUv": 0,
            "addToCartUv": 0,
            "purchaseUv": 0,
            "purchaseAmount": 0,
            "ctr": 0,
            "atc": 0,
            "cvr": 0,
            "metricSource": "none",
        }
    raw_payload = _brand_metric_raw_payload(metric)
    return {
        "brandUv": metric.brand_uv or 0,
        "brandPv": metric.brand_pv or 0,
        "clickUv": raw_payload.get("click_uv") or 0,
        "addToCartUv": raw_payload.get("add_to_cart_uv") or 0,
        "purchaseUv": raw_payload.get("purchase_uv") or 0,
        "purchaseAmount": raw_payload.get("purchase_amount") or 0,
        "ctr": metric.ctr or 0,
        "atc": raw_payload.get("atc") or 0,
        "cvr": metric.cvr or 0,
        "metricSource": raw_payload.get("metric_source") or "query_behavior_legacy",
    }

def _brand_metric_value(metric: BrandDailyMetric, key: str) -> float:
    raw_payload = _brand_metric_raw_payload(metric)
    if key == "brandUv":
        return float(metric.brand_uv or 0)
    if key == "brandPv":
        return float(metric.brand_pv or 0)
    if key == "ctr":
        return float(metric.ctr or 0)
    if key == "atc":
        return float(raw_payload.get("atc") or 0)
    if key == "cvr":
        return float(metric.cvr or 0)
    raw_key_map = {
        "clickUv": "click_uv",
        "addToCartUv": "add_to_cart_uv",
        "purchaseUv": "purchase_uv",
    }
    return float(raw_payload.get(raw_key_map[key]) or 0)

def _brand_metric_7d_payload(db, brand: str, metric: Optional[BrandDailyMetric]) -> Dict[str, Any]:
    metric_keys = {
        "BrandUv": "brandUv",
        "ClickUv": "clickUv",
        "AddToCartUv": "addToCartUv",
        "PurchaseUv": "purchaseUv",
        "Ctr": "ctr",
        "Atc": "atc",
        "Cvr": "cvr",
    }
    if not metric or not metric.data_date:
        payload: Dict[str, Any] = {}
        for suffix in metric_keys:
            payload[f"avg{suffix}7d"] = None
            payload[f"{suffix[0].lower()}{suffix[1:]}Delta7d"] = None
        return payload
    metrics = db.query(BrandDailyMetric).filter(
        BrandDailyMetric.brand == brand,
        BrandDailyMetric.data_date < metric.data_date,
        BrandDailyMetric.data_date >= metric.data_date - datetime.timedelta(days=7),
    ).order_by(BrandDailyMetric.data_date.desc(), BrandDailyMetric.id.desc()).limit(30).all()
    grouped_by_date: Dict[datetime.date, List[BrandDailyMetric]] = {}
    for item in metrics:
        if not item.data_date:
            continue
        grouped_by_date.setdefault(item.data_date, []).append(item)
        if len(grouped_by_date) >= 7:
            break
    history = [
        metric for metric in (_preferred_brand_metric(items) for items in grouped_by_date.values())
        if metric is not None
    ]

    def average(values: List[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    payload: Dict[str, Any] = {}
    for suffix, key in metric_keys.items():
        avg_value = average([_brand_metric_value(item, key) for item in history])
        current_value = _brand_metric_value(metric, key)
        delta_key = f"{suffix[0].lower()}{suffix[1:]}Delta7d"
        payload[f"avg{suffix}7d"] = avg_value
        payload[delta_key] = current_value - avg_value if avg_value is not None else None
    return payload


def _brand_metric_7d_payloads_by_brand(
    db,
    brands: List[str],
    metrics_by_brand: Dict[str, Optional[BrandDailyMetric]],
) -> Dict[str, Dict[str, Any]]:
    metric_keys = {
        "BrandUv": "brandUv",
        "ClickUv": "clickUv",
        "AddToCartUv": "addToCartUv",
        "PurchaseUv": "purchaseUv",
        "Ctr": "ctr",
        "Atc": "atc",
        "Cvr": "cvr",
    }
    empty_payload = {
        f"avg{suffix}7d": None for suffix in metric_keys
    } | {
        f"{suffix[0].lower()}{suffix[1:]}Delta7d": None for suffix in metric_keys
    }
    valid_metrics = {
        brand: metric for brand, metric in metrics_by_brand.items()
        if metric and metric.data_date
    }
    if not valid_metrics:
        return {brand: dict(empty_payload) for brand in brands}
    min_date = min(metric.data_date for metric in valid_metrics.values()) - datetime.timedelta(days=7)
    max_date = max(metric.data_date for metric in valid_metrics.values()) - datetime.timedelta(days=1)
    rows = db.query(BrandDailyMetric).filter(
        BrandDailyMetric.brand.in_(list(valid_metrics.keys())),
        BrandDailyMetric.data_date >= min_date,
        BrandDailyMetric.data_date <= max_date,
    ).order_by(
        BrandDailyMetric.brand.asc(),
        BrandDailyMetric.data_date.desc(),
        BrandDailyMetric.id.desc(),
    ).all()
    grouped: Dict[str, Dict[datetime.date, List[BrandDailyMetric]]] = {}
    for row in rows:
        if not row.data_date:
            continue
        target_metric = valid_metrics.get(row.brand)
        if not target_metric or not target_metric.data_date:
            continue
        if row.data_date >= target_metric.data_date:
            continue
        if row.data_date < target_metric.data_date - datetime.timedelta(days=7):
            continue
        grouped.setdefault(row.brand, {}).setdefault(row.data_date, []).append(row)

    def average(values: List[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    payloads: Dict[str, Dict[str, Any]] = {}
    for brand in brands:
        metric = valid_metrics.get(brand)
        if not metric:
            payloads[brand] = dict(empty_payload)
            continue
        history_groups = grouped.get(brand) or {}
        history = [
            item
            for item in (_preferred_brand_metric(items) for _, items in sorted(history_groups.items(), reverse=True))
            if item is not None
        ][:7]
        brand_payload: Dict[str, Any] = {}
        for suffix, key in metric_keys.items():
            avg_value = average([_brand_metric_value(item, key) for item in history])
            current_value = _brand_metric_value(metric, key)
            delta_key = f"{suffix[0].lower()}{suffix[1:]}Delta7d"
            brand_payload[f"avg{suffix}7d"] = avg_value
            brand_payload[delta_key] = current_value - avg_value if avg_value is not None else None
        payloads[brand] = brand_payload
    return payloads


def _safe_top_query_event_aggregates_by_task(
    db,
    task_ids: List[int],
    *,
    context: str,
) -> Dict[int, Dict[str, Any]]:
    if not task_ids:
        return {}
    try:
        rows = db.query(
            TopQueryAgentEvent.task_id,
            func.coalesce(func.sum(TopQueryAgentEvent.duration), 0),
            func.coalesce(func.sum(TopQueryAgentEvent.token_usage), 0),
            func.max(TopQueryAgentEvent.created_at),
        ).filter(
            TopQueryAgentEvent.task_id.in_(task_ids)
        ).group_by(TopQueryAgentEvent.task_id).all()
        return {
            int(task_id): {
                "duration": float(duration or 0),
                "token_usage": int(token_usage or 0),
                "latest_event_time": latest_event_time,
            }
            for task_id, duration, token_usage, latest_event_time in rows
        }
    except DatabaseError as exc:
        print(f"读取可选治理数据失败，尝试 sqlite 原始兜底: {context}: {exc}")
        aggregates: Dict[int, Dict[str, Any]] = {}
        for task_id in task_ids:
            duration = 0.0
            token_usage = 0
            latest_event_time = None
            for event in _raw_top_query_events_for_task(db, task_id):
                duration += float(event.duration or 0)
                token_usage += int(event.token_usage or 0)
                if event.created_at and (latest_event_time is None or event.created_at > latest_event_time):
                    latest_event_time = event.created_at
            aggregates[int(task_id)] = {
                "duration": duration,
                "token_usage": token_usage,
                "latest_event_time": latest_event_time,
            }
        return aggregates

def _top_query_traffic_coverage(tasks: List[TopQueryTask], metric: Optional[BrandDailyMetric]) -> float:
    if metric and metric.top_query_pv_share and metric.top_query_pv_share > 0:
        return float(metric.top_query_pv_share)
    if not metric or not metric.brand_pv:
        return 0
    return sum(task.search_pv or 0 for task in tasks) / metric.brand_pv

def _active_top_query_experiment_summary(db, brand: str) -> Optional[Dict[str, Any]]:
    experiment = db.query(TopQueryExperiment).filter(
        TopQueryExperiment.brand == brand,
        TopQueryExperiment.status.in_(["scheduled", "running"]),
    ).order_by(TopQueryExperiment.id.desc()).first()
    if not experiment:
        return None
    samples = db.query(TopQueryExperimentSample).filter(
        TopQueryExperimentSample.experiment_id == experiment.id
    ).all()
    treatment_count = sum(1 for sample in samples if sample.group_type == "treatment")
    control_count = sum(1 for sample in samples if sample.group_type == "control")
    return {
        "id": int(experiment.id),
        "experimentName": experiment.experiment_name,
        "status": experiment.status,
        "sampleDataDate": experiment.sample_data_date.isoformat() if experiment.sample_data_date else None,
        "plannedStartDate": experiment.planned_start_date.isoformat() if experiment.planned_start_date else None,
        "plannedEndDate": experiment.planned_end_date.isoformat() if experiment.planned_end_date else None,
        "treatmentQueryCount": treatment_count,
        "controlQueryCount": control_count,
    }


def _treatment_queries_for_active_experiment(
    db,
    active_experiment: Optional[Dict[str, Any]],
) -> Optional[set[str]]:
    if not active_experiment or active_experiment.get("status") != "running":
        return None
    experiment_id = active_experiment.get("id")
    if not experiment_id:
        return set()
    samples = db.query(TopQueryExperimentSample).filter(
        TopQueryExperimentSample.experiment_id == int(experiment_id),
        TopQueryExperimentSample.group_type == "treatment",
    ).all()
    return {
        str(sample.query_text or "").strip()
        for sample in samples
        if str(sample.query_text or "").strip()
    }


# --- 现有数据接口 (增加权限验证) ---

def _parse_dashboard_date(value: Optional[str]) -> datetime.date:
    if value:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.date.today()

def _task_work_date_from_source(value: Optional[datetime.datetime | datetime.date]) -> str:
    if not value:
        return ""
    if isinstance(value, datetime.datetime):
        source_date = value.date()
    else:
        source_date = value
    return (source_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

def _top_query_execution_date_payload(
    requested_date: Optional[datetime.date],
    latest_metric: Optional[BrandDailyMetric],
    fallback_data_date: Optional[datetime.date] = None,
) -> Optional[str]:
    if requested_date:
        return requested_date.isoformat()
    source_date = latest_metric.data_date if latest_metric and latest_metric.data_date else fallback_data_date
    return _task_work_date_from_source(source_date) if source_date else None

def _top_query_brand_metric_trend(db, brand: str, end_date: Optional[datetime.date] = None) -> List[Dict[str, Any]]:
    max_daily_points = 3660
    query = db.query(BrandDailyMetric).filter(
        BrandDailyMetric.brand == brand
    )
    if end_date:
        query = query.filter(BrandDailyMetric.data_date <= end_date)
    metrics = query.order_by(BrandDailyMetric.data_date.desc(), BrandDailyMetric.id.desc()).limit(max_daily_points * 3).all()
    grouped_by_date: Dict[datetime.date, List[BrandDailyMetric]] = {}
    for metric in metrics:
        if not metric.data_date:
            continue
        grouped_by_date.setdefault(metric.data_date, []).append(metric)
        if len(grouped_by_date) >= max_daily_points:
            break
    latest_by_date = [
        metric for metric in (_preferred_brand_metric(items) for items in grouped_by_date.values())
        if metric is not None
    ]
    return [
        {
            "date": metric.data_date.strftime("%m-%d") if metric.data_date else "",
            "uv": metric.brand_uv or 0,
            "pv": metric.brand_pv or 0,
            "clickUv": _brand_metric_raw_payload(metric).get("click_uv") or 0,
            "addToCartUv": _brand_metric_raw_payload(metric).get("add_to_cart_uv") or 0,
            "purchaseUv": _brand_metric_raw_payload(metric).get("purchase_uv") or 0,
            "ctr": float(metric.ctr or 0),
            "atc": _brand_metric_atc(metric),
            "cvr": float(metric.cvr or 0),
        }
        for metric in sorted(latest_by_date, key=lambda item: (item.data_date or datetime.date.min, item.id or 0))
    ]


def _top_query_brand_detail_context(
    db,
    brand: str,
    data_date: Optional[datetime.date],
    query_source: str = "top_query",
) -> Dict[str, Any]:
    active_experiment = _active_top_query_experiment_summary(db, brand) if query_source == "top_query" else None
    latest_metric = _query_brand_metric(db, brand, data_date)
    tasks = _latest_top_query_tasks(db, brand, latest_metric, data_date=data_date, query_source=query_source)
    treatment_queries = _treatment_queries_for_active_experiment(db, active_experiment)
    if treatment_queries is not None:
        tasks = [
            task for task in tasks
            if str(task.query_text or "").strip() in treatment_queries
        ]
    task_ids = [task.id for task in tasks]
    event_totals = {"duration": 0.0, "token_usage": 0}
    steps_by_task: Dict[int, List[TopQueryStepStatus]] = {}
    actions_by_task: Dict[int, List[TopQueryActionPlan]] = {}
    if task_ids:
        total_duration, total_tokens = _safe_top_query_event_totals(
            db, task_ids, context=f"brand-detail-events:{brand}:{data_date.isoformat() if data_date else 'latest'}"
        )
        event_totals = {"duration": total_duration, "token_usage": total_tokens}
        steps_by_task = _safe_latest_steps_by_task(db, task_ids, context=f"brand-detail:{brand}:{data_date.isoformat() if data_date else 'latest'}")
        actions_by_task = _safe_top_query_actions_by_task(
            db, task_ids, context=f"brand-detail-actions:{brand}:{data_date.isoformat() if data_date else 'latest'}"
        )
    query_payloads = _top_query_brand_queries_payload(tasks, steps_by_task, actions_by_task)
    return {
        "latest_metric": latest_metric,
        "tasks": tasks,
        "task_ids": task_ids,
        "event_totals": event_totals,
        "steps_by_task": steps_by_task,
        "actions_by_task": actions_by_task,
        "query_payloads": query_payloads,
        "metric_7d_payload": _brand_metric_7d_payload(db, brand, latest_metric),
        "active_experiment": active_experiment,
    }


def _top_query_brand_detail_stats_payload(
    db,
    brand: str,
    latest_metric: Optional[BrandDailyMetric],
    tasks: List[TopQueryTask],
    steps_by_task: Dict[int, List[TopQueryStepStatus]],
    actions_by_task: Dict[int, List[TopQueryActionPlan]],
    event_totals: Dict[str, Any],
    query_payloads: Optional[List[Dict[str, Any]]] = None,
    metric_7d_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    step_issue_counts = {key: 0 for key in ["recall", "relevance", "open_factor", "inventory"]}
    no_result_or_low_result_count = 0
    result_drop_count = 0
    inventory_product_issue_count = 0
    query_payloads = query_payloads or [
        _top_query_task_payload(task, steps_by_task.get(task.id, []), actions_by_task.get(task.id, []))
        for task in tasks
    ]
    for payload in query_payloads:
        if payload.get("invalidated") is True:
            continue
        recall_issue_types = set(_top_query_payload_issue_types(payload, "recall"))
        if recall_issue_types.intersection({"no_result", "low_result"}):
            no_result_or_low_result_count += 1
        if "result_count_drop" in recall_issue_types:
            result_drop_count += 1
        if _top_query_payload_dimension_was_reordered(payload):
            step_issue_counts["open_factor"] += 1
        for check_key in ["relevance", "inventory"]:
            if _top_query_payload_dimension_has_issue(payload, check_key):
                step_issue_counts[check_key] += 1
        if recall_issue_types or _top_query_payload_dimension_has_issue(payload, "recall"):
            step_issue_counts["recall"] += 1
        if _top_query_payload_dimension_has_issue(payload, "inventory"):
            inventory_product_issue_count += 1
    top_query_pv_share = _top_query_traffic_coverage(tasks, latest_metric)
    return {
        "topQueryCount": len(tasks),
        "manualRequiredCount": sum(
            1 for task in tasks
            if not _is_top_query_invalidated(task) and task.status == "manual_required"
        ),
        "completedCount": sum(1 for task in tasks if task.status == "completed"),
        "stepIssueCounts": step_issue_counts,
        "topQueryPvShare": top_query_pv_share or 0,
        "noResultOrLowResultCount": no_result_or_low_result_count,
        "resultDropCount": result_drop_count,
        "relevanceIssueCount": step_issue_counts["relevance"],
        "openFactorIssueCount": step_issue_counts["open_factor"],
        "inventoryProductIssueCount": inventory_product_issue_count,
        **_brand_metric_payload(latest_metric),
        **(metric_7d_payload or _brand_metric_7d_payload(db, brand, latest_metric)),
        "durationSeconds": event_totals["duration"],
        "tokenUsage": event_totals["token_usage"],
        "experimentMode": False,
    }


def _top_query_brand_queries_payload(
    tasks: List[TopQueryTask],
    steps_by_task: Dict[int, List[TopQueryStepStatus]],
    actions_by_task: Dict[int, List[TopQueryActionPlan]],
) -> List[Dict[str, Any]]:
    return [
        _top_query_task_payload(task, steps_by_task.get(task.id, []), actions_by_task.get(task.id, []))
        for task in tasks
    ]

def _query_status(query: DailyAbnormalQuery) -> str:
    if query.ops_effect == "failed":
        return "reviewing"
    if query.action_taken == "no_action_needed":
        return "closed"
    if query.is_processed == 1:
        return "monitoring"
    return "pending"

def _format_product_price(product: Dict[str, Any]) -> str:
    value = product.get("discountPrice")
    if value is None:
        value = product.get("price")
    if value is None:
        return "-"
    try:
        numeric = float(value)
        return f"¥{numeric:.0f}" if numeric.is_integer() else f"¥{numeric:.2f}"
    except (TypeError, ValueError):
        return str(value)

def _product_key(product: Dict[str, Any]) -> str:
    return str(product.get("skuId") or product.get("productId") or product.get("spuId") or "")

def _first_non_empty_str(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None

def _first_image_from_list(value: Any) -> Optional[str]:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, str):
            normalized = item.strip()
            if normalized:
                return normalized
            continue
        if isinstance(item, dict):
            nested = _first_non_empty_str(
                item.get("url"),
                item.get("imageUrl"),
                item.get("mainImage"),
                item.get("src"),
                item.get("path"),
            )
            if nested:
                return nested
    return None

def _extract_product_image(product: Dict[str, Any]) -> Optional[str]:
    return (
        _first_non_empty_str(
            product.get("mainImage"),
            product.get("main_image"),
            product.get("imageUrl"),
            product.get("image_url"),
            product.get("image"),
            product.get("cover"),
            product.get("coverUrl"),
            product.get("cover_image"),
            product.get("pic"),
            product.get("picture"),
            product.get("pictureUrl"),
            product.get("img"),
            product.get("imgUrl"),
            product.get("thumbnail"),
            product.get("thumbnailUrl"),
        )
        or _first_image_from_list(product.get("imageUrls"))
        or _first_image_from_list(product.get("images"))
        or _first_image_from_list((product.get("media") or {}).get("images") if isinstance(product.get("media"), dict) else None)
    )

def _get_latest_search_products(db, brand: str, query_text: str) -> List[Dict[str, Any]]:
    latest_log_id = db.query(OperationLog.id).filter(
        OperationLog.brand == brand,
        OperationLog.query_text == query_text,
        OperationLog.module == "OperationalService",
        OperationLog.action == "POST mixed",
        OperationLog.status == "success",
        OperationLog.response_payload.isnot(None),
    ).order_by(OperationLog.created_at.desc()).limit(1).scalar()
    if not latest_log_id:
        return []

    response_payload = db.query(OperationLog.response_payload).filter(
        OperationLog.id == latest_log_id
    ).scalar()
    if not response_payload:
        return []
    try:
        payload = json.loads(response_payload)
    except json.JSONDecodeError:
        return []
    rows = payload.get("data", {}).get("rows", [])
    return rows if isinstance(rows, list) else []

def _safe_json_loads(value: Optional[str], fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback

def _serialize_no_result_action(action: NoResultGovernanceAction) -> Dict[str, Any]:
    return {
        "id": action.id,
        "actionType": action.action_type,
        "targetInterface": action.target_interface,
        "requestPayload": _safe_json_loads(action.request_payload, {}),
        "responsePayload": _safe_json_loads(action.response_payload, {}),
        "beforeSnapshot": _safe_json_loads(action.before_snapshot, {}),
        "rollbackPayload": _safe_json_loads(action.rollback_payload, {}),
        "status": action.status,
        "rollbackStatus": action.rollback_status,
        "riskLevel": action.risk_level,
        "agentReason": action.agent_reason,
        "errorMessage": action.error_message,
        "createdAt": action.created_at.strftime("%Y-%m-%d %H:%M:%S") if action.created_at else "",
    }

def _serialize_no_result_agent_event(event: NoResultAgentEvent) -> Dict[str, Any]:
    return {
        "id": event.id,
        "stage": event.stage,
        "eventType": event.event_type,
        "toolName": event.tool_name,
        "summary": event.summary,
        "payload": _safe_json_loads(event.payload, {}),
        "createdAt": event.created_at.strftime("%Y-%m-%d %H:%M:%S") if event.created_at else "",
    }

def _load_no_result_agent_timeline(query_id: int) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        events = db.query(NoResultAgentEvent).filter(
            NoResultAgentEvent.query_id == query_id,
        ).order_by(NoResultAgentEvent.created_at.asc(), NoResultAgentEvent.id.asc()).all()
        return [_serialize_no_result_agent_event(event) for event in events]
    finally:
        db.close()

def _no_result_execution_status(row: NoResultQuery) -> str:
    actions = list(row.actions or [])
    pending_count = len([action for action in actions if action.status == "pending_execute"])
    failed_count = len([action for action in actions if action.status in {"failed", "manual_review"}])
    if row.status == "execution_paused":
        return "paused"
    if row.status == "execution_running":
        return "running"
    if actions and pending_count == 0 and failed_count == 0:
        return "completed"
    if failed_count > 0:
        return "blocked"
    return "idle"

def _serialize_no_result_query(row: NoResultQuery) -> Dict[str, Any]:
    actions = sorted(row.actions or [], key=lambda item: item.created_at or datetime.datetime.min)
    return {
        "id": row.id,
        "batchId": row.batch_id,
        "dataDate": row.data_date.strftime("%Y-%m-%d") if row.data_date else "",
        "brand": row.brand,
        "query": row.query_text,
        "status": row.status,
        "aiDecision": row.ai_decision,
        "aiReason": row.ai_reason,
        "finalStrategy": row.final_strategy,
        "beforeResultCount": int(row.before_result_count or 0),
        "afterResultCount": int(row.after_result_count or 0),
        "searchPv": int(row.search_pv or 0),
        "searchUv": row.search_uv,
        "executionStatus": _no_result_execution_status(row),
        "actions": [_serialize_no_result_action(action) for action in actions],
        "agentTimeline": _load_no_result_agent_timeline(int(row.id)),
        "createdAt": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
        "processedAt": row.processed_at.strftime("%Y-%m-%d %H:%M:%S") if row.processed_at else "",
    }

__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
