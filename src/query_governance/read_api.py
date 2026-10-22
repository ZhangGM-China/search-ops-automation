"""Read models for the deterministic Query governance API.

The service reads the rewritten ``query_governance_*`` tables plus the
retained inventory recovery records needed to show the independent broken-rate
monitor. It deliberately does not translate legacy Planner, Executor,
price, or health-score state into the new contract.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import load_only

from src.persistence.models import (
    ActionIntent,
    ActionLog,
    ActionTerminalFact,
    BrandDailyMetric,
    InventoryRecoveryTask,
    NodeModelConfigVersion,
    SchedulerJobRun,
)
from src.persistence.inventory_recovery_dual_write import read_inventory_recovery_checks, read_inventory_recovery_tasks
from src.config import get_brand_business_config, _raw_config

from .image_integrity import SUSPECT_IMAGE_WARNING_CODES
from .models import (
    GovernancePlanExecutorType,
    GovernancePlanStatus,
    NODE_ORDER,
    NodeStatus,
    QueryGovernanceAgentEvent,
    QueryGovernanceBatchSummary,
    QueryGovernanceCandidate,
    QueryGovernanceSelectionBatch,
    QueryGovernanceActionPlan,
    QueryGovernanceAsyncTask,
    QueryGovernanceContextSnapshot,
    QueryGovernanceExperiment,
    QueryGovernanceExperimentSample,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
    QueryMetricSnapshot,
)
from .visibility import CHECK_KEYS, check_modes_for_date, result_has_disabled_reason, visible_check_keys


_STATUS_VALUES = tuple(status.value for status in NodeStatus)

_L0_L2_RECALL_BUCKETS = frozenset({
    "top",
    "productIdsByAgent",
    "productIdsByCode",
    "productIdsByProductNameTag",
    "productIdsByCodeSpellCorrections",
    "productIdsByTagsAll",
    "productIdsByWhiteList",
    "productIdsBySubTag",
})
_GOVERNANCE_PENDING_STATUSES = frozenset({
    "pending", "manual_review", "execution_unknown", "rollback_failed",
})
_GOVERNANCE_MANUAL_MODES = frozenset({"human_approval"})
_GOVERNANCE_NON_AI_STATUSES = _GOVERNANCE_PENDING_STATUSES | frozenset({
    "executing", "rollback_pending", "rolled_back", "failed", "rejected",
})
_QUALITY_METRICS_VERSION = 6
_QUALITY_STEP_FAILURE_STATUSES = frozenset({
    "pending", "running", "error", "failed", "still_problematic", "check_failed",
})
_DEFAULT_METRIC_TREND_DAYS = 7
_MAX_METRIC_TREND_DAYS = 180


def quality_metrics_payload_marker() -> str:
    """LIKE pattern matching summary payloads written with the current quality metrics definition."""

    return f'%"qualityMetricsVersion": {_QUALITY_METRICS_VERSION}%'


def _selection_check_modes(selection_config: Mapping[str, Any] | None) -> Mapping[str, bool] | None:
    modes = selection_config.get("check_modes") if isinstance(selection_config, Mapping) else None
    return modes if isinstance(modes, Mapping) else None


def _check_policy(brand: str, data_date: date, selection_config: Mapping[str, Any] | None) -> Mapping[str, bool] | None:
    return _selection_check_modes(selection_config) or check_modes_for_date(
        _raw_config, brand, data_date, for_display=True,
    )


def _inventory_activity_queries(
    session: Any,
    records: Sequence[Any],
    data_date: date | None,
) -> set[str]:
    """Return Queries with independent inventory activity in the read scope.

    When ``records`` was loaded with ``updated_after=run.started_at``, the
    records are already bounded to the workflow's read scope.  Independent
    monitoring can run after midnight, so requiring the record date to equal
    the workflow date hides valid next-day checks.  A date filter is retained
    for callers that do not have a workflow run and therefore have no such
    lower-bound query.
    """
    checks = read_inventory_recovery_checks(session, [int(item.id) for item in records])
    active: set[str] = set()
    for item in records:
        timestamps = [getattr(item, "created_at", None), getattr(item, "updated_at", None)]
        timestamps.extend(check.get("checked_at") for check in checks.get(int(item.id), ()))
        if data_date is None or any(str(value or "")[:10] == data_date.isoformat() for value in timestamps):
            query = str(getattr(item, "original_query", "") or "").strip()
            if query:
                active.add(query)
    return active


def _completed_inventory_scan_at(
    session: Any,
    *,
    brand: str,
    run: Any | None,
    policy: Mapping[str, bool] | None,
    data_date: date | None = None,
) -> datetime | None:
    """Read a successful independent scan for this daily workflow window."""
    if not (policy or {}).get("inventory"):
        return None
    if run is not None and getattr(run, "started_at", None) is not None:
        scan = session.execute(
            select(SchedulerJobRun.terminal_at).where(
                SchedulerJobRun.job_id == f"inventory_monitor_job:{brand}",
                SchedulerJobRun.status == "succeeded",
                SchedulerJobRun.started_at >= run.started_at,
                SchedulerJobRun.started_at < run.started_at + timedelta(days=1),
                SchedulerJobRun.terminal_at.is_not(None),
            ).order_by(SchedulerJobRun.terminal_at.desc()).limit(1)
        ).scalar_one_or_none()
        if scan is not None:
            return scan
    if data_date is not None:
        day_start = datetime.combine(data_date, datetime.min.time())
        day_end = datetime.combine(data_date, datetime.max.time())
        scan = session.execute(
            select(SchedulerJobRun.terminal_at).where(
                SchedulerJobRun.job_id == f"inventory_monitor_job:{brand}",
                SchedulerJobRun.status == "succeeded",
                SchedulerJobRun.started_at >= day_start,
                SchedulerJobRun.started_at <= day_end,
                SchedulerJobRun.terminal_at.is_not(None),
            ).order_by(SchedulerJobRun.terminal_at.desc()).limit(1)
        ).scalar_one_or_none()
        return scan
    return None


def _observed_check_keys(steps: Sequence[Any], inventory_queries: set[str] | None = None) -> set[str]:
    observed: set[str] = set()
    for step in steps:
        key = "open_factor" if str(getattr(step, "node_key", "")) == "reorder" else str(getattr(step, "node_key", ""))
        if key not in CHECK_KEYS:
            continue
        result = _json_object(getattr(step, "result_json", None)) or {}
        if isinstance(result.get("result"), Mapping):
            result = result["result"]
        status = str(getattr(step, "status", "")).lower()
        if status == "pending":
            continue
        if status == "skipped" and (result_has_disabled_reason(result) or str(result.get("reason") or "") in {"s0_failed", "control_group_read_only"}):
            continue
        observed.add(key)
    if inventory_queries:
        observed.add("inventory")
    return observed


def _visible_checks(
    policy: Mapping[str, bool] | None,
    steps: Sequence[Any],
    inventory_queries: set[str] | None = None,
) -> set[str]:
    return visible_check_keys(
        policy,
        observed=_observed_check_keys(steps, inventory_queries),
    )
def _number(value: Any, default: int | float = 0) -> int | float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _format_percentage_point(value: float) -> str:
    """Format a percentage-point delta for a human-readable conclusion."""
    return f"{abs(value):.2f}".rstrip("0").rstrip(".")


def _reorder_strategy_status(
    reorder_node: Mapping[str, Any],
    *,
    requires_reorder: bool,
) -> str:
    if not requires_reorder:
        return "not_applicable"
    result = _node_result(reorder_node) or {}
    preference = (
        result.get("preference_description")
        or result.get("preferenceDescription")
        or result.get("preference")
        or result.get("strategy")
    )
    if isinstance(preference, str) and preference.strip():
        return "success"
    status = str(reorder_node.get("status") or "").strip().lower()
    if status in {"pending", "running"}:
        return "pending"
    return "failed"


def _reorder_execution_status(
    actions: Sequence[Any],
    *,
    requires_reorder: bool,
) -> str:
    if not requires_reorder:
        return "not_applicable"
    action = next(
        (
            item
            for item in actions
            if str(getattr(item, "action_type", "") or "") == "query_reorder"
        ),
        None,
    )
    if action is None:
        return "not_run"
    status = str(getattr(action, "status", "") or "").strip().lower()
    if status in {
        GovernancePlanStatus.PENDING.value,
        GovernancePlanStatus.EXECUTING.value,
    }:
        return "pending"
    if status == GovernancePlanStatus.VERIFIED.value:
        return "success"
    if status == GovernancePlanStatus.EXECUTION_UNKNOWN.value:
        return "inconclusive"
    return "failed"


_PLAN_STATUS_LABELS = {
    GovernancePlanStatus.PENDING.value: "待执行",
    GovernancePlanStatus.EXECUTING.value: "执行中",
    GovernancePlanStatus.EXECUTION_UNKNOWN.value: "结果待确认",
    GovernancePlanStatus.ROLLBACK_PENDING.value: "回滚中",
    GovernancePlanStatus.ROLLED_BACK.value: "已回滚",
    GovernancePlanStatus.ROLLBACK_FAILED.value: "回滚失败",
    GovernancePlanStatus.REJECTED.value: "已取消",
}


_REORDER_FAILURE_REASON_BY_CODE = {
    "ACTION_EXECUTION_FAILED": "排序服务执行失败，重排序未完成。",
    "ACTION_GATEWAY_FAILED": "排序服务调用异常，重排序未完成。",
    "ACTION_READBACK_FAILED": "排序已提交，但系统未能确认新排序是否已生效。",
    "ACTION_READBACK_INCONCLUSIVE": "排序结果暂时无法确认，请稍后检查搜索结果。",
    "ACTION_VERIFICATION_FAILED": "排序结果与预期不一致，系统已撤销本次排序。",
    "ACTION_RESULT_UNKNOWN": "排序结果未知，需要人工检查搜索结果。",
    "ACTION_RECOVERY_READBACK_FAILED": "中断后的排序结果无法确认，请人工检查搜索结果。",
    "ACTION_IDEMPOTENCY_CONFLICT": "检测到重复的排序请求，本次未重复执行。",
    "ACTION_IN_PROGRESS": "已有相同排序任务正在执行，请稍后查看结果。",
    "ACTION_AUTHORIZATION_FAILED": "当前账号无权执行重排序。",
    "ACTION_RESULT_INVALID": "排序服务返回了无法识别的结果。",
    "ACTION_ROLLBACK_FAILED": "重排序未确认生效，且自动撤销失败，需要人工处理。",
}


def _reorder_plan_reason(action: Any) -> str:
    """Return an operator-facing reason instead of internal readback jargon."""
    error_code = str(getattr(action, "error_code", None) or "").strip().upper()
    if error_code in _REORDER_FAILURE_REASON_BY_CODE:
        return _REORDER_FAILURE_REASON_BY_CODE[error_code]
    error_message = str(getattr(action, "error_message", None) or "").strip()
    if "权威回读" in error_message:
        return _REORDER_FAILURE_REASON_BY_CODE["ACTION_READBACK_FAILED"]
    if "与期望排序不一致" in error_message:
        return _REORDER_FAILURE_REASON_BY_CODE["ACTION_VERIFICATION_FAILED"]
    if error_message:
        return error_message
    return "CTR/CVR 指标达到重排序条件"


def _plan_status(status: Any, *, executor_type: Any = None) -> tuple[str, str]:
    normalized = str(status or "pending").strip().lower()
    try:
        GovernancePlanStatus(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported governance plan status: {normalized}") from exc
    normalized_executor = str(executor_type or "").strip().lower()
    if normalized == GovernancePlanStatus.VERIFIED.value:
        if normalized_executor == GovernancePlanExecutorType.AI_AUTO.value:
            return normalized, "AI 已执行"
        if normalized_executor == GovernancePlanExecutorType.HUMAN.value:
            return normalized, "手动已执行"
        raise ValueError("verified governance plan requires executor_type")
    if normalized == GovernancePlanStatus.FAILED.value:
        if normalized_executor == GovernancePlanExecutorType.AI_AUTO.value:
            return normalized, "AI 执行失败"
        if normalized_executor == GovernancePlanExecutorType.HUMAN.value:
            return normalized, "手动执行失败"
        raise ValueError("failed governance plan requires executor_type")
    return normalized, _PLAN_STATUS_LABELS[normalized]


def _numeric_action_id(value: Any) -> int | None:
    """Return an executable action id only when the value is numeric.

    Legacy blacklist facts use stable display ids such as ``fact-1``.  Those
    facts can be shown in the read model, but they are not action-plan ids and
    must not be sent to the execution endpoint.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _plan_products(request: Mapping[str, Any], response: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = request.get("products") or response.get("products") or request.get("expected_products") or []
    if not isinstance(raw, list):
        raw = []
    names = request.get("product_names") or request.get("productNames") or {}
    if not isinstance(names, Mapping):
        names = {}
    ids = (
        request.get("product_ids")
        or request.get("productIds")
        or request.get("expected_product_ids")
        or request.get("add_product_ids")
        or request.get("remove_product_ids")
        or []
    )
    reasons = request.get("reasons") or {}
    if not isinstance(reasons, Mapping):
        reasons = {}
    image_urls = (
        request.get("product_images")
        or request.get("productImages")
        or response.get("product_images")
        or response.get("productImages")
        or {}
    )
    if not isinstance(image_urls, Mapping):
        image_urls = {}
    image_warnings = _first_mapping(
        request.get("image_warnings"),
        request.get("imageWarnings"),
        response.get("image_warnings"),
        response.get("imageWarnings"),
    )
    image_warning_messages = _first_mapping(
        request.get("image_warning_messages"),
        request.get("imageWarningMessages"),
        response.get("image_warning_messages"),
        response.get("imageWarningMessages"),
    )
    if not raw and isinstance(ids, list):
        raw = [{"product_id": value, "product_name": names.get(str(value), str(value)), "reason": reasons.get(str(value))} for value in ids]
    snapshot_items = snapshot.get("products") if isinstance(snapshot, Mapping) else []
    by_id = {str(item.get("product_id") or item.get("productId")): item for item in snapshot_items if isinstance(item, Mapping)}
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        product_id = str(item.get("product_id") or item.get("productId") or item.get("skuId") or "").strip()
        merged = dict(by_id.get(product_id, {}))
        merged.update(dict(item))
        if product_id:
            merged["productId"] = product_id
        product_name = merged.get("productName") or merged.get("product_name") or merged.get("name")
        if product_name:
            merged["productName"] = product_name
        if merged.get("reason") is None and product_id:
            merged["reason"] = reasons.get(product_id)
        image_url = image_urls.get(product_id)
        if image_url:
            # 计划 product_images 是生成计划时经资产链路解析的图，比任务快照回带的
            # 历史图新；统一覆盖各图片字段，避免快照里的旧错图压过已修复的正确图。
            for key in ("imageUrl", "image_url", "mainImage", "main_image"):
                merged[key] = image_url
        warning = _first_present(image_warnings.get(product_id), merged.get("image_warning"))
        if warning and not merged.get("imageWarning"):
            merged["imageWarning"] = str(warning)
            message = _first_present(
                image_warning_messages.get(product_id), merged.get("image_warning_message")
            )
            if message:
                merged["imageWarningMessage"] = str(message)
        result.append(merged)
    return result


def _first_mapping(*values: Any) -> Mapping[str, Any]:
    for value in values:
        if isinstance(value, Mapping):
            return value
    return {}


def _image_warning_count(products: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for item in products if isinstance(item, Mapping) and item.get("imageWarning"))


def _apply_image_warning_guard(plan: dict[str, Any], products: Sequence[Mapping[str, Any]]) -> None:
    """Never let suspect images drive an automatic blacklist write.

    When a candidate's image is itself suspect (shared by many products, or
    mismatched against the catalog) the card keeps the finding visible but drops
    the automatic execution mode, so the operator has to confirm it by hand.
    """

    count = _image_warning_count(products)
    if not count:
        return
    plan["imageWarningCount"] = count
    suspect = [
        item
        for item in products
        if isinstance(item, Mapping) and str(item.get("imageWarning") or "").upper() in SUSPECT_IMAGE_WARNING_CODES
    ]
    if suspect and str(plan.get("executionMode") or "") == "auto":
        plan["executionMode"] = "human_approval"
        plan["cancelable"] = False
        plan["imageSuspectCount"] = len(suspect)


def _kg_reason(trigger_source: Any, evidence: Mapping[str, Any]) -> str:
    reason_code = str(evidence.get("reason") or evidence.get("reasonCode") or trigger_source or "").strip().lower()
    current = _first_present(evidence.get("current_result_count"), evidence.get("currentResultCount"), evidence.get("result_count"), evidence.get("resultCount"))
    previous = _first_present(evidence.get("previous_result_count"), evidence.get("previousResultCount"))
    if reason_code in {"low_result", "low_recall", "recall_low_result"}:
        if current is not None and previous is not None:
            return f"召回数量不足：当前 {current} 件，上一次 {previous} 件"
        if current is not None:
            return f"召回数量不足：当前 {current} 件"
        return "召回数量不足"
    if reason_code in {"result_count_drop", "recall_drop", "recall_count_drop"}:
        if current is not None and previous is not None:
            return f"召回数量下降：由 {previous} 件降至 {current} 件"
        return "召回数量较上一次下降"
    if reason_code in {"incorrect_recall", "wrong_recall", "relevance_issue", "relevance_blacklist_ratio"}:
        return "发现错误召回商品，需要调整 KG 标签或关系"
    explicit = evidence.get("description") or evidence.get("summary")
    return str(explicit or "召回数量或召回质量触发 KG 治理")


def _kg_evidence_items(evidence: Mapping[str, Any], plan: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    current = _first_present(evidence.get("current_result_count"), evidence.get("currentResultCount"), evidence.get("result_count"), evidence.get("resultCount"))
    previous = _first_present(evidence.get("previous_result_count"), evidence.get("previousResultCount"))
    if current is not None:
        items.append({"label": "当前召回数量", "value": f"{current} 件"})
    if previous is not None:
        items.append({"label": "上一次召回数量", "value": f"{previous} 件"})
    graph = plan.get("tool_evidence") if isinstance(plan, Mapping) else None
    searches = graph.get("searches") if isinstance(graph, Mapping) else None
    if isinstance(searches, Mapping):
        for query, result in searches.items():
            if not isinstance(result, Mapping) or result.get("error"):
                continue
            count = result.get("result_count")
            if count is not None:
                items.append({"label": f"搜索「{query}」结果", "value": f"{count} 件"})
    tags = graph.get("tags") if isinstance(graph, Mapping) else None
    if isinstance(tags, Mapping):
        valid_tag_count = sum(1 for value in tags.values() if isinstance(value, Mapping) and not value.get("error"))
        if valid_tag_count:
            items.append({"label": "KG 标签查询", "value": f"{valid_tag_count} 个标签"})
    relationships = graph.get("relationships") if isinstance(graph, Mapping) else None
    if isinstance(relationships, Mapping):
        valid_relation_count = sum(1 for value in relationships.values() if isinstance(value, Mapping) and not value.get("error"))
        if valid_relation_count:
            items.append({"label": "KG 关系查询", "value": f"{valid_relation_count} 个标签"})
    return items


def _kg_steps(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_steps = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(raw_steps, 1):
        if not isinstance(step, Mapping):
            continue
        params = step.get("params") if isinstance(step.get("params"), Mapping) else {}
        action_name = step.get("actionType") or step.get("type") or "执行 KG 调整"
        description = step.get("description") or step.get("summary") or step.get("reason")
        if not description:
            description = json.dumps(params, ensure_ascii=False) if params else str(action_name)
        steps.append({
            "index": index,
            "title": str(step.get("title") or action_name),
            "description": str(description),
            "actionType": str(action_name),
            "params": dict(params),
            "dependsOn": step.get("dependsOn") or step.get("depends_on") or [],
        })
    return steps


def _cause_groups_for_plan(
    value: Any,
    *,
    action_id: int | None,
    status: str,
    status_label: str,
    executor_type: Any = None,
) -> list[dict[str, Any]]:
    groups = value if isinstance(value, list) else []
    enriched: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        item = dict(group)
        root_cause = str(item.get("rootCause") or item.get("root_cause") or "uncertain")
        group_status = str(item.get("status") or status)
        group_label = str(item.get("statusLabel") or item.get("status_label") or status_label)
        group_id = str(item.get("groupId") or item.get("group_id") or "").strip() or None
        actions = item.get("actions") or item.get("steps") or []
        executable = bool(
            item.get("executable")
            if item.get("executable") is not None
            else root_cause == "kg_issue" and group_status == GovernancePlanStatus.PENDING.value and bool(actions)
        )
        if root_cause == "kg_issue" and (not actions or item.get("validationErrors")):
            executable = False
            group_label = "需人工复核"
        item.update(
            {
                "groupId": group_id,
                "status": group_status,
                "statusLabel": group_label,
                "executable": executable,
                "actionId": action_id,
                "actionIds": [action_id] if action_id is not None else [],
                "executorType": item.get("executorType") or item.get("executor_type") or executor_type,
            }
        )
        enriched.append(item)
    return enriched


def _demotion_metrics(request: Mapping[str, Any], products: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    first_product = products[0] if products else {}
    return {
        "fromRate": _first_present(
            request.get("executed_broken_rate"), request.get("executedBrokenRate"),
            request.get("broken_rate_percent"), request.get("brokenRatePercent"),
            first_product.get("executed_broken_rate"), first_product.get("executedBrokenRate"),
            first_product.get("broken_rate_percent"), first_product.get("brokenRatePercent"),
        ),
        "toRate": _first_present(
            request.get("restored_broken_rate"), request.get("restoredBrokenRate"),
            request.get("current_broken_rate"), request.get("currentBrokenRate"),
            first_product.get("restored_broken_rate"), first_product.get("restoredBrokenRate"),
            first_product.get("current_broken_rate"), first_product.get("currentBrokenRate"),
        ),
    }


def _with_blacklist_status(
    products: list[dict[str, Any]],
    status: str,
    product_statuses: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Fill the display status for legacy blacklist facts without overriding facts."""
    for product in products:
        product_id = str(product.get("productId") or product.get("product_id") or "").strip()
        projected_status = (product_statuses or {}).get(product_id)
        if projected_status:
            product["status"] = projected_status
        elif status == GovernancePlanStatus.VERIFIED.value and not product.get("status"):
            product["status"] = "blacklisted"
    return products


def _blacklist_product_statuses(session: Any, *, brand: str, query: str) -> dict[str, str]:
    """Project the latest local blacklist toggle for each product in this Query."""
    rows = session.execute(
        select(ActionLog).where(
            ActionLog.brand == brand,
            ActionLog.query_text == query,
            ActionLog.action_type.in_((1, 5)),
        ).order_by(ActionLog.created_at.asc(), ActionLog.id.asc())
    ).scalars().all()
    statuses: dict[str, str] = {}
    for row in rows:
        product_id = str(getattr(row, "product_id", None) or "").strip()
        if product_id:
            statuses[product_id] = "blacklisted" if int(row.action_type or 0) == 1 else "not_blacklisted"
    return statuses


def _has_irrelevant_product_pairs(payload: Mapping[str, Any]) -> bool:
    """True when the summary carries the new-format irrelevant pair list."""
    telemetry = payload.get("telemetry")
    return isinstance(telemetry, Mapping) and isinstance(telemetry.get("irrelevantProductPairs"), list)


def _irrelevant_product_pairs(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Read the deduplicated (query, product) pairs the model judged irrelevant."""
    telemetry = payload.get("telemetry")
    pairs_raw = telemetry.get("irrelevantProductPairs") if isinstance(telemetry, Mapping) else None
    if not isinstance(pairs_raw, list):
        return []
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in pairs_raw:
        if not isinstance(item, Mapping):
            continue
        query_text = str(item.get("query") or "").strip()
        product_id = str(item.get("productId") or item.get("product_id") or "").strip()
        if not query_text or not product_id:
            continue
        key = (query_text, product_id)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


_BLACKLIST_STATUS_QUERY_LIMIT = 900


def _pair_blacklist_status_map(
    session: Any,
    *,
    brand: str,
    pairs: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Project the current blacklist state for the given (query, product) pairs.

    Same rule as ``_blacklist_product_statuses``: the latest action_type 1/5
    row wins, so a later cancel (5) flips a pair to not blacklisted.  When the
    pair set spans too many distinct queries (trend windows), fall back to a
    brand-wide scan instead of an oversized IN clause.
    """
    if not pairs:
        return {}
    wanted = set(pairs)
    queries = sorted({query for query, _ in wanted})
    filters = [
        ActionLog.brand == brand,
        ActionLog.action_type.in_((1, 5)),
    ]
    if len(queries) <= _BLACKLIST_STATUS_QUERY_LIMIT:
        filters.append(ActionLog.query_text.in_(queries))
    rows = session.execute(
        select(ActionLog).where(*filters).order_by(ActionLog.created_at.asc(), ActionLog.id.asc())
    ).scalars().all()
    statuses: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (
            str(getattr(row, "query_text", None) or "").strip(),
            str(getattr(row, "product_id", None) or "").strip(),
        )
        if key in wanted:
            statuses[key] = "blacklisted" if int(row.action_type or 0) == 1 else "not_blacklisted"
    return statuses


def _inject_relevance_detection_accuracy(session: Any, brand: str, overview: dict[str, Any]) -> None:
    """Fill the read-time blacklist ratio into one overview payload.

    The materialized summary stores only the pair list; the numerator and the
    ratio are recomputed on every read so human cancels/approvals move the
    metric immediately.  Old-format payloads without a pair list keep ``None``
    (rendered as a gap) instead of a fabricated value.
    """
    telemetry = overview.get("telemetry")
    if not isinstance(telemetry, Mapping):
        return
    pairs = _irrelevant_product_pairs(overview)
    if not pairs and "irrelevantProductPairCount" not in telemetry:
        return
    statuses = _pair_blacklist_status_map(session, brand=brand, pairs=pairs)
    blacklisted = sum(1 for pair in pairs if statuses.get(pair) == "blacklisted")
    pair_count = _metric_trend_number(telemetry.get("irrelevantProductPairCount"))
    if pair_count is None:
        pair_count = len(pairs)
    telemetry["irrelevantProductPairCount"] = int(pair_count)
    telemetry["blacklistedProductCount"] = blacklisted
    telemetry["relevanceDetectionAccuracy"] = round(blacklisted / pair_count, 4) if pair_count else None
    # The raw pair list is internal state for read-time recomputation; the API
    # response only needs the counts.
    overview["telemetry"] = {key: value for key, value in telemetry.items() if key != "irrelevantProductPairs"}


def _coalesce_product_blacklist_actions(actions: Sequence[Any]) -> list[Any]:
    """Project one Query's blacklist rounds as one error-product plan.

    Relevance checks can execute in multiple rounds: after the first blacklist
    write, a follow-up Mixed read may still find another candidate. Those
    rounds are execution facts, not separate root-cause groups. Keep the raw
    action records in the API's ``actions`` history, while the governance-plan
    projection presents their products together.
    """
    blacklist_actions = [
        action
        for action in actions
        if str(getattr(action, "action_type", "") or "") == "product_blacklist"
    ]
    if len(blacklist_actions) <= 1:
        return list(actions)

    first = blacklist_actions[0]
    merged_request: dict[str, Any] = {}
    product_ids: list[str] = []
    product_names: dict[str, Any] = {}
    product_images: dict[str, Any] = {}
    reasons: dict[str, Any] = {}
    products: dict[str, dict[str, Any]] = {}
    product_statuses: dict[str, str] = {}
    statuses: list[str] = []

    for action in blacklist_actions:
        request = _json_object(getattr(action, "request_json", None)) or {}
        if not merged_request:
            merged_request = dict(request)
        status = str(getattr(action, "status", "") or "").strip().lower()
        if status:
            statuses.append(status)

        raw_ids = request.get("product_ids") or request.get("productIds") or []
        if isinstance(raw_ids, (str, bytes)):
            raw_ids = [raw_ids]
        names = request.get("product_names") or request.get("productNames") or {}
        images = request.get("product_images") or request.get("productImages") or {}
        request_reasons = request.get("reasons") or {}
        for product_id in raw_ids if isinstance(raw_ids, list) else []:
            normalized_id = str(product_id).strip()
            if not normalized_id:
                continue
            if normalized_id not in product_ids:
                product_ids.append(normalized_id)
            product_statuses[normalized_id] = (
                "blacklisted" if status == GovernancePlanStatus.VERIFIED.value else "not_blacklisted"
            )
            if isinstance(names, Mapping) and names.get(normalized_id) is not None:
                product_names[normalized_id] = names[normalized_id]
            if isinstance(images, Mapping) and images.get(normalized_id) is not None:
                product_images[normalized_id] = images[normalized_id]
            if isinstance(request_reasons, Mapping) and request_reasons.get(normalized_id) is not None:
                reasons[normalized_id] = request_reasons[normalized_id]

        raw_products = request.get("products") or []
        if isinstance(raw_products, list):
            for product in raw_products:
                if not isinstance(product, Mapping):
                    continue
                product_id = str(product.get("product_id") or product.get("productId") or "").strip()
                if product_id:
                    if product_id not in product_ids:
                        product_ids.append(product_id)
                    products[product_id] = {**products.get(product_id, {}), **product}
                    product_statuses[product_id] = (
                        "blacklisted" if status == GovernancePlanStatus.VERIFIED.value else "not_blacklisted"
                    )

    merged_request["product_ids"] = product_ids
    if product_names:
        merged_request["product_names"] = product_names
    if product_images:
        merged_request["product_images"] = product_images
    if reasons:
        merged_request["reasons"] = reasons
    merged_request["products"] = [
        {
            **products.get(product_id, {}),
            "product_id": product_id,
            "product_name": product_names.get(product_id) or products.get(product_id, {}).get("product_name"),
            "reason": reasons.get(product_id) or products.get(product_id, {}).get("reason"),
            "status": product_statuses[product_id],
        }
        for product_id in product_ids
    ]

    merged_status = statuses[0] if statuses and all(item == statuses[0] for item in statuses) else GovernancePlanStatus.EXECUTION_UNKNOWN.value
    merged = SimpleNamespace(
        id=getattr(first, "id", None),
        action_type="product_blacklist",
        status=merged_status,
        executor_type=getattr(first, "executor_type", None),
        executor_user_id=getattr(first, "executor_user_id", None),
        executed_at=getattr(first, "executed_at", None),
        failure_stage=getattr(first, "failure_stage", None),
        error_message=None,
        request_json=json.dumps(merged_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        response_json=getattr(first, "response_json", None),
        action_ids=[_numeric_action_id(getattr(action, "id", None)) for action in blacklist_actions],
    )
    return [
        action
        for action in actions
        if str(getattr(action, "action_type", "") or "") != "product_blacklist"
    ] + [merged]


def _relevance_plan_source(
    actions: Sequence[Any], async_tasks: Sequence[Any], task_id: int | None = None,
) -> tuple[Any | None, Any | None]:
    def in_task(item: Any) -> bool:
        return task_id is None or getattr(item, "task_id", task_id) == task_id

    relevance_actions = [
        item for item in actions if in_task(item)
        and str(getattr(item, "action_type", "")) == "relevance_plan"
    ]
    relevance_tasks = [
        item for item in async_tasks if in_task(item)
        and str(getattr(item, "trigger_source", "")) == "relevance_root_cause"
    ]
    latest_task = max(relevance_tasks, key=lambda item: int(getattr(item, "id", 0) or 0), default=None)
    if latest_task is not None:
        key = str(getattr(latest_task, "idempotency_key", "") or "")
        matching = [item for item in relevance_actions if key and getattr(item, "idempotency_key", None) == f"{key}:plan"]
        # 旧记录没有关联键时沿用已持久化方案；有键时只读取同一轮结果。
        if not matching and not key:
            matching = relevance_actions
        return max(matching, key=lambda item: int(getattr(item, "id", 0) or 0), default=None), latest_task
    return max(relevance_actions, key=lambda item: int(getattr(item, "id", 0) or 0), default=None), None


def _governance_plans(
    task: Any,
    nodes: Sequence[Mapping[str, Any]],
    actions: Sequence[Any],
    async_tasks: Sequence[Any],
    inventory_records: Sequence[Any] = (),
    inventory_checks: Mapping[int, Sequence[Mapping[str, Any]]] | None = None,
    blacklist_product_statuses: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    snapshot = _json_object(getattr(task, "s0_snapshot", None)) or {}
    plans: list[dict[str, Any]] = []
    relevance_action, relevance_task = _relevance_plan_source(actions, async_tasks, int(task.id))
    actions = [item for item in actions if str(getattr(item, "action_type", "")) != "relevance_plan" or item is relevance_action]
    async_tasks = [item for item in async_tasks if str(getattr(item, "trigger_source", "")) != "relevance_root_cause" or item is relevance_task]
    executable_statuses = {GovernancePlanStatus.PENDING.value}
    for action in _coalesce_product_blacklist_actions(actions):
        executor_type = getattr(action, "executor_type", None)
        status, status_label = _plan_status(
            getattr(action, "status", None), executor_type=executor_type
        )
        request = _json_object(getattr(action, "request_json", None)) or {}
        response = _json_object(getattr(action, "response_json", None)) or {}
        action_type = str(getattr(action, "action_type", "") or "")
        raw_action_id = getattr(action, "id", None)
        action_id = _numeric_action_id(raw_action_id)
        action_ids = getattr(action, "action_ids", None) or ([action_id] if action_id is not None else [])
        source = str(request.get("source") or "")
        default_execution_mode = "auto" if action_type in {"query_reorder", "product_blacklist"} or source == "inventory_monitor" else "human_approval"
        execution_mode = str(request.get("execution_mode") or request.get("executionMode") or default_execution_mode)
        base = {
            "id": f"{action_type}-{raw_action_id or 'unknown'}", "taskId": int(task.id), "actionId": action_id,
            "actionType": action_type, "objective": "",
            "actionIds": [item for item in action_ids if item is not None], "status": status, "statusLabel": status_label,
            "executionMode": execution_mode, "executable": action_id is not None and status in executable_statuses,
            "executorType": executor_type,
            "executorUserId": getattr(action, "executor_user_id", None),
            "executedAt": _iso(getattr(action, "executed_at", None)),
            "failureStage": getattr(action, "failure_stage", None),
            "cancelable": action_type in {"product_blacklist", "query_demote"} and source != "historical_action_intent" and execution_mode == "auto" and status == GovernancePlanStatus.VERIFIED.value,
            "evidence": [], "steps": [], "products": [], "metrics": {},
        }
        if action_type == "query_reorder":
            strategy = request.get("preference_description") or request.get("preferenceDescription") or request.get("preference") or response.get("preference_description")
            base.update({"type": "reorder", "title": "AI 重排序策略", "summary": str(strategy or "AI 生成排序策略"), "reason": _reorder_plan_reason(action), "products": _plan_products(request, response, snapshot)})
            plans.append(base)
        elif action_type == "product_blacklist":
            products = _with_blacklist_status(_plan_products(request, response, snapshot), status, blacklist_product_statuses)
            base.update({"type": "blacklist", "title": "错误商品", "summary": f"共 {len(products)} 个错误商品", "reason": str(getattr(action, "error_message", None) or request.get("reason") or "相关性检测判定商品不相关"), "products": products})
            _apply_image_warning_guard(base, products)
            plans.append(base)
        elif action_type == "query_demote":
            products = _plan_products(request, response, snapshot)
            operation = str(request.get("operation") or "").lower()
            restoring = operation == "restore" or bool(request.get("remove_product_ids"))
            metrics = _demotion_metrics(request, products)
            if not restoring:
                metrics.pop("toRate", None)
            base.update({"type": "demotion_restore" if restoring else "demotion", "title": "恢复降桶商品" if restoring else "降桶商品", "summary": f"共 {len(products)} 个商品", "reason": str(getattr(action, "error_message", None) or request.get("reason") or ("断码率恢复至阈值以下" if restoring else "断码率超过治理阈值")), "products": products, "metrics": metrics})
            plans.append(base)
        elif action_type in {"whitelist_tag", "query_whitelist"}:
            products = _plan_products(request, response, snapshot)
            tag = str(request.get("tag") or "").strip()
            if action_type == "whitelist_tag":
                plan_title = f"白名单标签「{tag}」补召回" if tag else "白名单标签补召回"
                plan_reason = str(getattr(action, "error_message", None) or request.get("reason") or "AI 召回率检测判定该商品应召回，拟补打白名单标签")
            else:
                plan_title = "Query 白名单补召回"
                plan_reason = str(getattr(action, "error_message", None) or request.get("reason") or "AI 召回率检测判定该商品应召回，拟加入 Query 白名单")
            base.update({"type": action_type, "title": plan_title, "summary": f"共 {len(products)} 个商品", "reason": plan_reason, "products": products})
            plans.append(base)
        elif action_type in {"kg_plan", "kg_tag", "kg_relation", "relevance_plan"}:
            kg_plan = response.get("plan") if isinstance(response.get("plan"), Mapping) else response
            decision = str(kg_plan.get("decision") or ("adjust" if kg_plan.get("actions") else "manual_review"))
            evidence_payload = request.get("evidence") if isinstance(request.get("evidence"), Mapping) else {}
            products = _plan_products(evidence_payload, {}, snapshot)
            reason = _kg_reason(request.get("trigger_source"), evidence_payload)
            evidence = _kg_evidence_items(evidence_payload, kg_plan)
            cause_groups = _cause_groups_for_plan(
                kg_plan.get("causeGroups") or kg_plan.get("cause_groups") or [],
                action_id=action_id,
                status=status,
                status_label=status_label,
                executor_type=executor_type,
            )
            plan_title = "相关性治理 Agent 调整方案" if action_type == "relevance_plan" else "召回数量治理策略"
            if status == GovernancePlanStatus.FAILED.value:
                base.update({"type": "kg", "title": plan_title, "summary": "生成失败", "reason": "生成失败", "statusLabel": "生成失败" if getattr(action, "failure_stage", None) == "generation" else status_label, "failureStage": None, "steps": [], "evidence": [], "products": [], "decision": None, "causeGroups": []})
            else:
                base.update({"type": "kg", "title": plan_title, "summary": str(kg_plan.get("reason") or kg_plan.get("summary") or "根据问题生成治理调整方案"), "reason": str(reason), "steps": _kg_steps(kg_plan), "evidence": evidence, "products": products, "decision": decision, "causeGroups": cause_groups})
            if status == GovernancePlanStatus.FAILED.value:
                base["executable"] = False
            elif decision == "no_change":
                base.update({"statusLabel": "无需调整", "executable": False})
            elif decision == "manual_review":
                base.update({"statusLabel": "需人工复核", "executable": False})
            else:
                base["executable"] = bool(base["executable"] and base["steps"])
            plans.append(base)
    represented_sources = {
        str(_json_object(getattr(action, "request_json", None)).get("trigger_source") or "")
        for action in actions
        if str(getattr(action, "action_type", "")) in {"kg_plan", "relevance_plan"}
    }
    for async_task in async_tasks:
        if str(getattr(async_task, "trigger_source", "")) in represented_sources:
            continue
        async_status = str(getattr(async_task, "status", None) or "pending").lower()
        # A pending record without a plan is useful as a generation
        # placeholder only when the Query has no other persisted plan.
        # Once another plan is present, omit the empty placeholder to keep
        # the workbench stable until KG evidence is available.
        if async_status == "pending" and plans:
            continue
        plan = _json_object(getattr(async_task, "plan_json", None))
        evidence_payload = _json_object(getattr(async_task, "evidence_json", None)) or {}
        decision = str(plan.get("decision") or "") if isinstance(plan, Mapping) else ""
        if async_status == "error":
            status, status_label = GovernancePlanStatus.FAILED.value, "生成失败"
            summary = "生成失败"
        elif not isinstance(plan, Mapping):
            status = GovernancePlanStatus.EXECUTING.value if async_status == "processing" else GovernancePlanStatus.PENDING.value
            status_label = "生成中"
            summary = "KG Agent 正在查询知识图谱与商品召回数据"
        else:
            status = GovernancePlanStatus.PENDING.value
            status_label = "无需调整" if decision == "no_change" else "需人工复核" if decision == "manual_review" else "待执行"
            summary = str(plan.get("reason") or plan.get("summary") or "根据召回问题生成 KG 调整方案")
        plan_payload = plan if isinstance(plan, Mapping) and async_status != "error" else {}
        if async_status == "error":
            evidence_payload = {}
        cause_groups = _cause_groups_for_plan(
            plan_payload.get("causeGroups") or plan_payload.get("cause_groups") or [],
            action_id=None,
            status=status,
            status_label=status_label,
        )
        is_relevance = getattr(async_task, "trigger_source", None) == "relevance_root_cause"
        plans.append({"id": f"kg-async-{async_task.id}", "taskId": int(task.id), "actionId": None, "actionIds": [], "actionType": "relevance_plan" if is_relevance else "kg_plan", "objective": "", "type": "kg", "title": "相关性治理 Agent 调整方案" if is_relevance else "召回数量治理策略", "summary": summary, "reason": "生成失败" if async_status == "error" else _kg_reason(getattr(async_task, "trigger_source", None), evidence_payload), "status": status, "statusLabel": status_label, "executionMode": "human_approval", "executorType": None, "executorUserId": None, "executedAt": None, "failureStage": None, "executable": False, "cancelable": False, "decision": decision or None, "evidence": _kg_evidence_items(evidence_payload, plan_payload), "steps": _kg_steps(plan_payload), "products": _plan_products(evidence_payload, {}, snapshot), "causeGroups": cause_groups, "metrics": {}})
    relevance = _node_result(next((node for node in nodes if node.get("nodeKey") == "relevance"), {})) or {}
    candidates = relevance.get("blacklist_candidates") or relevance.get("blacklistCandidates") or []
    if isinstance(candidates, list) and candidates:
        execution_mode = str(
            relevance.get("blacklist_execution_mode")
            or relevance.get("blacklistExecutionMode")
            or "disabled"
        )
        products = _plan_products({"products": candidates}, {}, snapshot)
        represented_ids = {product["productId"] for plan in plans if plan["type"] == "blacklist" for product in plan["products"]}
        products = [product for product in products if product["productId"] not in represented_ids]
        if products:
            candidate_plan = {
                "id": f"product-blacklist-candidates-{task.id}",
                "taskId": int(task.id),
                "actionId": None,
                "actionIds": [],
                "actionType": "product_blacklist",
                "objective": "",
                "type": "blacklist",
                "title": "错误商品",
                "summary": f"共 {len(products)} 个错误商品",
                "reason": "相关性检测判定商品不相关",
                "status": GovernancePlanStatus.PENDING.value,
                "statusLabel": "待执行",
                "executionMode": execution_mode,
                "executorType": None,
                "executorUserId": None,
                "executedAt": None,
                "failureStage": None,
                "executable": False,
                "cancelable": False,
                "evidence": [],
                "steps": [],
                "products": products,
                "metrics": {},
                "causeGroups": [],
            }
            _apply_image_warning_guard(candidate_plan, products)
            plans.append(candidate_plan)
    inventory_by_type: dict[str, list[Any]] = {"demotion": [], "demotion_restore": []}
    existing_inventory_products = {
        str(product.get("productId") or product.get("product_id") or "")
        for plan in plans
        if plan.get("type") in inventory_by_type
        for product in plan.get("products", [])
        if isinstance(product, Mapping)
    }
    for record in inventory_records:
        product_id = str(getattr(record, "product_id", None) or "").strip()
        # Only records carrying an action request are governance-plan facts.
        # Plain monitoring rows remain in the inventory section and should not
        # be mistaken for an executable plan.
        if not product_id or not getattr(record, "last_action_request_id", None) or product_id in existing_inventory_products:
            continue
        record_type = "demotion_restore" if str(getattr(record, "status", "")).lower() == "recovered" else "demotion"
        inventory_by_type[record_type].append(record)
    for plan_type, records in inventory_by_type.items():
        if not records:
            continue
        restoring = plan_type == "demotion_restore"
        products = []
        for record in records:
            checks = list((inventory_checks or {}).get(int(record.id), ()))
            monitoring_rates = [item.get("broken_rate_percent") for item in checks if str(item.get("status") or "") == "monitoring" and item.get("broken_rate_percent") is not None]
            restored_rate = record.last_check_stock
            product = {
                "productId": str(record.product_id),
                "productName": record.product_name,
                "brokenRatePercent": restored_rate,
            }
            if restoring:
                product["executedBrokenRate"] = monitoring_rates[-1] if monitoring_rates else None
                product["restoredBrokenRate"] = restored_rate
            else:
                product["executedBrokenRate"] = restored_rate
            products.append(product)
        from_rates = [product.get("executedBrokenRate") for product in products if product.get("executedBrokenRate") is not None]
        to_rates = [product.get("restoredBrokenRate") for product in products if product.get("restoredBrokenRate") is not None]
        plans.append({
            "id": f"inventory-{plan_type}-{task.id}",
            "taskId": int(task.id),
            "actionId": None,
            "actionIds": [],
            "actionType": "query_demote",
            "objective": "",
            "type": plan_type,
            "title": "恢复降桶商品" if restoring else "降桶商品",
            "summary": f"共 {len(products)} 个商品",
            "reason": "断码率已恢复至阈值以下" if restoring else "断码率超过治理阈值",
            "status": GovernancePlanStatus.VERIFIED.value,
            "statusLabel": "AI 已执行",
            "executionMode": "auto",
            "executorType": GovernancePlanExecutorType.AI_AUTO.value,
            "executorUserId": None,
            "executedAt": _iso(getattr(record, "updated_at", None)),
            "failureStage": None,
            "executable": False,
            "cancelable": not restoring,
            "evidence": [],
            "steps": [],
            "products": products,
            "metrics": (
                {"fromRate": max(from_rates) if from_rates else None, "toRate": max(to_rates) if to_rates else None}
                if restoring else {"fromRate": max(from_rates) if from_rates else None}
            ),
        })
    for plan in plans:
        if plan.get("type") not in {"kg", "blacklist"}:
            continue
        for product in plan.get("products") or []:
            product_id = str(product.get("productId") or product.get("product_id") or "").strip()
            if product_id in (blacklist_product_statuses or {}):
                product["blacklistStatus"] = blacklist_product_statuses[product_id]
        for group in plan.get("causeGroups") or []:
            for product in group.get("products") or group.get("affectedProducts") or group.get("affected_products") or []:
                product_id = str(product.get("productId") or product.get("product_id") or "").strip()
                if product_id in (blacklist_product_statuses or {}):
                    product["blacklistStatus"] = blacklist_product_statuses[product_id]
    order = {"kg": 0, "reorder": 1, "blacklist": 2, "demotion": 3, "demotion_restore": 4}
    return sorted(plans, key=lambda item: (order.get(item.get("type"), 9), str(item.get("id"))))


def _reorder_conclusion_with_statuses(
    conclusion: str,
    *,
    strategy_status: str,
    execution_status: str,
) -> str:
    strategy_text = {
        "success": "AI 重排序策略已生成",
        "pending": "AI 重排序策略生成中",
    }.get(strategy_status, "AI 重排序策略生成失败")
    execution_text = {
        "success": "重排序执行成功",
        "pending": "重排序执行中",
        "not_run": "重排序未执行",
        "inconclusive": "重排序结果待确认",
    }.get(execution_status, "重排序执行失败")
    connector = "但" if strategy_status == "success" and execution_status == "failed" else ""
    return f"{conclusion}{strategy_text}，{connector}{execution_text}。"


def _node_result(node: Mapping[str, Any]) -> Mapping[str, Any] | None:
    result = node.get("result")
    for _ in range(2):
        if not isinstance(result, Mapping) or not isinstance(result.get("result"), Mapping):
            break
        result = result["result"]
    return result if isinstance(result, Mapping) else None



def _step_issue_evidence(node: Mapping[str, Any], *, inventory_count: int = 0) -> dict[str, Any]:
    """Expose Query-level issue evidence while retaining node execution status."""
    node_key = str(node.get("nodeKey") or "")
    result = _node_result(node) or {}
    issue_types: list[str] = []
    if node_key == "recall":
        raw_reasons = result.get("reasons")
        if isinstance(raw_reasons, list):
            issue_types = [str(item) for item in raw_reasons if item]
        elif raw_reasons:
            issue_types = [str(raw_reasons)]
        raw_evidence = result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {}
        count = raw_evidence.get("result_count", raw_evidence.get("resultCount"))
        payload = {"issueTypes": issue_types}
        if _is_token_count(count):
            payload["resultCount"] = int(count)
        return payload
    if node_key in {"reorder", "open_factor"} and result.get("should_reorder") is True:
        return {"issueTypes": ["should_reorder"]}
    if node_key == "relevance":
        candidates = result.get("blacklist_candidates") or result.get("blacklistCandidates") or []
        normalized_candidates = [dict(item) for item in candidates if isinstance(item, Mapping)] if isinstance(candidates, list) else []
        return {
            "issueTypes": ["blacklist_candidates"] if normalized_candidates else [],
            "irrelevantProductCount": len(normalized_candidates),
            "blacklistCandidates": normalized_candidates,
        }
    if node_key == "inventory" and inventory_count > 0:
        return {"issueTypes": ["inventory"], "affectedProductCount": inventory_count}
    return {"issueTypes": []}


_GOVERNANCE_STAGE_BY_ACTION = {
    "kg_plan": "recall", "kg_tag": "recall", "kg_relation": "recall",
    "relevance_plan": "relevance", "product_blacklist": "relevance",
    "query_reorder": "open_factor", "query_demote": "inventory",
}
_GOVERNANCE_PRIORITY = {
    "failed": 5, "rollback_failed": 5, "execution_unknown": 4,
    "pending": 3, "executing": 3, "rollback_pending": 3,
    "manual_review": 3, "rejected": 3, "rolled_back": 3,
    "verified": 1, "no_change": 1,
}


def _governance_states(
    actions: Sequence[Any],
    async_tasks: Sequence[Any],
    *,
    include_execution_mode: bool = False,
) -> dict[str, dict[str, Any]]:
    """Project downstream governance facts without changing the node status."""
    candidates: dict[str, list[dict[str, Any]]] = {}
    represented_sources = {
        str(_json_object(getattr(action, "request_json", None)).get("trigger_source") or "")
        for action in actions
        if str(getattr(action, "action_type", "")) in {"kg_plan", "relevance_plan"}
    }
    for action in actions:
        action_type = str(getattr(action, "action_type", "") or "")
        stage = _GOVERNANCE_STAGE_BY_ACTION.get(action_type)
        if stage is None:
            continue
        status = str(getattr(action, "status", "") or "pending").lower()
        request = _json_object(getattr(action, "request_json", None)) or {}
        source = str(request.get("source") or "")
        default_execution_mode = (
            "auto"
            if action_type in {"query_reorder", "product_blacklist"} or source == "inventory_monitor"
            else "human_approval"
        )
        execution_mode = str(
            request.get("execution_mode") or request.get("executionMode") or default_execution_mode
        ).strip().lower()
        failure_stage = str(getattr(action, "failure_stage", "") or "")
        if status == "failed" and failure_stage == "generation":
            label = "生成失败"
        else:
            try:
                _, label = _plan_status(status, executor_type=getattr(action, "executor_type", None))
            except ValueError:
                label = status
        state = {
            "status": status, "label": label,
            "errorCode": getattr(action, "error_code", None),
            "errorMessage": getattr(action, "error_message", None),
        }
        if include_execution_mode:
            state["executionMode"] = execution_mode
            state["executorType"] = getattr(action, "executor_type", None)
        candidates.setdefault(stage, []).append(state)
    for async_task in async_tasks:
        source = str(getattr(async_task, "trigger_source", "") or "")
        stage = "recall" if source == "recall_anomaly" else "relevance" if source == "relevance_root_cause" else None
        if stage is None or source in represented_sources:
            continue
        status = str(getattr(async_task, "status", "") or "pending").lower()
        if status == "pending" and actions:
            continue
        plan = _json_object(getattr(async_task, "plan_json", None)) or {}
        decision = str(plan.get("decision") or "")
        if status == "error":
            projected_status, label = "failed", "生成失败"
        elif status in {"pending", "processing"}:
            projected_status, label = "pending", "生成中"
        elif decision == "manual_review":
            projected_status, label = "manual_review", "需人工复核"
        elif decision == "no_change":
            projected_status, label = "no_change", "无需调整"
        else:
            projected_status, label = "pending", "待执行"
        state = {
            "status": projected_status, "label": label,
            "errorCode": getattr(async_task, "error_code", None),
            "errorMessage": getattr(async_task, "error_message", None),
        }
        if include_execution_mode:
            state["executionMode"] = "human_approval"
            state["executorType"] = None
        candidates.setdefault(stage, []).append(state)
    states: dict[str, dict[str, Any]] = {}
    for stage, items in candidates.items():
        projected = dict(max(items, key=lambda item: _GOVERNANCE_PRIORITY.get(item["status"], 0)))
        if include_execution_mode:
            projected["requiresManualIntervention"] = any(
                item["status"] in {"manual_review", "execution_unknown", "rollback_failed"}
                or (
                    item["status"] == "pending"
                    and item.get("label") != "生成中"
                    and item.get("executionMode") in _GOVERNANCE_MANUAL_MODES
                )
                for item in items
            )
            projected["hasNonAiStatus"] = any(
                item["status"] in _GOVERNANCE_NON_AI_STATUSES for item in items
            )
            projected["hasHumanExecution"] = any(
                item["status"] == "verified" and item.get("executorType") == "human"
                for item in items
            )
        states[stage] = projected
    return states


def _governance_states_for_tasks(
    session: Any,
    tasks: Sequence[Any],
    *,
    include_execution_mode: bool = False,
) -> dict[int, dict[str, dict[str, Any]]]:
    task_ids = [int(task.id) for task in tasks]
    if not task_ids:
        return {}
    actions_by_task: dict[int, list[Any]] = {task_id: [] for task_id in task_ids}
    for action in session.execute(
        select(QueryGovernanceActionPlan)
        .options(load_only(
            QueryGovernanceActionPlan.task_id,
            QueryGovernanceActionPlan.action_type,
            QueryGovernanceActionPlan.request_json,
            QueryGovernanceActionPlan.status,
            QueryGovernanceActionPlan.failure_stage,
            QueryGovernanceActionPlan.executor_type,
            QueryGovernanceActionPlan.error_code,
            QueryGovernanceActionPlan.error_message,
        ))
        .where(QueryGovernanceActionPlan.task_id.in_(task_ids))
        .order_by(QueryGovernanceActionPlan.id.asc())
    ).scalars().all():
        actions_by_task.setdefault(int(action.task_id), []).append(action)
    async_by_task: dict[int, list[Any]] = {task_id: [] for task_id in task_ids}
    for async_task in session.execute(
        select(QueryGovernanceAsyncTask)
        .options(load_only(
            QueryGovernanceAsyncTask.task_id,
            QueryGovernanceAsyncTask.trigger_source,
            QueryGovernanceAsyncTask.status,
            QueryGovernanceAsyncTask.plan_json,
            QueryGovernanceAsyncTask.error_code,
            QueryGovernanceAsyncTask.error_message,
        ))
        .where(QueryGovernanceAsyncTask.task_id.in_(task_ids))
        .order_by(QueryGovernanceAsyncTask.id.asc())
    ).scalars().all():
        async_by_task.setdefault(int(async_task.task_id), []).append(async_task)
    return {
        task_id: _governance_states(
            actions_by_task.get(task_id, ()),
            async_by_task.get(task_id, ()),
            include_execution_mode=include_execution_mode,
        )
        for task_id in task_ids
    }


def _step_governance_status(
    node_key: Any, governance_states: Mapping[str, Mapping[str, Any]] | None
) -> dict[str, Any]:
    stage = "open_factor" if node_key == "reorder" else str(node_key or "")
    state = governance_states.get(stage) if governance_states else None
    return {"governanceStatus": dict(state)} if state else {}


def _query_issue_counts(
    tasks: Sequence[Any],
    steps: Sequence[Any],
    inventory_records: Sequence[Any] = (),
) -> dict[str, int]:
    task_ids = {int(task.id) for task in tasks}
    steps_by_task: dict[int, dict[str, Any]] = {}
    for step in steps:
        if int(step.task_id) not in task_ids:
            continue
        steps_by_task.setdefault(int(step.task_id), {})[str(step.node_key)] = step
    inventory_by_query: dict[str, int] = {}
    for item in inventory_records:
        if item.status in {"monitoring", "auto_stopped"}:
            query = str(item.original_query or "").strip()
            if query:
                inventory_by_query[query] = inventory_by_query.get(query, 0) + 1
    counts = {"recall": 0, "relevance": 0, "open_factor": 0, "inventory": 0}
    for task in tasks:
        task_steps = steps_by_task.get(int(task.id), {})
        recall = _step_issue_evidence(_node_payload_from_step(task_steps.get("recall"), "recall"))
        if recall["issueTypes"]:
            counts["recall"] += 1
        relevance = _step_issue_evidence(_node_payload_from_step(task_steps.get("relevance"), "relevance"))
        if relevance["issueTypes"]:
            counts["relevance"] += 1
        reorder_step = task_steps.get("reorder")
        reorder = _step_issue_evidence(_node_payload_from_step(reorder_step, "reorder"))
        reorder_status = str(getattr(reorder_step, "status", "") or "").lower()
        if reorder["issueTypes"] or reorder_status in {"error", "failed", "still_problematic", "check_failed"}:
            counts["open_factor"] += 1
        if inventory_by_query.get(str(task.query_text or "").strip(), 0):
            counts["inventory"] += 1
    return counts


def _recall_issue_counts(tasks: Sequence[Any], steps: Sequence[Any]) -> tuple[int, int]:
    """Count recall Query issues by their persisted reason categories."""
    task_ids = {int(task.id) for task in tasks}
    steps_by_task: dict[int, Any] = {}
    for step in steps:
        if int(step.task_id) in task_ids and str(step.node_key) == "recall":
            steps_by_task[int(step.task_id)] = step
    no_result_or_low_result = 0
    result_drop = 0
    for task in tasks:
        evidence = _step_issue_evidence(
            _node_payload_from_step(steps_by_task.get(int(task.id)), "recall")
        )
        issue_types = set(evidence["issueTypes"])
        if issue_types & {"no_result", "low_result"}:
            no_result_or_low_result += 1
        if "result_count_drop" in issue_types:
            result_drop += 1
    return no_result_or_low_result, result_drop


def _s0_l0_l2_recall_status(snapshot: Mapping[str, Any] | None) -> bool | None:
    """Return whether a complete S0 product list has no L0-L2 products.

    ``None`` means the snapshot does not contain enough bucket evidence to
    replace the persisted recall-node result.  In particular, a truncated
    Mixed response or a product without a bucket must keep the old fallback
    semantics instead of producing a false no-recall diagnosis.
    """
    if not isinstance(snapshot, Mapping):
        return None
    if snapshot.get("result_complete") is False:
        return None
    products = snapshot.get("products")
    if not isinstance(products, list):
        return None
    if not products:
        return True
    buckets: list[str] = []
    for product in products:
        if not isinstance(product, Mapping):
            return None
        ext = product.get("ext")
        bucket = ext.get("bucket") if isinstance(ext, Mapping) else product.get("bucket")
        if not isinstance(bucket, str) or not bucket.strip():
            return None
        buckets.append(bucket.strip())
    return not any(bucket in _L0_L2_RECALL_BUCKETS for bucket in buckets)


def _query_quality_metrics(
    tasks: Sequence[Any],
    steps: Sequence[Any],
    *,
    inventory_records: Sequence[Any] = (),
    visible_checks: set[str] | None = None,
    s0_snapshots: Mapping[int, Mapping[str, Any]] | None = None,
    governance_states_by_task: Mapping[int, Mapping[str, Mapping[str, Any]]] | None = None,
) -> dict[str, int | float | None]:
    """Calculate Query-level quality and processing metrics for the overview."""
    task_ids = {int(task.id) for task in tasks}
    steps_by_task: dict[int, dict[str, Any]] = {}
    for step in steps:
        if int(step.task_id) in task_ids:
            steps_by_task.setdefault(int(step.task_id), {})[str(step.node_key)] = step
    recall_by_task = {
        int(step.task_id): step
        for step in steps
        if int(step.task_id) in task_ids and str(step.node_key) == "recall"
    }
    no_recall_count = 0
    recall_rate_values: list[float] = []
    for task in tasks:
        s0_status = _s0_l0_l2_recall_status(
            (s0_snapshots or {}).get(int(task.id))
        )
        if s0_status is not None:
            no_recall_count += int(s0_status)
            continue
        node = _node_payload_from_step(recall_by_task.get(int(task.id)), "recall")
        result = _node_result(node) or {}
        evidence = result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {}
        result_count = evidence.get("result_count", evidence.get("resultCount"))
        if not _is_token_count(result_count):
            result_count = result.get("result_count", result.get("resultCount"))
        reasons = result.get("reasons") or []
        if (isinstance(result_count, (int, float)) and result_count <= 0) or any(str(reason) == "no_result" for reason in reasons if reason):
            no_recall_count += 1
    # AI 召回率检测只对启用且跑出报告的 Query 生效；未启用的 Query 不进分母。
    for task in tasks:
        node = _node_payload_from_step(recall_by_task.get(int(task.id)), "recall")
        result = _node_result(node) or {}
        evidence = result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {}
        report = evidence.get("recall_rate")
        if not isinstance(report, Mapping):
            report = result.get("recall_rate") if isinstance(result.get("recall_rate"), Mapping) else None
        if not isinstance(report, Mapping):
            continue
        post_rate = report.get("post_search_recall_rate")
        if isinstance(post_rate, (int, float)) and not isinstance(post_rate, bool):
            recall_rate_values.append(float(post_rate))
    relevance_check_enabled = visible_checks is None or "relevance" in visible_checks
    pass_count = 0
    if relevance_check_enabled:
        for task in tasks:
            if _relevance_check_passed(task, steps_by_task.get(int(task.id), {})):
                pass_count += 1

    total = len(tasks)
    pending = 0
    completed = 0
    for task in tasks:
        task_states = (governance_states_by_task or {}).get(int(task.id), {})
        contains_non_ai_status = any(
            bool(state.get("hasNonAiStatus"))
            if "hasNonAiStatus" in state
            else str(state.get("status") or "").lower() in _GOVERNANCE_NON_AI_STATUSES
            for state in task_states.values()
            if isinstance(state, Mapping)
        )
        needs_manual_intervention = any(
            bool(state.get("requiresManualIntervention"))
            if "requiresManualIntervention" in state
            else (
                status in {"manual_review", "execution_unknown", "rollback_failed"}
                or (
                    status == "pending"
                    and str(state.get("label") or "") != "生成中"
                    and str(state.get("executionMode") or "").lower() in _GOVERNANCE_MANUAL_MODES
                )
            )
            for state in task_states.values()
            if isinstance(state, Mapping)
            for status in [str(state.get("status") or "").lower()]
        )
        manually_executed = any(
            bool(state.get("hasHumanExecution"))
            if "hasHumanExecution" in state
            else str(state.get("status") or "").lower() == "verified"
            and (
                str(state.get("executorType") or "").lower() == "human"
                or (
                    not state.get("executorType")
                    and str(state.get("executionMode") or "").lower() in _GOVERNANCE_MANUAL_MODES
                )
            )
            for state in task_states.values()
            if isinstance(state, Mapping)
        )
        if str(getattr(task, "status", "")).lower() == "manual_required" or needs_manual_intervention:
            pending += 1
        if (
            str(getattr(task, "status", "")).lower() == "completed"
            and not contains_non_ai_status
            and not manually_executed
        ):
            completed += 1
    return {
        "noRecallCount": no_recall_count,
        "noRecallRate": no_recall_count / total if total else None,
        "passCount": pass_count if relevance_check_enabled else None,
        "passRate": (pass_count / total if total else None) if relevance_check_enabled else None,
        "aiProcessedCount": completed,
        "pendingCount": pending,
        "avgSearchRecallRate": (
            round(sum(recall_rate_values) / len(recall_rate_values), 4)
            if recall_rate_values
            else None
        ),
        "recallRateQueryCount": len(recall_rate_values),
        # 趋势按 Query 数加权聚合：sum ÷ count，避免直接平均日均值。
        "searchRecallRateSum": round(sum(recall_rate_values), 4) if recall_rate_values else None,
    }


def _relevance_check_passed(task: Any, task_steps: Mapping[str, Any]) -> bool:
    """Only the relevance check decides whether a Query counts as passed."""
    step = task_steps.get("relevance")
    if step is None:
        return False
    status = str(getattr(step, "status", "executed") or "executed").lower()
    if status in _QUALITY_STEP_FAILURE_STATUSES:
        return False
    evidence = _step_issue_evidence(_node_payload_from_step(step, "relevance"))
    return not evidence.get("issueTypes")


def _inventory_issue_queries(inventory_records: Sequence[Any]) -> set[str]:
    return {
        str(item.original_query or "").strip()
        for item in inventory_records
        if item.status in {"monitoring", "auto_stopped"} and str(item.original_query or "").strip()
    }


def _node_payload_from_step(step: Any, node_key: str) -> dict[str, Any]:
    if step is None:
        return {"nodeKey": node_key, "result": None}
    return {"nodeKey": node_key, "result": _json_object(step.result_json)}



def _is_token_count(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value >= 0 and value == int(value)
    )


def _zero_model_calls(node: Mapping[str, Any]) -> bool:
    telemetry = node.get("telemetry")
    count = telemetry.get("modelCallCount") if isinstance(telemetry, Mapping) else None
    return _is_token_count(count) and count == 0


def _node_token_audits(node: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], bool]:
    result = _node_result(node)
    if not isinstance(result, Mapping):
        return [], node.get("nodeKey") == "relevance" and node.get("status") in {"executed", "error"} and not _zero_model_calls(node)
    if isinstance(result.get("modelCallAudits"), list):
        audits = [item for item in result["modelCallAudits"] if isinstance(item, Mapping)]
        return audits, len(audits) != len(result["modelCallAudits"])
    audit = result.get("preference_audit") or result.get("audit")
    if isinstance(audit, Mapping):
        telemetry = node.get("telemetry") if isinstance(node.get("telemetry"), Mapping) else {}
        if isinstance(audit.get("modelCallAudits"), list):
            inherited = {
                "batchKey": audit.get("batchKey") or telemetry.get("batchKey"),
                "batchQueryCount": audit.get("batchQueryCount") or telemetry.get("batchQueryCount") or 1,
            }
            calls = [{**inherited, **item} for item in audit["modelCallAudits"] if isinstance(item, Mapping)]
            return calls, len(calls) != len(audit["modelCallAudits"])
        return [{
            **audit,
            "requestId": audit.get("requestId") or audit.get("batchKey") or telemetry.get("batchKey"),
            "batchQueryCount": audit.get("batchQueryCount") or telemetry.get("batchQueryCount") or 1,
        }], False
    return [], (
        node.get("nodeKey") == "relevance"
        and node.get("status") not in {"pending", "running", "skipped"}
        and not _zero_model_calls(node)
    )


def _token_totals(nodes: Sequence[Mapping[str, Any]], *, allocate: bool) -> dict[str, Any]:
    total = 0.0
    relevance = 0.0
    seen: set[str] = set()
    missing = relevance_missing = False
    relevance_known = False
    for node in nodes:
        audits, node_missing = _node_token_audits(node)
        is_relevance = node.get("nodeKey") == "relevance"
        missing |= node_missing
        relevance_missing |= is_relevance and node_missing
        if is_relevance and ((_node_result(node) or {}).get("modelCallAudits") == [] or (not audits and _zero_model_calls(node))):
            relevance_known = True
        for audit in audits:
            tokens = audit.get("tokenUsage")
            known = _is_token_count(tokens)
            audit_missing = not known or audit.get("tokenUsageMissing") is True
            missing |= audit_missing
            relevance_missing |= is_relevance and audit_missing
            if not known:
                continue
            if is_relevance:
                relevance_known = True
            request_id = audit.get("requestId")
            if request_id and str(request_id) in seen:
                continue
            if request_id:
                seen.add(str(request_id))
            count = audit.get("batchQueryCount")
            count = count if _is_token_count(count) and count > 0 else 1
            value = float(tokens) / count if allocate else float(tokens)
            total += value
            if is_relevance:
                relevance += value
    return {
        "tokenUsage": round(total, 3),
        "tokenUsageMissing": missing,
        "relevanceTokenUsage": round(relevance, 3) if relevance_known else None,
        "relevanceTokenUsageMissing": relevance_missing,
    }


class QueryGovernanceReadError(RuntimeError):
    """Raised when the deterministic read model cannot produce a contract."""


class QueryGovernanceNotFoundError(QueryGovernanceReadError):
    """Raised when a requested batch or Query does not exist."""


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(decoded) if isinstance(decoded, Mapping) else None


def _inventory_action_failure(
    session: Any,
    *,
    brand: str,
    query: str,
    updated_after: datetime | None = None,
) -> dict[str, Any] | None:
    """Read the latest failed independent inventory action for one Query.

    Inventory monitoring is intentionally not represented by a workflow step.
    Its durable execution fact lives in ``action_intent`` and, when available,
    ``action_terminal_fact``.  Keep this projection read-only and only trust
    intents explicitly marked as originating from the inventory monitor.
    """
    statement = select(ActionIntent).where(
        ActionIntent.brand == brand,
        ActionIntent.query_text == query,
        ActionIntent.status == "failed",
    )
    if updated_after is not None:
        statement = statement.where(ActionIntent.created_at >= updated_after)
    intents = session.execute(
        statement
        .order_by(ActionIntent.created_at.desc(), ActionIntent.id.desc())
    ).scalars().all()
    for intent in intents:
        payload = _json_object(intent.payload_json) or {}
        if str(payload.get("source") or "").strip().lower() != "inventory_monitor":
            continue
        terminal = session.execute(
            select(ActionTerminalFact)
            .where(ActionTerminalFact.intent_id == intent.id)
            .order_by(ActionTerminalFact.created_at.desc(), ActionTerminalFact.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        terminal_payload = _json_object(
            terminal.replay_result_json if terminal is not None else None
        ) or _json_object(terminal.result_json if terminal is not None else None) or {}
        error_code = str(
            terminal_payload.get("errorCode")
            or terminal_payload.get("error_code")
            or terminal_payload.get("code")
            or payload.get("errorCode")
            or payload.get("error_code")
            or "INVENTORY_ACTION_FAILED"
        ).strip()
        error_message = str(
            terminal_payload.get("errorMessage")
            or terminal_payload.get("error_message")
            or terminal_payload.get("error")
            or terminal_payload.get("message")
            or payload.get("errorMessage")
            or payload.get("error_message")
            or "断码动作执行失败，请查看动作事实"
        ).strip()
        return {
            "available": False,
            "source": "independent_job",
            "status": "failed",
            "errorCode": error_code or "INVENTORY_ACTION_FAILED",
            "errorMessage": error_message or "断码动作执行失败，请查看动作事实",
        }
    return None


def _empty_counts() -> dict[str, int]:
    return {status: 0 for status in _STATUS_VALUES}


def _aggregate_status(statuses: Sequence[str]) -> str:
    normalized = [status for status in statuses if status in _STATUS_VALUES]
    if not normalized or all(status == NodeStatus.PENDING.value for status in normalized):
        return NodeStatus.PENDING.value
    if NodeStatus.RUNNING.value in normalized:
        return NodeStatus.RUNNING.value
    if NodeStatus.ERROR.value in normalized:
        return NodeStatus.ERROR.value
    if all(status == NodeStatus.SKIPPED.value for status in normalized):
        return NodeStatus.SKIPPED.value
    if all(
        status in {
            NodeStatus.EXECUTED.value,
            NodeStatus.SKIPPED.value,
        }
        for status in normalized
    ):
        return NodeStatus.EXECUTED.value
    return NodeStatus.PENDING.value


def _aggregate_batch_status(
    statuses: Sequence[str],
    *,
    workflow_status: str | None = None,
) -> str:
    """Aggregate a batch without promoting an isolated Query/node error.

    Query-level errors remain visible in the detail payload.  A batch is only
    abnormal when it has no usable executed/skipped result and contains an
    error, or when workflow initialization failed before any Query existed.
    """
    normalized = [status for status in statuses if status in _STATUS_VALUES]
    if not normalized:
        return NodeStatus.ERROR.value if workflow_status == "error" else NodeStatus.PENDING.value
    if NodeStatus.RUNNING.value in normalized:
        return NodeStatus.RUNNING.value
    if NodeStatus.PENDING.value in normalized:
        return NodeStatus.PENDING.value
    if NodeStatus.EXECUTED.value in normalized:
        return NodeStatus.EXECUTED.value
    if NodeStatus.ERROR.value in normalized:
        return NodeStatus.ERROR.value
    return NodeStatus.SKIPPED.value


def _duration_stats(values: Sequence[float]) -> dict[str, float | None]:
    normalized = sorted(max(0.0, float(value)) for value in values if value is not None)
    if not normalized:
        return {"averageSeconds": None, "p50Seconds": None, "p95Seconds": None}

    def percentile(ratio: float) -> float:
        # Nearest-rank percentile keeps the metric deterministic for small
        # batches (P95 of two samples is the larger sample).
        index = min(len(normalized) - 1, max(0, int((len(normalized) * ratio + 0.999999999) - 1)))
        return round(normalized[index], 3)

    return {
        "averageSeconds": round(sum(normalized) / len(normalized), 3),
        "p50Seconds": percentile(0.5),
        "p95Seconds": percentile(0.95),
    }


def _metric_raw_payload(metric: BrandDailyMetric | None) -> dict[str, Any]:
    if metric is None:
        return {}
    return _json_object(metric.raw_payload) or {}


def _metric_priority(metric: BrandDailyMetric) -> tuple[int, int, float, int]:
    raw = _metric_raw_payload(metric)
    return (
        1 if raw.get("metric_source") == "user_behavior" else 0,
        int(metric.top_query_count or 0),
        float(metric.top_query_pv or 0),
        int(metric.id or 0),
    )


def _business_metric_payload(metric: BrandDailyMetric | None) -> dict[str, Any] | None:
    if metric is None:
        return None
    raw = _metric_raw_payload(metric)
    return {
        "brandUv": int(metric.brand_uv or 0),
        "brandPv": int(metric.brand_pv or 0),
        "clickUv": int(raw.get("click_uv") or 0),
        "addToCartUv": int(raw.get("add_to_cart_uv") or 0),
        "purchaseUv": int(raw.get("purchase_uv") or 0),
        "ctr": float(metric.ctr or 0),
        "atc": float(raw.get("atc") or 0),
        "cvr": float(metric.cvr or 0),
    }


def _business_metric_trend(
    session: Any,
    brand: str,
    *,
    end_date: date | None = None,
) -> list[dict[str, Any]]:
    query = select(BrandDailyMetric).where(BrandDailyMetric.brand == brand)
    if end_date is not None:
        query = query.where(BrandDailyMetric.data_date <= end_date)
    rows = session.execute(
        query.order_by(BrandDailyMetric.data_date.asc(), BrandDailyMetric.id.asc())
    ).scalars().all()
    by_date: dict[date, BrandDailyMetric] = {}
    for row in rows:
        if row.data_date is None:
            continue
        current = by_date.get(row.data_date)
        if current is None or _metric_priority(row) > _metric_priority(current):
            by_date[row.data_date] = row
    trend: list[dict[str, Any]] = []
    for metric_date in sorted(by_date):
        metric = by_date[metric_date]
        payload = _business_metric_payload(metric) or {}
        trend.append({
            "date": metric_date.isoformat(),
            "uv": payload.get("brandUv", 0),
            "pv": payload.get("brandPv", 0),
            "clickUv": payload.get("clickUv", 0),
            "addToCartUv": payload.get("addToCartUv", 0),
            "purchaseUv": payload.get("purchaseUv", 0),
            "ctr": payload.get("ctr", 0),
            "atc": payload.get("atc", 0),
            "cvr": payload.get("cvr", 0),
        })
    return trend


def _metric_trend_number(value: Any) -> int | float | None:
    """Return a chartable number, treating booleans and gaps as missing."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _metric_trend_ratio(numerator: Any, denominator: Any) -> float | None:
    top = _metric_trend_number(numerator)
    bottom = _metric_trend_number(denominator)
    if top is None or bottom is None or bottom <= 0:
        return None
    return round(top / bottom, 4)


def _metric_trend_metrics(
    payload: Mapping[str, Any],
    *,
    blacklisted_product_count: int | None = None,
) -> dict[str, Any]:
    """Expose one summary as trendable values plus the proportion inputs.

    Ratios are returned next to their numerator and denominator so the client
    can re-aggregate over week/month/year buckets instead of averaging daily
    ratios. ``blacklisted_product_count`` is computed at read time from the
    current blacklist state so the relevance-detection accuracy moves with
    later human cancels or approvals.
    """

    telemetry = payload.get("telemetry")
    telemetry = telemetry if isinstance(telemetry, Mapping) else {}
    query_count = _metric_trend_number(telemetry.get("queryCount"))
    pass_count = _metric_trend_number(telemetry.get("passCount"))
    no_recall_count = _metric_trend_number(telemetry.get("noRecallCount"))
    detected_product_count = _metric_trend_number(telemetry.get("detectedProductCount"))
    reused_product_count = _metric_trend_number(telemetry.get("reusedProductCount"))
    inspected_product_count = _metric_trend_number(telemetry.get("inspectedProductCount"))
    if inspected_product_count is None and detected_product_count is not None:
        inspected_product_count = detected_product_count + (reused_product_count or 0)
    irrelevant_product_count = _metric_trend_number(telemetry.get("irrelevantProductCount"))
    relevance_accuracy = None
    if inspected_product_count is not None and irrelevant_product_count is not None:
        relevance_accuracy = _metric_trend_ratio(
            inspected_product_count - irrelevant_product_count,
            inspected_product_count,
        )
    pairs_raw = telemetry.get("irrelevantProductPairs")
    irrelevant_product_pair_count = _metric_trend_number(telemetry.get("irrelevantProductPairCount"))
    if irrelevant_product_pair_count is None and isinstance(pairs_raw, list):
        irrelevant_product_pair_count = len(pairs_raw)
    return {
        "query_count": query_count,
        "pass_count": pass_count,
        "pass_rate": _metric_trend_ratio(pass_count, query_count),
        "no_recall_count": no_recall_count,
        "no_recall_rate": _metric_trend_ratio(no_recall_count, query_count),
        "ai_processed_count": _metric_trend_number(telemetry.get("aiProcessedCount")),
        "pending_count": _metric_trend_number(telemetry.get("pendingCount")),
        "irrelevant_query_count": _metric_trend_number(telemetry.get("irrelevantQueryCount")),
        "preference_adjusted_query_count": _metric_trend_number(telemetry.get("preferenceAdjustedQueryCount")),
        "stock_bucket_reduction_product_count": _metric_trend_number(telemetry.get("stockBucketReductionProductCount")),
        "detected_product_count": detected_product_count,
        "reused_product_count": reused_product_count,
        "inspected_product_count": inspected_product_count,
        "irrelevant_product_count": irrelevant_product_count,
        "relevance_accuracy": relevance_accuracy,
        "irrelevant_product_pair_count": irrelevant_product_pair_count,
        "blacklisted_product_count": blacklisted_product_count,
        "avg_search_recall_rate": _metric_trend_number(telemetry.get("avgSearchRecallRate")),
        "recall_rate_query_count": _metric_trend_number(telemetry.get("recallRateQueryCount")),
        "search_recall_rate_sum": _metric_trend_number(telemetry.get("searchRecallRateSum")),
        "relevance_detection_accuracy": (
            _metric_trend_ratio(blacklisted_product_count, irrelevant_product_pair_count)
            if blacklisted_product_count is not None
            else None
        ),
        "token_usage": _metric_trend_number(telemetry.get("tokenUsage")),
        "duration_seconds": _metric_trend_number(telemetry.get("wallClockSeconds")),
        "top_query_limit": _metric_trend_number(telemetry.get("topQueryLimit")),
        "rapid_pv_growth_query_count": _metric_trend_number(telemetry.get("rapidPvGrowthQueryCount")),
        "new_query_count": _metric_trend_number(telemetry.get("newQueryCount")),
        "ctr_decline_query_count": _metric_trend_number(telemetry.get("ctrDeclineQueryCount")),
        "cvr_decline_query_count": _metric_trend_number(telemetry.get("cvrDeclineQueryCount")),
        "model_call_count": _metric_trend_number(telemetry.get("modelCallCount")),
    }


def _metric_trend_day(
    day: date,
    summary: Mapping[str, Any] | None,
    *,
    blacklisted_product_count: int | None = None,
) -> dict[str, Any]:
    """Build one trend point, keeping missing or stale days as explicit gaps."""

    if summary is None:
        return {"date": day.isoformat(), "batchId": None, "metrics": None}
    telemetry = summary.get("telemetry")
    telemetry = telemetry if isinstance(telemetry, Mapping) else {}
    if telemetry.get("qualityMetricsVersion") != _QUALITY_METRICS_VERSION:
        return {"date": day.isoformat(), "batchId": summary.get("batchId") or None, "metrics": None}
    return {
        "date": day.isoformat(),
        "batchId": summary.get("batchId") or None,
        "metrics": _metric_trend_metrics(summary, blacklisted_product_count=blacklisted_product_count),
    }


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


class QueryGovernanceReadService:
    """Build stable API payloads from the rewrite's persisted state."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        live_search: Callable[[str, str, int], Mapping[str, Any]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._live_search = live_search

    def overview(
        self,
        brands: Sequence[str],
        *,
        data_date: date | None = None,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        with self._session_factory() as session:
            summaries = self._overview_summaries(
                session,
                brands,
                data_date=data_date,
            )
            for brand in brands:
                summary = summaries.get(brand)
                telemetry = summary.get("telemetry") if summary is not None else None
                if summary is not None and (
                    "noRecallRate" not in (telemetry or {})
                    or telemetry.get("qualityMetricsVersion") != _QUALITY_METRICS_VERSION
                ):
                    summary = self._build_overview_item(session, brand, data_date=data_date)
                summary = (
                    summary
                    if summary is not None
                    else self._build_overview_item(session, brand, data_date=data_date)
                )
                if summary is not None:
                    _inject_relevance_detection_accuracy(session, brand, summary)
                items.append(summary)
        return {"brands": items}

    def materialize_overview_summary(
        self,
        brand: str,
        *,
        data_date: date | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one completed batch summary outside the request hot path."""

        with self._session_factory() as session:
            item = self._build_overview_item(session, brand, data_date=data_date, batch_id=batch_id)
            selected_batch_id = str(item.get("batchId") or "")
            item_date = item.get("dataDate")
            if not selected_batch_id or not item_date:
                return item
            summary = session.execute(
                select(QueryGovernanceBatchSummary).where(
                    QueryGovernanceBatchSummary.batch_id == selected_batch_id,
                    QueryGovernanceBatchSummary.brand == brand,
                )
            ).scalar_one_or_none()
            if summary is None:
                summary = QueryGovernanceBatchSummary(
                    batch_id=selected_batch_id,
                    brand=brand,
                    data_date=date.fromisoformat(str(item_date)),
                    payload_json="{}",
                )
                session.add(summary)
            summary.data_date = date.fromisoformat(str(item_date))
            summary.payload_json = json.dumps(item, ensure_ascii=False, sort_keys=True)
            session.commit()
            return item

    @classmethod
    def _build_overview_item(
        cls,
        session: Any,
        brand: str,
        *,
        data_date: date | None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        batch_id = batch_id or cls._latest_batch_id(session, brand, data_date=data_date)
        if batch_id is None:
            return {
                "brand": brand,
                "batchId": "",
                "dataDate": None,
                "status": "skipped",
                "reason": "no_data",
                "queryCount": 0,
                "nodeStatusCounts": _empty_counts(),
                "durationSeconds": None,
                "startedAt": None,
                "completedAt": None,
                "telemetry": {},
            }
        tasks = cls._tasks(session, batch_id, brand=brand)
        steps = cls._steps_for_tasks(session, tasks, include_s0_result=False)
        run = cls._workflow_run(session, batch_id)
        query_statuses = cls._query_statuses(tasks, steps)
        item = {
            "brand": brand,
            "batchId": batch_id,
            "dataDate": _iso(tasks[0].data_date) if tasks else None,
            "status": _aggregate_status(list(query_statuses.values())),
            "queryCount": len(tasks),
            "queryStatusCounts": {
                status: sum(1 for value in query_statuses.values() if value == status)
                for status in _STATUS_VALUES
            },
            "nodeStatusCounts": cls._node_counts(steps),
            "durationSeconds": None,
            "startedAt": _iso(run.started_at if run is not None else None),
            "completedAt": _iso(run.finished_at if run is not None else None),
        }
        inventory_records = read_inventory_recovery_tasks(
            session,
            brand=brand,
            updated_after=run.started_at if run is not None else None,
        )
        inventory_issue_queries = _inventory_issue_queries(inventory_records)
        selection_batch = session.execute(
            select(QueryGovernanceSelectionBatch).where(
                QueryGovernanceSelectionBatch.batch_id == batch_id,
                QueryGovernanceSelectionBatch.brand == brand,
            )
        ).scalar_one_or_none()
        candidate_rows = session.execute(
            select(QueryGovernanceCandidate).where(
                QueryGovernanceCandidate.selection_batch_id
                == (selection_batch.id if selection_batch is not None else -1)
            )
        ).scalars().all()
        candidate_metrics, _ = cls._candidate_display_metrics(
            session,
            tasks,
            candidate_rows=candidate_rows,
        )
        ordered_tasks = cls._tasks_by_pv(tasks, candidate_metrics)
        filter_task_ids = {
            "all": [int(task.id) for task in ordered_tasks],
        }
        for filter_key in (
            "waiting_human_approval", "fixed", "normal", "still_problematic",
            "recall", "result_drop", "relevance", "open_factor", "inventory",
        ):
            filter_task_ids[filter_key] = cls._filtered_task_ids(
                ordered_tasks,
                steps,
                inventory_issue_queries,
                filter_key,
                query_statuses=query_statuses,
            )
        batch = cls._batch_payload(
            session,
            batch_id,
            brand,
            tasks,
            query_statuses,
            run=run,
            step_rows=steps,
            inventory_records=inventory_records,
            selection_batch=selection_batch,
            candidate_rows=candidate_rows,
            include_quality_metrics_evidence=True,
        )
        item.update({key: batch[key] for key in ("status", "reason", "dataDate")})
        item["telemetry"] = batch.get("telemetry", {})
        item["durationSeconds"] = item["telemetry"].get("wallClockSeconds")
        issue_counts = _query_issue_counts(tasks, steps, inventory_records)
        no_result_or_low_result_count, result_drop_count = _recall_issue_counts(tasks, steps)
        metric_date = tasks[0].data_date if tasks else data_date
        metric_rows = session.execute(
            select(BrandDailyMetric).where(
                BrandDailyMetric.brand == brand,
                BrandDailyMetric.data_date == metric_date,
            ).order_by(BrandDailyMetric.id.asc())
        ).scalars().all() if metric_date is not None else []
        stats = _business_metric_payload(max(metric_rows, key=_metric_priority)) if metric_rows else {}
        stats = stats or {}
        stats.update({
            "topQueryCount": len(tasks),
            "manualRequiredCount": sum(1 for task in tasks if str(task.status) == "manual_required"),
            "completedCount": sum(1 for task in tasks if str(task.status) == "completed"),
            "stepIssueCounts": issue_counts,
            "noResultOrLowResultCount": no_result_or_low_result_count,
            "resultDropCount": result_drop_count,
            "relevanceIssueCount": issue_counts["relevance"],
            "openFactorIssueCount": issue_counts["open_factor"],
            "inventoryProductIssueCount": issue_counts["inventory"],
        })
        item["stats"] = stats
        item["filterTaskIds"] = filter_task_ids
        return item

    @classmethod
    def _overview_summary(
        cls,
        session: Any,
        brand: str,
        *,
        data_date: date | None,
    ) -> dict[str, Any] | None:
        return cls._overview_summaries(
            session,
            [brand],
            data_date=data_date,
        ).get(brand)

    @staticmethod
    def _overview_summary_for_batch(
        session: Any,
        brand: str,
        batch_id: str,
    ) -> dict[str, Any] | None:
        """Read the materialized summary belonging to the selected batch.

        The Query rail already resolved the authoritative batch id.  Looking
        the summary up by that id avoids a second date-wide sort and, more
        importantly, prevents a summary from a different same-day batch from
        being mixed with the task page.
        """
        row = session.execute(
            select(QueryGovernanceBatchSummary.payload_json).where(
                QueryGovernanceBatchSummary.batch_id == batch_id,
                QueryGovernanceBatchSummary.brand == brand,
            )
        ).scalar_one_or_none()
        return _json_object(row)

    @classmethod
    def _overview_summaries(
        cls,
        session: Any,
        brands: Sequence[str],
        *,
        data_date: date | None,
    ) -> dict[str, dict[str, Any]]:
        """Read all available materialized brand summaries with one query."""

        normalized_brands = list(dict.fromkeys(str(brand) for brand in brands if brand))
        if not normalized_brands:
            return {}
        query = select(QueryGovernanceBatchSummary).where(
            QueryGovernanceBatchSummary.brand.in_(normalized_brands)
        )
        if data_date is not None:
            query = query.where(QueryGovernanceBatchSummary.data_date == data_date)
        rows = session.execute(
            query.order_by(
                QueryGovernanceBatchSummary.brand.asc(),
                QueryGovernanceBatchSummary.data_date.desc(),
                QueryGovernanceBatchSummary.id.desc(),
            )
        ).scalars().all()
        selected: dict[str, tuple[Any, dict[str, Any]]] = {}
        for row in rows:
            payload = _json_object(row.payload_json)
            if payload is None:
                continue
            brand = str(row.brand)
            candidate = (row, payload)
            current = selected.get(brand)
            if current is None or cls._overview_summary_sort_key(candidate) < cls._overview_summary_sort_key(current):
                selected[brand] = candidate
        return {brand: payload for brand, (_row, payload) in selected.items()}

    @classmethod
    def _overview_summary_sort_key(
        cls,
        candidate: tuple[Any, dict[str, Any]],
    ) -> tuple[int, int, int, int]:
        row, payload = candidate
        return (
            -row.data_date.toordinal(),
            cls._batch_priority(str(row.batch_id)),
            -int(payload.get("queryCount") or 0),
            -int(row.id or 0),
        )

    def brand_summary(
        self,
        brand: str,
        *,
        data_date: date | None = None,
    ) -> dict[str, Any]:
        """Read the small payload needed by the overview operations drawer."""

        with self._session_factory() as session:
            overview = self._overview_summaries(session, [brand], data_date=data_date).get(brand)
            if overview is not None:
                telemetry = overview.get("telemetry") if isinstance(overview.get("telemetry"), Mapping) else {}
                if "noRecallRate" not in (telemetry or {}) or telemetry.get("qualityMetricsVersion") != _QUALITY_METRICS_VERSION:
                    overview = self._build_overview_item(session, brand, data_date=data_date)
            if overview is not None:
                _inject_relevance_detection_accuracy(session, brand, overview)
            metric = None
            if data_date is not None:
                metric_rows = session.execute(
                    select(BrandDailyMetric).where(
                        BrandDailyMetric.brand == brand,
                        BrandDailyMetric.data_date == data_date,
                    ).order_by(BrandDailyMetric.id.asc())
                ).scalars().all()
                if metric_rows:
                    metric = max(metric_rows, key=_metric_priority)
            stats = _business_metric_payload(metric)
            if stats is not None:
                stats.update({
                    "topQueryCount": int((overview or {}).get("queryCount") or 0),
                    "manualRequiredCount": 0,
                    "completedCount": int((overview or {}).get("queryCount") or 0),
                })
            if overview is None:
                return {
                    "brand": brand,
                    "batch": None,
                    "summary": {"queryCount": 0, "queryStatusCounts": _empty_counts(), "nodeStatusCounts": _empty_counts()},
                    "stats": stats,
                }
            return {
                "brand": brand,
                "batch": {
                    "batchId": overview.get("batchId") or "",
                    "brand": brand,
                    "dataDate": overview.get("dataDate"),
                    "status": overview.get("status") or NodeStatus.PENDING.value,
                    "startedAt": overview.get("startedAt"),
                    "completedAt": overview.get("completedAt"),
                    "telemetry": overview.get("telemetry") or {},
                },
                "summary": {
                    "queryCount": int(overview.get("queryCount") or 0),
                    "queryStatusCounts": overview.get("queryStatusCounts") or _empty_counts(),
                    "nodeStatusCounts": overview.get("nodeStatusCounts") or _empty_counts(),
                },
                "stats": stats,
            }

    def brand_trend(self, brand: str, *, data_date: date | None = None) -> dict[str, Any]:
        with self._session_factory() as session:
            return {"brand": brand, "dataDate": _iso(data_date), "trend": _business_metric_trend(session, brand, end_date=data_date)}

    def metric_trend(
        self,
        brand: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, Any]:
        """Read per-day operations metric snapshots for the drawer trend modal.

        Only materialized batch summaries are read; a day without a summary, or
        with one written under an older quality definition, is returned with
        ``metrics: None`` so the chart leaves a gap instead of plotting a
        fabricated zero.
        """

        window_end = end_date or date.today()
        window_start = start_date or (window_end - timedelta(days=_DEFAULT_METRIC_TREND_DAYS - 1))
        if window_start > window_end:
            window_start, window_end = window_end, window_start
        window_start = max(window_start, window_end - timedelta(days=_MAX_METRIC_TREND_DAYS - 1))
        with self._session_factory() as session:
            rows = session.execute(
                select(QueryGovernanceBatchSummary)
                .where(
                    QueryGovernanceBatchSummary.brand == brand,
                    QueryGovernanceBatchSummary.data_date >= window_start,
                    QueryGovernanceBatchSummary.data_date <= window_end,
                )
                .order_by(
                    QueryGovernanceBatchSummary.data_date.asc(),
                    QueryGovernanceBatchSummary.id.desc(),
                )
            ).scalars().all()
        selected: dict[date, tuple[Any, dict[str, Any]]] = {}
        for row in rows:
            payload = _json_object(row.payload_json)
            if payload is None or row.data_date is None:
                continue
            candidate = (row, payload)
            current = selected.get(row.data_date)
            if current is None or self._overview_summary_sort_key(candidate) < self._overview_summary_sort_key(current):
                selected[row.data_date] = candidate
        day_pairs = {
            day: _irrelevant_product_pairs(payload)
            for day, (_row, payload) in selected.items()
        }
        pair_statuses: dict[tuple[str, str], str] = {}
        window_pairs = sorted({pair for pairs in day_pairs.values() for pair in pairs})
        if window_pairs:
            with self._session_factory() as session:
                pair_statuses = _pair_blacklist_status_map(session, brand=brand, pairs=window_pairs)
        blacklisted_by_day = {
            day: (
                sum(1 for pair in pairs if pair_statuses.get(pair) == "blacklisted")
                if _has_irrelevant_product_pairs(payload)
                else None
            )
            for day, pairs in day_pairs.items()
        }
        return {
            "brand": brand,
            "startDate": window_start.isoformat(),
            "endDate": window_end.isoformat(),
            "days": [
                _metric_trend_day(
                    day,
                    selected[day][1] if day in selected else None,
                    blacklisted_product_count=blacklisted_by_day.get(day),
                )
                for day in _date_range(window_start, window_end)
            ],
        }

    def brand_queries(
        self,
        brand: str,
        *,
        data_date: date | None = None,
        page: int = 1,
        page_size: int = 30,
        filter_key: str = "all",
    ) -> dict[str, Any]:
        """Read the Query rail without assembling the full brand workspace.

        The rail needs task cards, node issue evidence and summary stats only;
        trends, workflow telemetry, inventory checks and other detail-only
        projections are intentionally left out of this read path.
        """
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
        filter_key = str(filter_key or "all").strip().lower()
        if filter_key not in {
            "all", "waiting_human_approval", "fixed", "normal", "still_problematic",
            "recall", "result_drop", "relevance", "open_factor", "inventory",
        }:
            filter_key = "all"
        with self._session_factory() as session:
            batch_id = self._latest_batch_id(session, brand, data_date=data_date)
            if batch_id is None:
                return {
                    "brand": brand,
                    "batch": None,
                    "visibleCheckKeys": sorted(visible_check_keys(_check_policy(brand, data_date or date.today(), {}))),
                    "queries": [],
                    "total": 0,
                    "page": page,
                    "pageSize": page_size,
                    "hasNext": False,
                    "filter": filter_key,
                    "stats": {
                        "topQueryCount": 0,
                        "manualRequiredCount": 0,
                        "completedCount": 0,
                        "stepIssueCounts": {"recall": 0, "relevance": 0, "open_factor": 0, "inventory": 0},
                        "noResultOrLowResultCount": 0,
                        "resultDropCount": 0,
                        "relevanceIssueCount": 0,
                        "openFactorIssueCount": 0,
                        "inventoryProductIssueCount": 0,
                    },
                }
            run = self._workflow_run(session, batch_id)
            selection_row = session.execute(select(QueryGovernanceSelectionBatch.config_json).where(
                QueryGovernanceSelectionBatch.batch_id == batch_id,
                QueryGovernanceSelectionBatch.brand == brand,
            )).scalar_one_or_none()
            selection_config = _json_object(selection_row) or {}
            offset = (page - 1) * page_size
            summary_payload = self._overview_summary_for_batch(session, brand, batch_id) or {}
            materialized_filter_ids = summary_payload.get("filterTaskIds")
            materialized_filter_ids = (
                materialized_filter_ids
                if isinstance(materialized_filter_ids, Mapping)
                else None
            )
            if filter_key == "all" and isinstance(summary_payload.get("stats"), Mapping):
                total = self._task_count(session, batch_id, brand=brand)
                has_tasks = total > 0
                page_tasks = self._tasks_page(
                    session,
                    batch_id,
                    brand=brand,
                    offset=offset,
                    limit=page_size,
                )
                inventory_records = read_inventory_recovery_tasks(
                    session,
                    brand=brand,
                    updated_after=run.started_at if run is not None else None,
                )
                inventory_issue_queries = _inventory_issue_queries(inventory_records)
                business_stats = dict(summary_payload.get("stats") or {})
                business_stats.setdefault("topQueryCount", int(summary_payload.get("queryCount") or total))
                business_stats.setdefault("manualRequiredCount", 0)
                business_stats.setdefault("completedCount", 0)
                business_stats.setdefault("stepIssueCounts", {"recall": 0, "relevance": 0, "open_factor": 0, "inventory": 0})
                business_stats.setdefault("noResultOrLowResultCount", 0)
                business_stats.setdefault("resultDropCount", 0)
                business_stats.setdefault("relevanceIssueCount", 0)
                business_stats.setdefault("openFactorIssueCount", 0)
                business_stats.setdefault("inventoryProductIssueCount", 0)
                candidate_metrics = self._candidate_metrics_for_tasks(session, page_tasks)
                query_ranks = {}
            elif (
                materialized_filter_ids is not None
                and isinstance(materialized_filter_ids.get(filter_key), list)
            ):
                matching_task_ids = [
                    int(task_id)
                    for task_id in materialized_filter_ids[filter_key]
                    if str(task_id).isdigit()
                ]
                total = len(matching_task_ids)
                has_tasks = total > 0
                page_ids = matching_task_ids[offset:offset + page_size]
                tasks_by_id = {
                    int(task.id): task
                    for task in self._tasks_by_ids(
                        session,
                        batch_id,
                        brand=brand,
                        task_ids=page_ids,
                    )
                }
                page_tasks = [tasks_by_id[task_id] for task_id in page_ids if task_id in tasks_by_id]
                inventory_records = read_inventory_recovery_tasks(
                    session,
                    brand=brand,
                    updated_after=run.started_at if run is not None else None,
                )
                inventory_issue_queries = _inventory_issue_queries(inventory_records)
                business_stats = dict(summary_payload.get("stats") or {})
                business_stats.setdefault("topQueryCount", int(summary_payload.get("queryCount") or total))
                business_stats.setdefault("manualRequiredCount", 0)
                business_stats.setdefault("completedCount", 0)
                business_stats.setdefault("stepIssueCounts", {"recall": 0, "relevance": 0, "open_factor": 0, "inventory": 0})
                business_stats.setdefault("noResultOrLowResultCount", 0)
                business_stats.setdefault("resultDropCount", 0)
                business_stats.setdefault("relevanceIssueCount", 0)
                business_stats.setdefault("openFactorIssueCount", 0)
                business_stats.setdefault("inventoryProductIssueCount", 0)
                candidate_metrics = self._candidate_metrics_for_tasks(session, page_tasks)
                query_ranks = {}
                all_steps = self._visibility_steps_for_batch(session, batch_id, brand=brand)
            else:
                tasks = self._tasks(session, batch_id, brand=brand)
                has_tasks = bool(tasks)
                all_steps = self._steps_for_tasks(session, tasks, include_s0_result=False)
                inventory_records = read_inventory_recovery_tasks(
                    session,
                    brand=brand,
                    updated_after=run.started_at if run is not None else None,
                )
                inventory_issue_queries = _inventory_issue_queries(inventory_records)
                issue_counts = _query_issue_counts(tasks, all_steps, inventory_records)
                no_result_or_low_result_count, result_drop_count = _recall_issue_counts(tasks, all_steps)
                query_statuses = self._query_statuses(tasks, all_steps)
                metric_date = tasks[0].data_date if tasks else data_date
                metric_rows = session.execute(
                    select(BrandDailyMetric).where(
                        BrandDailyMetric.brand == brand,
                        BrandDailyMetric.data_date == metric_date,
                    ).order_by(BrandDailyMetric.id.asc())
                ).scalars().all() if metric_date is not None else []
                business_stats = _business_metric_payload(max(metric_rows, key=_metric_priority)) if metric_rows else {}
                business_stats = business_stats or {}
                business_stats.update({
                    "topQueryCount": len(tasks),
                    "manualRequiredCount": sum(1 for task in tasks if str(task.status) == "manual_required"),
                    "completedCount": sum(1 for task in tasks if str(task.status) == "completed"),
                    "stepIssueCounts": issue_counts,
                    "noResultOrLowResultCount": no_result_or_low_result_count,
                    "resultDropCount": result_drop_count,
                    "relevanceIssueCount": issue_counts["relevance"],
                    "openFactorIssueCount": issue_counts["open_factor"],
                    "inventoryProductIssueCount": issue_counts["inventory"],
                })
                candidate_metrics, candidate_ranks = self._candidate_display_metrics(session, tasks)
                ordered_tasks = self._tasks_by_pv(tasks, candidate_metrics)
                matching_task_ids = self._filtered_task_ids(
                    ordered_tasks,
                    all_steps,
                    inventory_issue_queries,
                    filter_key,
                    query_statuses=query_statuses,
                )
                total = len(matching_task_ids)
                if filter_key == "all":
                    page_tasks = ordered_tasks[offset:offset + page_size]
                else:
                    page_ids = set(matching_task_ids[offset:offset + page_size])
                    page_tasks = [task for task in ordered_tasks if int(task.id) in page_ids]
                query_ranks = {}
            page_steps = self._steps_for_tasks(
                session,
                page_tasks,
                include_s0_result=False,
            )
            governance_states_by_task = _governance_states_for_tasks(session, page_tasks)
            metric_date = (
                page_tasks[0].data_date if page_tasks else summary_payload.get("dataDate") or data_date
            )
            if metric_date is not None and not isinstance(metric_date, date):
                metric_date = date.fromisoformat(str(metric_date)[:10])
            display_date = metric_date or date.today()
            if filter_key == "all" and isinstance(summary_payload.get("stats"), Mapping):
                all_steps = self._visibility_steps_for_batch(session, batch_id, brand=brand)
            policy = _check_policy(brand, display_date, selection_config)
            inventory_scan_at = _completed_inventory_scan_at(session, brand=brand, run=run, policy=policy, data_date=metric_date)
            activity_date = None if run is not None and run.started_at is not None else display_date
            observed_inventory_queries = _inventory_activity_queries(session, inventory_records, activity_date)
            visible_checks = _visible_checks(policy, all_steps, observed_inventory_queries)
            return {
                "brand": brand,
                "dataDate": _iso(metric_date),
                "batch": {
                    "dataDate": _iso(metric_date),
                    "reason": "no_candidates" if not has_tasks else None,
                },
                "queries": [
                    self._query_list_payload(
                        task,
                        self._ordered_nodes(task.id, page_steps),
                        metrics=candidate_metrics.get((task.batch_id, task.brand, task.query_text)),
                        query_rank=query_ranks.get(
                            (str(task.batch_id), str(task.brand), str(task.query_text)),
                            task.query_rank,
                        ),
                        inventory_issue=str(task.query_text or "").strip() in inventory_issue_queries,
                        inventory_scan_at=inventory_scan_at if task.group_type != "control" else None,
                        governance_states=governance_states_by_task.get(int(task.id), {}),
                        visible_checks=_visible_checks(
                            policy,
                            [step for step in page_steps if int(step.task_id) == int(task.id)],
                            {str(task.query_text or "").strip()} & observed_inventory_queries,
                        ),
                    )
                    for task in page_tasks
                ],
                "total": total,
                "page": page,
                "pageSize": page_size,
                "hasNext": offset + len(page_tasks) < total,
                "filter": filter_key,
                "stats": business_stats,
                "visibleCheckKeys": sorted(visible_checks),
            }

    def query_workspace(self, task_id: int, *, include_trend: bool = True) -> dict[str, Any]:
        """Return the complete Query detail projection in one read."""
        return self.query_detail(task_id, include_trend=include_trend)

    def query_identity(self, task_id: int) -> dict[str, Any]:
        """Read only the task identity needed by live-result endpoints."""
        with self._session_factory() as session:
            task = session.get(QueryGovernanceTask, task_id)
            if task is None:
                raise QueryGovernanceNotFoundError("Query governance task not found")
            return {"taskId": task.id, "brand": task.brand, "queryText": task.query_text}

    def brand_detail(
        self,
        brand: str,
        *,
        data_date: date | None = None,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            batch_id = self._latest_batch_id(session, brand, data_date=data_date)
            if batch_id is None:
                business_metric = None
                if data_date is not None:
                    metric_rows = session.execute(
                        select(BrandDailyMetric).where(
                            BrandDailyMetric.brand == brand,
                            BrandDailyMetric.data_date == data_date,
                        )
                        .order_by(BrandDailyMetric.id.asc())
                    ).scalars().all()
                    if metric_rows:
                        business_metric = max(metric_rows, key=_metric_priority)
                business_stats = _business_metric_payload(business_metric) or {}
                business_stats.update({
                    "topQueryCount": 0,
                    "manualRequiredCount": 0,
                    "completedCount": 0,
                    "stepIssueCounts": {"recall": 0, "relevance": 0, "open_factor": 0, "inventory": 0},
                    "noResultOrLowResultCount": 0,
                    "resultDropCount": 0,
                    "relevanceIssueCount": 0,
                    "openFactorIssueCount": 0,
                    "inventoryProductIssueCount": 0,
                })
                visible_checks = _visible_checks(
                    _check_policy(brand, data_date or date.today(), {}),
                    (),
                )
                return {
                    "brand": brand,
                    "batch": None,
                    "summary": {
                        "queryCount": 0,
                        "queryStatusCounts": _empty_counts(),
                        "nodeStatusCounts": _empty_counts(),
                    },
                    "queries": [],
                    "visibleCheckKeys": sorted(visible_checks),
                    "slots": self._brand_slots(),
                    "stats": business_stats,
                    "trend": _business_metric_trend(session, brand, end_date=data_date),
                }
            tasks = self._tasks(session, batch_id, brand=brand)
            steps = self._steps_for_tasks(session, tasks)
            query_statuses = self._query_statuses(tasks, steps)
            run = self._workflow_run(session, batch_id)
            selection_row = session.execute(select(QueryGovernanceSelectionBatch.config_json).where(
                QueryGovernanceSelectionBatch.batch_id == batch_id,
                QueryGovernanceSelectionBatch.brand == brand,
            )).scalar_one_or_none()
            selection_config = _json_object(selection_row) or {}
            candidate_metrics, candidate_ranks = self._candidate_display_metrics(session, tasks)
            governance_states_by_task = _governance_states_for_tasks(session, tasks)
            query_counts = _empty_counts()
            for status in query_statuses.values():
                query_counts[status] += 1
            metric_date = tasks[0].data_date if tasks else data_date
            metric_rows = session.execute(
                select(BrandDailyMetric).where(
                    BrandDailyMetric.brand == brand,
                    BrandDailyMetric.data_date == metric_date,
                )
                .order_by(BrandDailyMetric.id.asc())
            ).scalars().all()
            business_metric = max(metric_rows, key=_metric_priority) if metric_rows else None

            inventory_records = read_inventory_recovery_tasks(
                session,
                brand=brand,
                updated_after=run.started_at if run is not None else None,
            )
            inventory_issue_queries = _inventory_issue_queries(inventory_records)
            metric_date = tasks[0].data_date if tasks else data_date
            display_date = metric_date or date.today()
            activity_date = None if run is not None and run.started_at is not None else display_date
            observed_inventory_queries = _inventory_activity_queries(session, inventory_records, activity_date)
            policy = _check_policy(brand, display_date, selection_config)
            inventory_scan_at = _completed_inventory_scan_at(session, brand=brand, run=run, policy=policy, data_date=metric_date)
            visible_checks = _visible_checks(policy, steps, observed_inventory_queries)
            issue_counts = _query_issue_counts(tasks, steps, inventory_records)
            no_result_or_low_result_count, result_drop_count = _recall_issue_counts(tasks, steps)

            business_stats = _business_metric_payload(business_metric) or {}
            business_stats.update({
                "topQueryCount": len(tasks),
                "manualRequiredCount": sum(1 for task in tasks if str(task.status) == "manual_required"),
                "completedCount": sum(1 for task in tasks if str(task.status) == "completed"),

                "stepIssueCounts": issue_counts,
                "noResultOrLowResultCount": no_result_or_low_result_count,
                "resultDropCount": result_drop_count,
                "relevanceIssueCount": issue_counts["relevance"],
                "openFactorIssueCount": issue_counts["open_factor"],
                "inventoryProductIssueCount": issue_counts["inventory"],
            })

            return {
                "brand": brand,
                "dataDate": _iso(metric_date),
                "batch": self._batch_payload(
                    session,
                    batch_id,
                    brand,
                    tasks,
                    query_statuses,
                    run=run,
                    step_rows=steps,
                    inventory_records=inventory_records,
                ),
                "summary": {
                    "queryCount": len(tasks),
                    "queryStatusCounts": query_counts,
                    "nodeStatusCounts": self._node_counts(steps),
                },
                "queries": [
                    self._query_payload(
                        task,
                        self._ordered_nodes(task.id, steps),
                        metrics=candidate_metrics.get((task.batch_id, task.brand, task.query_text)),
                        query_rank=candidate_ranks.get((task.batch_id, task.brand, task.query_text)),
                        inventory_issue=str(task.query_text or "").strip() in inventory_issue_queries,
                        inventory_scan_at=inventory_scan_at if task.group_type != "control" else None,
                        governance_states=governance_states_by_task.get(int(task.id), {}),
                        visible_checks=_visible_checks(
                            _check_policy(brand, display_date, selection_config),
                            [step for step in steps if int(step.task_id) == int(task.id)],
                            {str(task.query_text or "").strip()} & observed_inventory_queries,
                        ),
                    )
                    for task in tasks
                ],
                "slots": self._brand_slots(),
                "stats": business_stats,
                "visibleCheckKeys": sorted(visible_checks),
                "trend": _business_metric_trend(session, brand, end_date=metric_date),
            }

    def task_list(
        self,
        brands: Sequence[str],
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        with self._session_factory() as session:
            for brand in brands:
                batch_id = self._latest_batch_id(session, brand)
                if batch_id is None:
                    continue
                tasks = self._tasks(session, batch_id, brand=brand)
                steps = self._steps_for_tasks(session, tasks)
                candidate_metrics, candidate_ranks = self._candidate_display_metrics(session, tasks)
                governance_states_by_task = _governance_states_for_tasks(session, tasks)
                for task in tasks:
                    payload = self._query_payload(
                        task,
                        self._ordered_nodes(task.id, steps),
                        metrics=candidate_metrics.get((task.batch_id, task.brand, task.query_text)),
                        query_rank=candidate_ranks.get((task.batch_id, task.brand, task.query_text)),
                        governance_states=governance_states_by_task.get(int(task.id), {}),
                    )
                    if status is None or payload["status"] == status:
                        items.append(payload)
        items.sort(key=lambda item: (item["brand"], item["queryRank"] or 10**9, item["id"]))
        return {"items": items, "total": len(items)}

    def query_detail(self, task_id: int, *, include_trend: bool = True) -> dict[str, Any]:
        with self._session_factory() as session:
            task = session.get(QueryGovernanceTask, task_id)
            if task is None:
                raise QueryGovernanceNotFoundError("Query governance task not found")
            steps = session.execute(
                select(QueryGovernanceStepStatus).where(
                    QueryGovernanceStepStatus.task_id == task.id
                )
            ).scalars().all()
            nodes = self._ordered_nodes(task.id, steps)
            candidate_metrics, candidate_ranks = self._candidate_display_metrics(session, [task])
            actions = session.execute(
                select(QueryGovernanceActionPlan)
                .where(QueryGovernanceActionPlan.task_id == task.id)
                .order_by(QueryGovernanceActionPlan.id.asc())
            ).scalars().all()
            historical_blacklist_actions = self._historical_blacklist_actions(session, task, nodes)
            if not any(str(getattr(action, "action_type", "")) == "product_blacklist" for action in actions):
                actions = [*actions, *historical_blacklist_actions]
            retag_action = session.execute(
                select(QueryGovernanceActionPlan)
                .where(
                    QueryGovernanceActionPlan.idempotency_key
                    == f"query-governance:retag:{task.brand}:{task.data_date}",
                    QueryGovernanceActionPlan.action_type == "product_retag",
                )
                .limit(1)
            ).scalar_one_or_none()
            async_tasks = session.execute(
                select(QueryGovernanceAsyncTask)
                .where(QueryGovernanceAsyncTask.task_id == task.id)
                .order_by(QueryGovernanceAsyncTask.id.asc())
            ).scalars().all()
            contexts = session.execute(
                select(QueryGovernanceContextSnapshot)
                .where(
                    QueryGovernanceContextSnapshot.batch_id == task.batch_id,
                    QueryGovernanceContextSnapshot.brand.in_((task.brand, "__global__")),
                )
                .order_by(QueryGovernanceContextSnapshot.id.asc())
            ).scalars().all()
            events = session.execute(
                select(QueryGovernanceAgentEvent)
                .where(QueryGovernanceAgentEvent.task_id == task.id)
                .order_by(QueryGovernanceAgentEvent.created_at.asc(), QueryGovernanceAgentEvent.id.asc())
            ).scalars().all()
            run = self._workflow_run(session, task.batch_id)
            inventory_records = read_inventory_recovery_tasks(
                session,
                brand=task.brand,
                query_text=task.query_text,
                updated_after=run.started_at if run is not None else None,
            )
            inventory_action_failure = _inventory_action_failure(
                session,
                brand=task.brand,
                query=task.query_text,
                updated_after=run.started_at if run is not None else None,
            )
            inventory_checks = read_inventory_recovery_checks(
                session,
                [item.id for item in inventory_records],
            )
            snapshot_products = (self._s0_payload(task.s0_snapshot) or {}).get("products", [])
            snapshot_images = {
                str(item.get("productId")): item.get("imageUrl")
                for item in snapshot_products
                if isinstance(item, Mapping) and item.get("productId") and item.get("imageUrl")
            }
            inventory_payload = [
                self._inventory_payload(
                    item,
                    checks=inventory_checks.get(int(item.id), ()),
                    image_url=snapshot_images.get(str(item.product_id)),
                )
                for item in inventory_records
            ]
            try:
                inventory_interval = int((get_brand_business_config(task.brand) or {}).get("top_query_inventory_check_interval_minutes") or 60)
            except (TypeError, ValueError):
                inventory_interval = 60
            today = datetime.now().date()
            recovered_today_count = sum(
                1 for item in inventory_records
                if str(item.status or "").lower() == "recovered"
                and getattr(item, "updated_at", None) is not None
                and item.updated_at.date() == today
            )
            blacklist_product_statuses = _blacklist_product_statuses(
                session,
                brand=task.brand,
                query=task.query_text,
            )
            relevance = self._relevance_payload(nodes, task.query_text, async_tasks=async_tasks, actions=actions, task_id=int(task.id))
            remediation = self._relevance_remediation_payload(
                task=task,
                relevance=relevance,
                actions=[*actions, *([retag_action] if retag_action is not None else [])],
            )
            if remediation:
                relevance["remediation"] = remediation
                relevance["governance"] = remediation
            experiment = self._experiment_payload(session, task)
            model_config = self._model_config_payload(session, run)
            selection_config_row = session.execute(
                select(QueryGovernanceSelectionBatch.config_json).where(
                    QueryGovernanceSelectionBatch.batch_id == task.batch_id,
                    QueryGovernanceSelectionBatch.brand == task.brand,
                )
            ).scalar_one_or_none()
            try:
                selection_config = json.loads(selection_config_row or "{}")
            except (TypeError, ValueError):
                selection_config = {}
            policy = _check_policy(task.brand, task.data_date, selection_config)
            inventory_scan_at = _completed_inventory_scan_at(session, brand=task.brand, run=run, policy=policy, data_date=task.data_date)
            inventory_execution = inventory_action_failure or (
                {
                    "available": True,
                    "source": "independent_job",
                    "status": "success",
                    "checkedAt": _iso(inventory_scan_at),
                }
                if inventory_scan_at is not None else None
            )
            visible_checks = _visible_checks(
                policy,
                steps,
                _inventory_activity_queries(
                    session,
                    inventory_records,
                    None if run is not None and run.started_at is not None else task.data_date,
                ),
            )
            history_days = max(1, int(selection_config.get("history_days", 7))) if isinstance(selection_config, Mapping) else 7
            recall_display = self._recall_display(task, nodes, async_tasks)
            query_display = self._query_payload(
                task,
                nodes,
                metrics=candidate_metrics.get((task.batch_id, task.brand, task.query_text)),
                query_rank=candidate_ranks.get((task.batch_id, task.brand, task.query_text)),
                inventory_issue=bool(inventory_records),
                inventory_scan_at=inventory_scan_at if task.group_type != "control" else None,
                governance_states=_governance_states(actions, async_tasks),
                visible_checks=visible_checks,
            )
            inventory_scans = []
            for ev in events:
                if str(getattr(ev, "event_type", "") or "") != "inventory_scan":
                    continue
                ev_created_at = getattr(ev, "created_at", None)
                if ev_created_at is None or ev_created_at.date() != today:
                    continue
                ev_payload = _json_object(getattr(ev, "output_json", None)) or {}
                checked_at_str = ev_payload.get("checkedAt")
                if not checked_at_str:
                    checked_at_str = ev_created_at.strftime("%Y-%m-%d %H:%M")
                inventory_scans.append({
                    "checkedAt": checked_at_str or "",
                    "brokenProductCount": int(ev_payload.get("brokenProductCount", 0)),
                    "normalProductCount": int(ev_payload.get("normalProductCount", 0)),
                    "recoveredProductCount": int(ev_payload.get("recoveredProductCount", 0)),
                })
            if inventory_scans:
                inventory_scans.sort(key=lambda item: str(item.get("checkedAt") or ""), reverse=True)
            return {
                "query": query_display,
                "visibleCheckKeys": sorted(visible_checks),
                "nodes": nodes,
                "s0": self._s0_payload(task.s0_snapshot),
                "actions": [self._action_payload(action) for action in actions],
                "governancePlans": _governance_plans(
                    task,
                    nodes,
                    actions,
                    async_tasks,
                    inventory_records,
                    inventory_checks,
                    blacklist_product_statuses,
                ),
                "relevance": relevance,
                "inventory": inventory_payload,
                "inventoryExecution": inventory_execution,
                "inventoryCheckIntervalMinutes": inventory_interval,
                "recoveredTodayCount": recovered_today_count,
                "inventoryScans": inventory_scans,
                "events": [self._event_payload(item) for item in events],
                "experiment": experiment,
                "modelConfig": model_config,
                "trend": self._query_metric_trend(session, task) if include_trend else [],
                "historyDays": history_days,
                "recall": recall_display,
                "reorder": self._reorder_display(
                    task,
                    nodes,
                    query_display,
                    selection_config,
                    actions,
                ),
                "slots": {
                    "kgTasks": [self._async_task_payload(item) for item in async_tasks],
                    "sharedFacts": [self._context_payload(item) for item in contexts],
                    "preference": {"available": True},
                    "relevance": {"available": True},
                    "inventory": {
                        "available": inventory_action_failure is None and (bool(inventory_records) or inventory_scan_at is not None),
                        "source": "independent_job",
                        **({"status": inventory_execution.get("status")} if inventory_execution is not None and inventory_execution.get("status") else {}),
                    },
                    "modelConfig": {
                        "available": model_config is not None,
                        "version": run.model_config_version if run is not None else None,
                    },
                    "abTest": {
                        "available": experiment is not None,
                        "experimentId": task.experiment_id,
                        "groupType": task.group_type,
                    },
                },
            }

    @staticmethod
    def _relevance_remediation_payload(
        *,
        task: Any,
        relevance: Mapping[str, Any],
        actions: Sequence[Any],
    ) -> dict[str, Any]:
        """Project persisted remediation facts into the execution overview.

        The retag/verification worker stores one brand-day ``product_retag``
        action plan.  Query detail remains a read-only projection: it never
        triggers the worker or calls an external search endpoint.
        """
        cause_groups = relevance.get("causeGroups") or []
        if not isinstance(cause_groups, list):
            cause_groups = []
        blacklist_actions = [
            action for action in actions
            if str(getattr(action, "action_type", "")) == "product_blacklist"
        ]
        kg_actions = [
            action for action in actions
            if str(getattr(action, "action_type", "")) in {"kg_plan", "kg_tag", "kg_relation", "kg_remediation"}
        ]
        retag_action = next(
            (
                action for action in actions
                if str(getattr(action, "action_type", "")) == "product_retag"
                and str(getattr(action, "idempotency_key", "") or "").startswith(
                    f"query-governance:retag:{task.brand}:{task.data_date}"
                )
            ),
            None,
        )

        def display_status(action: Any | None, fallback: str) -> str:
            if action is None:
                return fallback
            status, label = _plan_status(
                getattr(action, "status", None),
                executor_type=getattr(action, "executor_type", None),
            )
            return label or status or fallback

        payload: dict[str, Any] = {
            "blacklistStatus": display_status(blacklist_actions[0] if blacklist_actions else None, "未执行"),
            "rootCauseGroupCount": len(cause_groups),
            "kgStrategyStatus": ("需人工复核" if any(
                isinstance(group, Mapping) and (
                    group.get("rootCause") == "uncertain" or
                    (group.get("rootCause") == "kg_issue" and (not group.get("actions") or group.get("validationErrors")))
                ) for group in cause_groups
            ) else display_status(kg_actions[0] if kg_actions else None, "无需处理")),
            "retagStatus": display_status(retag_action, "未触发"),
        }
        if retag_action is None:
            return payload if cause_groups or blacklist_actions or kg_actions else {}
        response = _json_object(getattr(retag_action, "response_json", None))
        retag = response.get("retag") if isinstance(response.get("retag"), Mapping) else {}
        summary = response.get("remediationSummary")
        if not isinstance(summary, Mapping):
            summary = {}
        verification_time = summary.get("verificationTime") or summary.get("verification_time")
        if verification_time:
            payload["verifiedAt"] = verification_time
        for source_key, target_key in (
            ("errorProductsDisappeared", "incorrectProductsDisappeared"),
            ("correctProductsHarmed", "correctProductsAffected"),
            ("beforeRecallCount", "beforeRecallCount"),
            ("afterRecallCount", "afterRecallCount"),
        ):
            if source_key in summary:
                payload[target_key] = summary[source_key]
        conclusion = summary.get("conclusion") or summary.get("finalConclusion")
        if conclusion:
            payload["finalConclusion"] = conclusion
        elif retag.get("reason"):
            payload["finalConclusion"] = retag["reason"]
        return payload

    def _recall_display(
        self,
        task: Any,
        nodes: Sequence[Mapping[str, Any]],
        async_tasks: Sequence[Any],
    ) -> dict[str, Any]:
        recall_node = next((node for node in nodes if node.get("nodeKey") == "recall"), {})
        result = _node_result(recall_node) or {}
        evidence = result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {}
        current = evidence.get("result_count", evidence.get("resultCount"))
        if not _is_token_count(current):
            s0_result = _node_result(next((node for node in nodes if node.get("nodeKey") == "s0"), {})) or {}
            current = s0_result.get("result_count", s0_result.get("resultCount"))
        previous = evidence.get("previous_result_count", evidence.get("previousResultCount"))
        conclusion = str(result.get("summary") or "").strip()
        if not conclusion:
            conclusion = f"本次召回 {int(current)} 件，数量充足，无需调整。" if _is_token_count(current) else "暂无召回数量结论。"
        strategy = self._kg_strategy_display(task, async_tasks)
        payload: dict[str, Any] = {
            "currentResultCount": int(current) if _is_token_count(current) else None,
            "previousResultCount": int(previous) if _is_token_count(previous) else None,
            "comparisonStatus": str(evidence.get("comparison_status") or evidence.get("comparisonStatus") or "not_comparable"),
            "conclusion": conclusion,
            "kgStrategy": strategy,
        }
        recall_rate = result.get("recall_rate") or evidence.get("recall_rate")
        if isinstance(recall_rate, Mapping):
            payload["recallRate"] = {
                "initialSearchRecallRate": recall_rate.get("initial_search_recall_rate"),
                "postSearchRecallRate": recall_rate.get("post_search_recall_rate"),
                "llmRecallRate": recall_rate.get("llm_recall_rate"),
                "isFullRecall": bool(recall_rate.get("is_full_recall", True)),
                "unrecalledProductIds": list(recall_rate.get("unrecalled_product_ids") or []),
                "unrecalledProducts": list(recall_rate.get("unrecalled_products") or []),
                "operatorAlert": recall_rate.get("operator_alert"),
                "taggingActions": list(recall_rate.get("tagging_actions") or []),
                "pendingActionCount": int(recall_rate.get("pending_action_count") or 0),
            }
        recall_rate_status = evidence.get("recall_rate_status") or result.get("recall_rate_status")
        if recall_rate_status:
            payload["recallRateStatus"] = str(recall_rate_status)
        return payload

    @staticmethod
    def _reorder_display(
        task: Any,
        nodes: Sequence[Mapping[str, Any]],
        query: Mapping[str, Any],
        selection_config: Mapping[str, Any] | None,
        actions: Sequence[Any] = (),
    ) -> dict[str, Any]:
        """Expose the deterministic evidence used by the reorder gate."""
        current_ctr = _number(query.get("ctr"), 0)
        recent_ctr = _number(query.get("avgCtr"), 0)
        current_cvr = _number(query.get("cvr"), 0)
        recent_cvr = _number(query.get("avgCvr"), 0)
        ctr_threshold = _number((selection_config or {}).get("alert_ctr_gap_pp"), 5)
        cvr_threshold = _number((selection_config or {}).get("alert_cvr_gap_pp"), 2)
        s0 = _node_result(next((node for node in nodes if node.get("nodeKey") == "s0"), {})) or {}
        snapshot = _json_object(getattr(task, "s0_snapshot", None)) or {}
        recall_count = s0.get("result_count", s0.get("resultCount"))
        if not isinstance(recall_count, (int, float)) or isinstance(recall_count, bool):
            recall_count = snapshot.get("result_count", snapshot.get("resultCount"))
        if not isinstance(recall_count, (int, float)) or isinstance(recall_count, bool):
            recall_count = None
        recall_count = int(recall_count) if recall_count is not None else None
        low_recall_threshold = 4
        ctr_change = round((float(current_ctr) - float(recent_ctr)) * 100, 4)
        cvr_change = round((float(current_cvr) - float(recent_cvr)) * 100, 4)
        ctr_declined = -ctr_change >= float(ctr_threshold)
        cvr_declined = -cvr_change >= float(cvr_threshold)
        requires_reorder = False
        if recall_count is not None and recall_count <= low_recall_threshold:
            conclusion = f"当前召回商品数为 {recall_count} 件，候选集过小，无需进行重排序。"
        else:
            if ctr_declined or cvr_declined:
                requires_reorder = True
                if ctr_declined and cvr_declined:
                    conclusion = (
                        f"当前 CTR、CVR 较近期分别下降 {_format_percentage_point(ctr_change)}、"
                        f"{_format_percentage_point(cvr_change)} 个百分点，均已达到告警阈值，触发重排序。"
                    )
                elif ctr_declined:
                    conclusion = (
                        f"当前 CTR 较近期下降 {_format_percentage_point(ctr_change)} 个百分点，"
                        "已达到告警阈值，触发重排序。"
                    )
                else:
                    conclusion = (
                        f"当前 CVR 较近期下降 {_format_percentage_point(cvr_change)} 个百分点，"
                        "已达到告警阈值，触发重排序。"
                    )
            else:
                conclusion = "CTR、CVR 均在正常范围，无需进行重排序。"
        reorder_node = next((node for node in nodes if node.get("nodeKey") == "reorder"), {})
        strategy_status = _reorder_strategy_status(reorder_node, requires_reorder=requires_reorder)
        execution_status = _reorder_execution_status(actions, requires_reorder=requires_reorder)
        if requires_reorder:
            conclusion = _reorder_conclusion_with_statuses(
                conclusion,
                strategy_status=strategy_status,
                execution_status=execution_status,
            )
        try:
            business_config = get_brand_business_config(task.brand) or {}
            products_limit = int(business_config.get("top_query_check_product_limit", 16))
        except (TypeError, ValueError):
            products_limit = 16
        return {
            "currentCtr": float(current_ctr),
            "recentCtr": float(recent_ctr),
            "ctrChange": ctr_change,
            "ctrAlertThreshold": float(ctr_threshold),
            "currentCvr": float(current_cvr),
            "recentCvr": float(recent_cvr),
            "cvrChange": cvr_change,
            "cvrAlertThreshold": float(cvr_threshold),
            "recallCount": recall_count,
            "lowRecallThreshold": low_recall_threshold,
            "productsLimit": products_limit,
            "conclusion": conclusion,
            "requiresReorder": requires_reorder,
            "strategyStatus": strategy_status,
            "executionStatus": execution_status,
        }

    def _kg_strategy_display(self, task: Any, async_tasks: Sequence[Any]) -> dict[str, Any] | None:
        plan = next((_json_object(item.plan_json) for item in async_tasks if item.plan_json), None)
        if not isinstance(plan, Mapping):
            return None
        actions = plan.get("actions")
        if not isinstance(actions, list):
            return None
        relation_names = {
            "902227092548485120": "从属",
            "902227138815852544": "同义",
            "902227092548485120.0": "从属",
            "902227138815852544.0": "同义",
        }
        items: list[str] = []
        tags: list[str] = []
        pending_tags: set[str] = set()

        def value(params: Mapping[str, Any], *keys: str) -> str:
            for key in keys:
                raw = params.get(key)
                if raw is not None and str(raw).strip():
                    return str(raw).strip()
            return ""

        for action in actions:
            if not isinstance(action, Mapping):
                continue
            action_type = str(action.get("actionType") or action.get("action_type") or "")
            params = action.get("params") if isinstance(action.get("params"), Mapping) else {}
            first_tag = value(params, "tagName", "tag_name", "name", "sourceTagName", "source_tag_name")
            second_tag = value(params, "responseTagName", "response_tag_name", "targetTagName", "target_tag_name")
            if first_tag and first_tag not in tags:
                tags.append(first_tag)
            if second_tag and second_tag not in tags:
                tags.append(second_tag)
            if action_type == "add_tag" and first_tag:
                pending_tags.add(first_tag)
                items.append(f"新增标签：{first_tag}")
            elif action_type == "update_tag" and first_tag:
                items.append(f"更新标签：{first_tag}")
            elif action_type in {"add_relationship", "update_relationship", "delete_relationship"} and first_tag and second_tag:
                relation_id = value(params, "relationships", "relationshipId", "relationship_id", "relationType", "relation_type")
                relation = relation_names.get(relation_id)
                if not relation:
                    continue
                verb = {"add_relationship": "建立", "update_relationship": "更新为", "delete_relationship": "删除"}[action_type]
                if action_type == "delete_relationship":
                    items.append(f"删除{first_tag}与{second_tag}的{relation}关系")
                elif action_type == "update_relationship":
                    items.append(f"将{first_tag}与{second_tag}的关系更新为{relation}")
                else:
                    items.append(f"将{first_tag}与{second_tag}{verb}{relation}关系")
        if not items and not tags:
            return None

        tag_payload: list[dict[str, Any]] = []
        for name in tags:
            result_count: int | None = None
            if self._live_search is not None:
                try:
                    response = self._live_search(task.brand, name, 1)
                    total = response.get("total_count") if isinstance(response, Mapping) else None
                    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                        result_count = total
                except Exception:
                    result_count = None
            pending = name in pending_tags
            tag_payload.append({
                "name": name,
                "status": "pending_create" if pending else "exists",
                "resultCount": None if pending else result_count,
            })
        return {"items": items, "tags": tag_payload}

    @staticmethod
    def _query_metric_trend(session: Any, task: Any) -> list[dict[str, Any]]:
        """Return the persisted daily business metrics for the selected Query.

        The rewritten task table intentionally contains workflow state only;
        historical PV/UV/CTR/CVR values remain in the frozen candidate
        selection inputs.  Reading those inputs here keeps the detail drawer
        independent from the retired Top Query tables.
        """
        snapshot_rows = session.execute(
            select(QueryMetricSnapshot)
            .where(
                QueryMetricSnapshot.brand == task.brand,
                QueryMetricSnapshot.query_text == task.query_text,
            )
            .order_by(QueryMetricSnapshot.data_date.asc(), QueryMetricSnapshot.id.asc())
        ).scalars().all()
        snapshot_by_date: dict[date, dict[str, Any]] = {
            row.data_date: {
                "date": _iso(row.data_date),
                "uv": int(row.uv or 0),
                "pv": int(row.pv or 0),
                "clickUv": int(row.click_uv or 0),
                "addToCartUv": int(row.add_to_cart_uv or 0),
                "purchaseUv": int(row.purchase_uv or 0),
                "ctr": float(row.ctr or 0),
                "atc": float(row.add_to_cart_uv / row.uv) if row.uv else None,
                "cvr": float(row.cvr or 0),
            }
            for row in snapshot_rows
        }
        if snapshot_by_date and max(snapshot_by_date) >= task.data_date:
            return [snapshot_by_date[metric_date] for metric_date in sorted(snapshot_by_date)]

        candidate_rows = session.execute(
            select(QueryGovernanceCandidate)
            .where(
                QueryGovernanceCandidate.brand == task.brand,
                QueryGovernanceCandidate.query_text == task.query_text,
                QueryGovernanceCandidate.data_date <= task.data_date,
            )
            .order_by(
                QueryGovernanceCandidate.data_date.asc(),
                QueryGovernanceCandidate.selected.asc(),
                QueryGovernanceCandidate.id.asc(),
            )
        ).scalars().all()
        candidate_by_date: dict[date, Any] = {}
        for row in candidate_rows:
            current = candidate_by_date.get(row.data_date)
            if current is None or (
                bool(row.selected) and not bool(current.selected)
            ) or (
                bool(row.selected) == bool(current.selected)
                and int(row.id or 0) > int(current.id or 0)
            ):
                candidate_by_date[row.data_date] = row

        def metric_number(metrics: Mapping[str, Any], key: str, default: float = 0.0) -> float:
            value = metrics.get(key)
            return float(_number(value, default))

        candidate_points: dict[date, dict[str, Any]] = {}
        for metric_date in sorted(candidate_by_date):
            cand = candidate_by_date[metric_date]
            metrics = _json_object(cand.metrics_json) or {}
            search_uv = metric_number(metrics, "search_uv")
            search_pv = metric_number(metrics, "search_pv", cand.target_day_pv or 0)
            click_uv = metric_number(metrics, "click_uv")
            add_to_cart_uv = metric_number(metrics, "add_to_cart_uv")
            purchase_uv = metric_number(metrics, "purchase_uv")
            default_atc = (add_to_cart_uv / search_uv) if search_uv and add_to_cart_uv else 0.0
            candidate_points[metric_date] = {
                "date": _iso(metric_date),
                "uv": int(search_uv),
                "pv": int(search_pv),
                "clickUv": int(click_uv),
                "addToCartUv": int(add_to_cart_uv),
                "purchaseUv": int(purchase_uv),
                "ctr": metric_number(metrics, "ctr", click_uv / search_uv if search_uv else 0),
                "atc": metric_number(metrics, "atc", default_atc),
                "cvr": metric_number(metrics, "cvr", purchase_uv / search_uv if search_uv else 0),
            }

        merged_by_date: dict[date, dict[str, Any]] = dict(candidate_points)
        merged_by_date.update(snapshot_by_date)
        return [merged_by_date[metric_date] for metric_date in sorted(merged_by_date)]

    def live_result(self, task_id: int, *, limit: int) -> dict[str, Any]:
        with self._session_factory() as session:
            task = session.get(QueryGovernanceTask, task_id)
            if task is None:
                raise QueryGovernanceNotFoundError("Query governance task not found")
            brand = task.brand
            query_text = task.query_text
        if self._live_search is None:
            return self._live_error(task_id, "LIVE_SEARCH_UNAVAILABLE", "实时 Mixed 未配置")
        try:
            result = self._live_search(brand, query_text, limit)
            products = result.get("products") if isinstance(result, Mapping) else None
            if result.get("success") is not True or not isinstance(products, list):
                raise ValueError("Mixed 返回格式无效")
            total = result.get("total_count")
            complete = result.get("result_complete")
            if total is None:
                result_count = None
                known = False
            elif isinstance(total, int) and not isinstance(total, bool) and total >= len(products):
                result_count = total
                known = True
            else:
                raise ValueError("Mixed 召回数量无效")
            if complete is True and result_count != len(products):
                raise ValueError("Mixed 完整结果与总数不一致")
            return {
                "taskId": task_id,
                "status": NodeStatus.EXECUTED.value,
                "resultCount": result_count,
                "resultCountKnown": known,
                "products": products,
                "errorCode": None,
                "errorMessage": None,
            }
        except Exception as exc:
            return self._live_error(task_id, "LIVE_SEARCH_FAILED", str(exc) or "实时 Mixed 失败")

    @staticmethod
    def _latest_batch_id(session: Any, brand: str, *, data_date: date | None = None) -> str | None:
        query = select(
            QueryGovernanceTask.batch_id,
            QueryGovernanceTask.data_date,
            func.count(QueryGovernanceTask.id),
            func.max(QueryGovernanceTask.id),
        ).where(QueryGovernanceTask.brand == brand)
        if data_date is not None:
            query = query.where(QueryGovernanceTask.data_date == data_date)
        task_groups = session.execute(
            query.group_by(
                QueryGovernanceTask.batch_id,
                QueryGovernanceTask.data_date,
            )
        ).all()
        grouped: dict[str, tuple[int, int, date]] = {}
        for batch_id, task_date, task_count, max_id in task_groups:
            grouped[str(batch_id)] = (
                int(task_count or 0),
                int(max_id or 0),
                task_date,
            )
        selection_query = select(
            QueryGovernanceSelectionBatch.batch_id,
            QueryGovernanceSelectionBatch.data_date,
            QueryGovernanceSelectionBatch.id,
        ).where(QueryGovernanceSelectionBatch.brand == brand)
        if data_date is not None:
            selection_query = selection_query.where(QueryGovernanceSelectionBatch.data_date == data_date)
        for batch_id, batch_date, batch_row_id in session.execute(selection_query):
            grouped.setdefault(str(batch_id), (0, int(batch_row_id), batch_date))
        if not grouped:
            return None

        return min(
            grouped,
            key=lambda batch_id: (-grouped[batch_id][2].toordinal(), QueryGovernanceReadService._batch_priority(batch_id), -grouped[batch_id][0], -grouped[batch_id][1]),
        )

    @staticmethod
    def _batch_priority(batch_id: str) -> int:
        if batch_id.startswith("query-governance:") and ":retest-" not in batch_id:
            return 0
        if batch_id.startswith("query-governance:"):
            return 1
        return 2

    @staticmethod
    def _query_task_ordering() -> tuple[Any, ...]:
        """Sort ranked tasks first without relying on dialect-specific NULLS syntax."""
        return (
            QueryGovernanceTask.query_rank.is_(None).asc(),
            QueryGovernanceTask.query_rank.asc(),
            QueryGovernanceTask.id.asc(),
        )

    @staticmethod
    def _tasks_by_pv(
        tasks: Sequence[Any],
        candidate_metrics: Mapping[tuple[str, str, str], Mapping[str, Any]],
    ) -> list[Any]:
        """Order the Query rail by the persisted business PV metric.

        ``query_rank`` is the source selection rank and can be sparse after
        filtering.  It must not determine the user-facing rail order.  The
        metric snapshot is read for the complete batch before slicing pages so
        a high-PV Query cannot appear late merely because its source rank was
        large.
        """
        return sorted(
            tasks,
            key=lambda task: (
                -float(
                    _number(
                        candidate_metrics.get(
                            (str(task.batch_id), str(task.brand), str(task.query_text)),
                            {},
                        ).get("search_pv"),
                        0,
                    )
                ),
                str(task.query_text or ""),
                int(task.id),
            ),
        )

    @staticmethod
    def _tasks(session: Any, batch_id: str, *, brand: str) -> list[Any]:
        return list(
            session.execute(
                select(QueryGovernanceTask).options(
                    load_only(
                        QueryGovernanceTask.id,
                        QueryGovernanceTask.batch_id,
                        QueryGovernanceTask.brand,
                        QueryGovernanceTask.data_date,
                        QueryGovernanceTask.query_text,
                        QueryGovernanceTask.experiment_id,
                        QueryGovernanceTask.group_type,
                        QueryGovernanceTask.query_rank,
                        QueryGovernanceTask.status,
                        QueryGovernanceTask.error_code,
                        QueryGovernanceTask.error_message,
                        QueryGovernanceTask.started_at,
                        QueryGovernanceTask.completed_at,
                        QueryGovernanceTask.created_at,
                        QueryGovernanceTask.updated_at,
                    )
                )
                .where(
                    QueryGovernanceTask.batch_id == batch_id,
                    QueryGovernanceTask.brand == brand,
                )
                .order_by(*QueryGovernanceReadService._query_task_ordering())
            ).scalars().all()
        )

    @staticmethod
    def _s0_snapshots_for_tasks(
        session: Any,
        tasks: Sequence[Any],
    ) -> dict[int, Mapping[str, Any]]:
        task_ids = list(dict.fromkeys(int(task.id) for task in tasks))
        if not task_ids:
            return {}
        rows = session.execute(
            select(QueryGovernanceTask.id, QueryGovernanceTask.s0_snapshot).where(
                QueryGovernanceTask.id.in_(task_ids)
            )
        ).all()
        return {
            int(task_id): payload
            for task_id, raw_snapshot in rows
            if isinstance((payload := _json_object(raw_snapshot)), Mapping)
        }

    @staticmethod
    def _task_count(session: Any, batch_id: str, *, brand: str) -> int:
        return int(session.execute(
            select(func.count(QueryGovernanceTask.id)).where(
                QueryGovernanceTask.batch_id == batch_id,
                QueryGovernanceTask.brand == brand,
            )
        ).scalar_one() or 0)

    @staticmethod
    def _tasks_by_ids(
        session: Any,
        batch_id: str,
        *,
        brand: str,
        task_ids: Sequence[int],
    ) -> list[Any]:
        normalized_ids = list(dict.fromkeys(int(task_id) for task_id in task_ids))
        if not normalized_ids:
            return []
        statement = select(QueryGovernanceTask).options(
            load_only(
                QueryGovernanceTask.id,
                QueryGovernanceTask.batch_id,
                QueryGovernanceTask.brand,
                QueryGovernanceTask.data_date,
                QueryGovernanceTask.query_text,
                QueryGovernanceTask.experiment_id,
                QueryGovernanceTask.group_type,
                QueryGovernanceTask.query_rank,
                QueryGovernanceTask.status,
                QueryGovernanceTask.error_code,
                QueryGovernanceTask.error_message,
                QueryGovernanceTask.started_at,
                QueryGovernanceTask.completed_at,
                QueryGovernanceTask.created_at,
                QueryGovernanceTask.updated_at,
            )
        ).where(
            QueryGovernanceTask.id.in_(normalized_ids),
            QueryGovernanceTask.batch_id == batch_id,
            QueryGovernanceTask.brand == brand,
        )
        return list(session.execute(statement).scalars().all())

    @staticmethod
    def _tasks_page(
        session: Any,
        batch_id: str,
        *,
        brand: str,
        offset: int,
        limit: int,
    ) -> list[Any]:
        """Read only the visible Query cards in persisted PV order."""
        candidate_pv = (
            select(
                QueryGovernanceCandidate.batch_id.label("candidate_batch_id"),
                QueryGovernanceCandidate.brand.label("candidate_brand"),
                QueryGovernanceCandidate.query_text.label("candidate_query_text"),
                func.max(QueryGovernanceCandidate.target_day_pv).label("target_day_pv"),
            )
            .where(
                QueryGovernanceCandidate.batch_id == batch_id,
                QueryGovernanceCandidate.brand == brand,
                QueryGovernanceCandidate.selected.is_(True),
            )
            .group_by(
                QueryGovernanceCandidate.batch_id,
                QueryGovernanceCandidate.brand,
                QueryGovernanceCandidate.query_text,
            )
            .subquery()
        )
        statement = (
            select(QueryGovernanceTask).options(
                load_only(
                    QueryGovernanceTask.id,
                    QueryGovernanceTask.batch_id,
                    QueryGovernanceTask.brand,
                    QueryGovernanceTask.data_date,
                    QueryGovernanceTask.query_text,
                    QueryGovernanceTask.experiment_id,
                    QueryGovernanceTask.group_type,
                    QueryGovernanceTask.query_rank,
                    QueryGovernanceTask.status,
                    QueryGovernanceTask.error_code,
                    QueryGovernanceTask.error_message,
                    QueryGovernanceTask.started_at,
                    QueryGovernanceTask.completed_at,
                    QueryGovernanceTask.created_at,
                    QueryGovernanceTask.updated_at,
                )
            )
            .outerjoin(
                candidate_pv,
                and_(
                    candidate_pv.c.candidate_batch_id == QueryGovernanceTask.batch_id,
                    candidate_pv.c.candidate_brand == QueryGovernanceTask.brand,
                    candidate_pv.c.candidate_query_text == QueryGovernanceTask.query_text,
                ),
            )
            .where(
                QueryGovernanceTask.batch_id == batch_id,
                QueryGovernanceTask.brand == brand,
            )
            .order_by(
                func.coalesce(candidate_pv.c.target_day_pv, 0).desc(),
                QueryGovernanceTask.query_text.asc(),
                QueryGovernanceTask.id.asc(),
            )
            .offset(max(0, offset))
            .limit(max(1, limit))
        )
        return list(session.execute(statement).scalars().all())

    @classmethod
    def _filtered_task_ids(
        cls,
        tasks: Sequence[Any],
        steps: Sequence[Any],
        inventory_issue_queries: set[str],
        filter_key: str,
        *,
        query_statuses: Mapping[int, str],
    ) -> list[int]:
        """Apply the rail filter while keeping full counts independent of the page."""
        if filter_key == "all":
            return [int(task.id) for task in tasks]
        steps_by_task: dict[int, dict[str, Any]] = {}
        for step in steps:
            steps_by_task.setdefault(int(step.task_id), {})[str(step.node_key)] = step

        matching: list[int] = []
        for task in tasks:
            task_id = int(task.id)
            task_steps = steps_by_task.get(task_id, {})
            recall = _step_issue_evidence(_node_payload_from_step(task_steps.get("recall"), "recall"))
            relevance = _step_issue_evidence(_node_payload_from_step(task_steps.get("relevance"), "relevance"))
            reorder_step = task_steps.get("reorder")
            reorder = _step_issue_evidence(_node_payload_from_step(reorder_step, "reorder"))
            reorder_status = str(getattr(reorder_step, "status", "") or "").lower()
            query = str(task.query_text or "").strip()
            issue_match = {
                "recall": bool(recall["issueTypes"]),
                "result_drop": "result_count_drop" in recall["issueTypes"],
                "relevance": bool(relevance["issueTypes"]),
                "open_factor": bool(reorder["issueTypes"]) or reorder_status in {"error", "failed", "still_problematic", "check_failed"},
                "inventory": query in inventory_issue_queries,
            }
            if filter_key in issue_match:
                matched = issue_match[filter_key]
            else:
                status = query_statuses.get(task_id, NodeStatus.PENDING.value)
                matched = {
                    "waiting_human_approval": status == NodeStatus.PENDING.value,
                    "still_problematic": status == NodeStatus.ERROR.value,
                    "normal": status not in {NodeStatus.PENDING.value, NodeStatus.ERROR.value},
                    "fixed": False,
                }.get(filter_key, False)
            if matched:
                matching.append(task_id)
        return matching

    @staticmethod
    def _steps_for_tasks(
        session: Any,
        tasks: Sequence[Any],
        *,
        include_s0_result: bool = True,
    ) -> list[Any]:
        task_ids = list(dict.fromkeys(task.id for task in tasks))
        if not task_ids:
            return []
        # The full-detail path stays a single indexed read. The overview path
        # reads node metadata and only non-S0 JSON; S0 product snapshots are
        # large and do not contribute to batch-level summary metrics.
        statement = select(QueryGovernanceStepStatus).where(
            QueryGovernanceStepStatus.task_id.in_(task_ids)
        ).order_by(QueryGovernanceStepStatus.task_id, QueryGovernanceStepStatus.id)
        if include_s0_result:
            return list(session.execute(statement).scalars().all())

        lightweight_columns = [
            QueryGovernanceStepStatus.id,
            QueryGovernanceStepStatus.task_id,
            QueryGovernanceStepStatus.node_key,
            QueryGovernanceStepStatus.status,
            QueryGovernanceStepStatus.error_code,
            QueryGovernanceStepStatus.error_message,
            QueryGovernanceStepStatus.duration_seconds,
            QueryGovernanceStepStatus.started_at,
            QueryGovernanceStepStatus.completed_at,
            QueryGovernanceStepStatus.created_at,
            QueryGovernanceStepStatus.updated_at,
        ]
        metadata_rows = session.execute(select(*lightweight_columns).where(
            QueryGovernanceStepStatus.task_id.in_(task_ids)
        ).order_by(
            QueryGovernanceStepStatus.task_id,
            QueryGovernanceStepStatus.id,
        )).mappings().all()
        result_by_id = dict(session.execute(select(
            QueryGovernanceStepStatus.id,
            QueryGovernanceStepStatus.result_json,
        ).where(
            QueryGovernanceStepStatus.task_id.in_(task_ids),
            QueryGovernanceStepStatus.node_key != "s0",
        )).all())
        return [
            SimpleNamespace(**row, result_json=result_by_id.get(row["id"]))
            for row in metadata_rows
        ]

    @staticmethod
    def _visibility_steps_for_batch(
        session: Any,
        batch_id: str,
        *,
        brand: str,
    ) -> list[Any]:
        """Read only the narrow step facts needed for batch-level visibility.

        The all-filter Query rail used to hydrate every step ORM row, which
        made MySQL read every MEDIUMTEXT ``result_json`` column for the whole
        batch.  Visibility only needs node/status and the payload of skipped
        rows (to distinguish disabled checks), so keep the wide read bounded
        to that small subset.
        """
        metadata = session.execute(
            select(
                QueryGovernanceStepStatus.id,
                QueryGovernanceStepStatus.task_id,
                QueryGovernanceStepStatus.node_key,
                QueryGovernanceStepStatus.status,
            )
            .join(
                QueryGovernanceTask,
                QueryGovernanceStepStatus.task_id == QueryGovernanceTask.id,
            )
            .where(
                QueryGovernanceTask.batch_id == batch_id,
                QueryGovernanceTask.brand == brand,
            )
        ).mappings().all()
        skipped_ids = [
            int(row["id"])
            for row in metadata
            if str(row["status"] or "").lower() == "skipped"
        ]
        result_by_id: dict[int, Any] = {}
        if skipped_ids:
            result_by_id = dict(session.execute(
                select(
                    QueryGovernanceStepStatus.id,
                    QueryGovernanceStepStatus.result_json,
                ).where(QueryGovernanceStepStatus.id.in_(skipped_ids))
            ).all())
        return [
            SimpleNamespace(
                task_id=row["task_id"],
                node_key=row["node_key"],
                status=row["status"],
                result_json=result_by_id.get(int(row["id"])),
            )
            for row in metadata
        ]

    @staticmethod
    def _workflow_run(session: Any, batch_id: str) -> Any:
        return session.execute(
            select(QueryGovernanceWorkflowRun).where(
                QueryGovernanceWorkflowRun.batch_id == batch_id
            )
        ).scalar_one_or_none()

    @staticmethod
    def _node_counts(steps: Sequence[Any]) -> dict[str, int]:
        counts = _empty_counts()
        for step in steps:
            if step.status in counts:
                counts[step.status] += 1
        return counts

    @classmethod
    def _query_statuses(cls, tasks: Sequence[Any], steps: Sequence[Any]) -> dict[int, str]:
        return {
            task.id: _aggregate_status(
                [node["status"] for node in cls._ordered_nodes(task.id, steps)]
            )
            for task in tasks
        }

    @staticmethod
    def _ordered_nodes(task_id: int, steps: Sequence[Any]) -> list[dict[str, Any]]:
        by_key = {step.node_key: step for step in steps if step.task_id == task_id}
        return [
            QueryGovernanceReadService._node_payload(by_key.get(node_key), node_key)
            for node_key in NODE_ORDER
        ]

    @staticmethod
    def _node_payload(step: Any, node_key: str) -> dict[str, Any]:
        if step is None:
            return {
                "nodeKey": node_key,
                "status": NodeStatus.PENDING.value,
                "result": None,
                "errorCode": None,
                "errorMessage": None,
                "durationSeconds": None,
                "tokenUsage": None,
                "tokenUsageMissing": False,
                "startedAt": None,
                "completedAt": None,
            }
        status = step.status if step.status in _STATUS_VALUES else NodeStatus.ERROR.value
        error_code = step.error_code
        error_message = step.error_message
        if step.status not in _STATUS_VALUES:
            error_code = "INVALID_NODE_STATUS"
            error_message = f"未知节点状态: {step.status}"
        raw = _json_object(step.result_json)
        if isinstance(raw, Mapping) and "telemetry" in raw:
            # Keep the historical result envelope intact. Consumers already
            # rely on ``result.result`` for checkpointed nodes; telemetry is a
            # parallel field and must not rewrite that contract.
            result = raw
            telemetry = raw.get("telemetry")
            if not isinstance(telemetry, Mapping):
                telemetry = {}
        else:
            result = raw
            telemetry = {}
        payload = {
            "nodeKey": node_key,
            "status": status,
            "result": result if isinstance(result, Mapping) else None,
            "errorCode": error_code,
            "errorMessage": error_message,
            "durationSeconds": step.duration_seconds,
            "tokenUsage": None,
            "tokenUsageMissing": False,
            "startedAt": _iso(step.started_at),
            "completedAt": _iso(step.completed_at),
        }
        if telemetry:
            payload["telemetry"] = dict(telemetry)
        audits, audit_missing = _node_token_audits(payload)
        explicit_zero_calls = (
            isinstance(result, Mapping)
            and result.get("modelCallAudits") == []
        ) or telemetry.get("modelCallCount") == 0
        if audits:
            usage = _token_totals([payload], allocate=True)
            payload["tokenUsage"] = usage["tokenUsage"]
            payload["tokenUsageMissing"] = audit_missing or usage["tokenUsageMissing"]
        elif explicit_zero_calls:
            payload["tokenUsage"] = 0
        else:
            payload["tokenUsageMissing"] = audit_missing
        return payload

    @staticmethod
    def _query_list_payload(
        task: Any,
        nodes: Sequence[dict[str, Any]],
        *,
        metrics: Mapping[str, Any] | None = None,
        query_rank: int | None = None,
        inventory_issue: bool = False,
        inventory_scan_at: datetime | None = None,
        governance_states: Mapping[str, Mapping[str, Any]] | None = None,
        visible_checks: set[str] | None = None,
    ) -> dict[str, Any]:
        payload = QueryGovernanceReadService._query_payload(
            task,
            nodes,
            metrics=metrics,
            query_rank=query_rank,
            inventory_issue=inventory_issue,
            inventory_scan_at=inventory_scan_at,
            governance_states=governance_states,
            visible_checks=visible_checks,
        )
        payload.pop("nodes", None)
        payload.pop("telemetry", None)
        compact_steps = []
        for step in payload.get("stepStatuses", []):
            compact_step = dict(step)
            evidence = compact_step.get("evidence")
            if isinstance(evidence, Mapping):
                compact_step["evidence"] = {
                    key: evidence[key]
                    for key in ("issueTypes", "resultCount", "irrelevantProductCount", "affectedProductCount", "checkedAt")
                    if key in evidence
                }
            compact_steps.append(compact_step)
        payload["stepStatuses"] = compact_steps
        return payload

    @staticmethod
    def _query_payload(
        task: Any,
        nodes: Sequence[dict[str, Any]],
        *,
        metrics: Mapping[str, Any] | None = None,
        query_rank: int | None = None,
        inventory_issue: bool = False,
        inventory_scan_at: datetime | None = None,
        governance_states: Mapping[str, Mapping[str, Any]] | None = None,
        visible_checks: set[str] | None = None,
    ) -> dict[str, Any]:
        execution_values = [
            float(
                node["telemetry"].get(
                    "independentExecutionSeconds",
                    node["telemetry"].get("executionSeconds"),
                )
            )
            for node in nodes
            if isinstance(node.get("telemetry"), Mapping)
            and node["telemetry"].get(
                "independentExecutionSeconds",
                node["telemetry"].get("executionSeconds"),
            ) is not None
        ]
        stage_durations = {
            str(node["nodeKey"]): round(float(node["telemetry"]["executionSeconds"]), 3)
            for node in nodes
            if isinstance(node.get("telemetry"), Mapping)
            and node["telemetry"].get("executionSeconds") is not None
        }
        round_durations: list[float] = []
        for node in nodes:
            if node.get("nodeKey") != "relevance" or not isinstance(node.get("result"), Mapping):
                continue
            relevance_result = node["result"].get("result") if isinstance(node["result"].get("result"), Mapping) else node["result"]
            rounds = relevance_result.get("rounds") if isinstance(relevance_result, Mapping) else None
            if isinstance(rounds, list):
                round_durations.extend(
                    float(item["telemetry"]["roundSeconds"])
                    for item in rounds
                    if isinstance(item, Mapping)
                    and isinstance(item.get("telemetry"), Mapping)
                    and item["telemetry"].get("roundSeconds") is not None
                )
        end_to_end = None
        if task.started_at is not None and task.completed_at is not None:
            end_to_end = round(max(0.0, (task.completed_at - task.started_at).total_seconds()), 3)
        telemetry = {
            "endToEndSeconds": end_to_end,
            "nodeWallClockSeconds": round(
                sum(float(node["durationSeconds"] or 0) for node in nodes), 3
            ),
            "independentProcessingSeconds": (
                round(sum(execution_values), 3) if execution_values else None
            ),
            "stageDurations": stage_durations,
            "roundDurations": [round(value, 3) for value in round_durations],
        }
        usage = _token_totals(nodes, allocate=True)
        # Preserve legacy integer rounding while new call-ledger allocations
        # retain fractional shares instead of inventing extra tokens.
        if not any("modelCallAudits" in (_node_result(node) or {}) for node in nodes):
            usage["tokenUsage"] = int(round(usage["tokenUsage"]))
        telemetry.update(usage)
        metrics = metrics or {}
        search_pv = _number(metrics.get("search_pv"), 0)
        search_uv = _number(metrics.get("search_uv"), 0)
        click_uv = _number(metrics.get("click_uv"), 0)
        purchase_uv = _number(metrics.get("purchase_uv"), 0)
        ctr = _number(metrics.get("ctr"), click_uv / search_uv if search_uv else 0)
        cvr = _number(metrics.get("cvr"), purchase_uv / search_uv if search_uv else 0)
        return {
            "id": task.id,
            "batchId": task.batch_id,
            "brand": task.brand,
            "dataDate": _iso(task.data_date),
            "queryText": task.query_text,
            "queryRank": query_rank if query_rank is not None else task.query_rank,
            "searchPv": int(search_pv),
            "searchUv": int(search_uv),
            "ctr": float(ctr),
            "cvr": float(cvr),
            "avgCtr": float(_number(metrics.get("history_ctr"), 0)),
            "avgCvr": float(_number(metrics.get("history_cvr"), 0)),
            "queryPvShare": float(_number(metrics.get("query_pv_share"), 0)),
            "experimentId": task.experiment_id,
            "groupType": task.group_type,
            "status": _aggregate_status([node["status"] for node in nodes]),
            "errorCode": task.error_code,
            "errorMessage": task.error_message,
            "durationSeconds": round(
                sum(float(node["durationSeconds"] or 0) for node in nodes),
                3,
            ),
            "telemetry": telemetry,
            "nodes": list(nodes),
            "visibleCheckKeys": sorted(visible_checks if visible_checks is not None else set(CHECK_KEYS)),
            "stepStatuses": [
                {
                    "checkKey": "open_factor" if node.get("nodeKey") == "reorder" else node.get("nodeKey"),
                    "status": node.get("status"),
                    "displayStatus": node.get("status"),
                    "summary": node.get("errorMessage") or (
                        (node.get("result") or {}).get("reason")
                        if isinstance(node.get("result"), Mapping)
                        else None
                    ),

                    "evidence": _step_issue_evidence(
                        node,
                        inventory_count=1 if inventory_issue and node.get("nodeKey") == "inventory" else 0,
                    ),
                    **_step_governance_status(node.get("nodeKey"), governance_states),

                }
                for node in nodes
                if node.get("nodeKey") in {"recall", "reorder", "relevance", "inventory"}
                and ("open_factor" if node.get("nodeKey") == "reorder" else node.get("nodeKey")) in (visible_checks if visible_checks is not None else set(CHECK_KEYS))
            ] + ([{
                "checkKey": "inventory",
                "status": "executed",
                "displayStatus": "executed",
                "summary": "检测到断码商品" if inventory_issue else (
                    f"最近一次巡检时间：{inventory_scan_at.strftime('%Y-%m-%d %H:%M')} ；未发现断码商品"
                    if inventory_scan_at is not None
                    else "已巡检，未发现断码商品"
                ),
                "evidence": {
                    "issueTypes": ["inventory"] if inventory_issue else [],
                    **({"checkedAt": _iso(inventory_scan_at)} if inventory_scan_at is not None else {}),
                },
                **_step_governance_status("inventory", governance_states),
            }] if (inventory_issue or inventory_scan_at is not None) and "inventory" in (visible_checks if visible_checks is not None else set(CHECK_KEYS)) and not any(node.get("nodeKey") == "inventory" for node in nodes) else []),
        }

    @staticmethod
    def _candidate_display_metrics(
        session: Any,
        tasks: Sequence[Any],
        *,
        candidate_rows: Sequence[Any] | None = None,
    ) -> tuple[dict[tuple[str, str, str], dict[str, Any]], dict[tuple[str, str, str], int]]:
        """Read persisted candidate metrics and derive the PV rank for display.

        Query governance tasks intentionally do not duplicate business traffic
        columns.  Candidate ``metrics_json`` is the frozen selection input and
        therefore the authoritative source for the historical Query metrics.
        """
        if not tasks:
            return {}, {}
        batch_ids = {str(task.batch_id) for task in tasks if task.batch_id}
        brands = {str(task.brand) for task in tasks if task.brand}
        rows = list(candidate_rows) if candidate_rows is not None else session.execute(
            select(QueryGovernanceCandidate).where(
                QueryGovernanceCandidate.batch_id.in_(batch_ids),
                QueryGovernanceCandidate.brand.in_(brands),
            )
        ).scalars().all()
        by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        rank_rows: dict[tuple[str, str], list[tuple[float, str]]] = {}
        for row in rows:
            key = (str(row.batch_id), str(row.brand), str(row.query_text))
            metrics = _json_object(row.metrics_json) or {}
            if "search_pv" not in metrics:
                metrics["search_pv"] = row.target_day_pv or 0
            by_key[key] = metrics
            rank_rows.setdefault((str(row.batch_id), str(row.brand)), []).append(
                (_number(metrics.get("search_pv"), row.target_day_pv or 0), str(row.query_text))
            )
        ranks: dict[tuple[str, str, str], int] = {}
        for batch_brand, items in rank_rows.items():
            ordered = sorted(items, key=lambda item: (-item[0], item[1]))
            for index, (_pv, query_text) in enumerate(ordered, start=1):
                ranks[(batch_brand[0], batch_brand[1], query_text)] = index
        return by_key, ranks

    @staticmethod
    def _candidate_metrics_for_tasks(
        session: Any,
        tasks: Sequence[Any],
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        """Read candidate metrics only for cards on the current page."""
        if not tasks:
            return {}
        identities = {
            (str(task.batch_id), str(task.brand), str(task.query_text))
            for task in tasks
        }
        rows = session.execute(
            select(QueryGovernanceCandidate).where(
                QueryGovernanceCandidate.batch_id.in_({item[0] for item in identities}),
                QueryGovernanceCandidate.brand.in_({item[1] for item in identities}),
                QueryGovernanceCandidate.query_text.in_({item[2] for item in identities}),
            )
        ).scalars().all()
        result: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row.batch_id), str(row.brand), str(row.query_text))
            if key not in identities:
                continue
            metrics = _json_object(row.metrics_json) or {}
            if "search_pv" not in metrics:
                metrics["search_pv"] = row.target_day_pv or 0
            result[key] = metrics
        return result

    @staticmethod
    def _batch_payload(
        session: Any,
        batch_id: str,
        brand: str,
        tasks: Sequence[Any],
        query_statuses: Mapping[int, str],
        *,
        run: Any,
        step_rows: Sequence[Any],
        inventory_records: Sequence[Any] = (),
        selection_batch: Any | None = None,
        candidate_rows: Sequence[Any] | None = None,
        include_quality_metrics_evidence: bool = False,
    ) -> dict[str, Any]:
        query_durations = [
            max(0.0, (task.completed_at - task.started_at).total_seconds())
            for task in tasks
            if task.started_at is not None and task.completed_at is not None
        ]
        query_nodes = {
            task.id: QueryGovernanceReadService._ordered_nodes(task.id, step_rows)
            for task in tasks
        }
        product_counts: list[int] = []
        round_count = 0
        for nodes in query_nodes.values():
            relevance_node = next((node for node in nodes if node.get("nodeKey") == "relevance"), None)
            result = relevance_node.get("result") if isinstance(relevance_node, Mapping) else None
            if not isinstance(result, Mapping):
                continue
            result = result.get("result") if isinstance(result.get("result"), Mapping) else result
            if isinstance(result.get("product_count"), int) and not isinstance(result.get("product_count"), bool):
                product_counts.append(int(result["product_count"]))
            rounds = result.get("rounds")
            if isinstance(rounds, list):
                round_count += len(rounds)
        independent = [
            sum(
                float(
                    node["telemetry"].get(
                        "independentExecutionSeconds",
                        node["telemetry"].get("executionSeconds") or 0,
                    )
                    or 0
                )
                for node in nodes
                if isinstance(node.get("telemetry"), Mapping)
            )
            for nodes in query_nodes.values()
            if any(isinstance(node.get("telemetry"), Mapping) for node in nodes)
        ]
        telemetry_values = [
            node.get("telemetry")
            for nodes in query_nodes.values()
            for node in nodes
            if isinstance(node.get("telemetry"), Mapping)
        ]
        batch_keys = {
            str(value.get("batchKey"))
            for value in telemetry_values
            if value.get("batchKey")
        }

        def unique_metric_total(key: str) -> float | None:
            values_by_batch: dict[str, float] = {}
            for value in telemetry_values:
                batch_key = value.get("batchKey")
                metric = value.get(key)
                if not batch_key or not isinstance(metric, (int, float)) or isinstance(metric, bool):
                    continue
                values_by_batch.setdefault(str(batch_key), float(metric))
            return round(sum(values_by_batch.values()), 3) if values_by_batch else None

        def metric_total(key: str) -> int | None:
            values = [value.get(key) for value in telemetry_values if value.get(key) is not None]
            return int(sum(int(item) for item in values)) if values else None

        def unique_metric_count(key: str) -> int | None:
            values_by_batch: dict[str, int] = {}
            unkeyed_total = 0
            found = False
            for value in telemetry_values:
                metric = value.get(key)
                if not isinstance(metric, (int, float)) or isinstance(metric, bool):
                    continue
                found = True
                batch_key = value.get("batchKey")
                if batch_key:
                    values_by_batch.setdefault(str(batch_key), int(metric))
                else:
                    unkeyed_total += int(metric)
            if not found:
                return None
            return sum(values_by_batch.values()) + unkeyed_total

        def truncation_total() -> int | None:
            event_keys: set[str] = set()
            has_event_lists = False
            fallback_values: list[tuple[str | None, int]] = []
            for value in telemetry_values:
                raw_keys = value.get("truncationKeys")
                if isinstance(raw_keys, (list, tuple, set, frozenset)):
                    has_event_lists = True
                    event_keys.update(str(key) for key in raw_keys if str(key).strip())
                    continue
                metric = value.get("truncationCount")
                if isinstance(metric, (int, float)) and not isinstance(metric, bool):
                    fallback_values.append((str(value.get("batchKey")) if value.get("batchKey") else None, int(metric)))
            by_batch: dict[str, int] = {}
            unkeyed = 0
            for batch_key, metric in fallback_values:
                if batch_key:
                    by_batch.setdefault(batch_key, metric)
                else:
                    unkeyed += metric
            if has_event_lists:
                return len(event_keys) + sum(
                    metric for batch_key, metric in by_batch.items() if batch_key not in event_keys
                ) + unkeyed
            if not fallback_values:
                return None
            return sum(by_batch.values()) + unkeyed

        def duration_total(key: str) -> float | None:
            values = [
                float(value.get(key))
                for value in telemetry_values
                if isinstance(value.get(key), (int, float)) and not isinstance(value.get(key), bool)
            ]
            return round(sum(values), 3) if values else None

        model_call_keys = {
            str(value.get("batchKey"))
            for value in telemetry_values
            if value.get("batchKey") and value.get("modelCallCount")
        }
        if selection_batch is None:
            selection_batch = session.execute(
                select(QueryGovernanceSelectionBatch).where(
                    QueryGovernanceSelectionBatch.batch_id == batch_id,
                    QueryGovernanceSelectionBatch.brand == brand,
                )
            ).scalar_one_or_none()
        candidates = (
            list(candidate_rows)
            if candidate_rows is not None
            else session.execute(
                select(QueryGovernanceCandidate).where(
                    QueryGovernanceCandidate.selection_batch_id
                    == (selection_batch.id if selection_batch is not None else -1)
                )
            ).scalars().all()
        )
        selection_config = _json_object(selection_batch.config_json) if selection_batch is not None else None
        ctr_decline_threshold = (
            float(selection_config["alert_ctr_gap_pp"])
            if isinstance(selection_config, Mapping) and isinstance(selection_config.get("alert_ctr_gap_pp"), (int, float))
            else None
        )
        metric_date = tasks[0].data_date if tasks else None
        observed_inventory_queries = {
            str(item.original_query or "").strip()
            for item in inventory_records
            if str(item.original_query or "").strip()
        }
        visible_checks = _visible_checks(
            _check_policy(brand, metric_date or date.today(), selection_config),
            step_rows,
            observed_inventory_queries,
        )
        quality_metrics = _query_quality_metrics(
            tasks,
            step_rows,
            inventory_records=inventory_records,
            visible_checks=visible_checks,
            s0_snapshots=(
                QueryGovernanceReadService._s0_snapshots_for_tasks(session, tasks)
                if include_quality_metrics_evidence else None
            ),
            governance_states_by_task=(
                _governance_states_for_tasks(session, tasks, include_execution_mode=True)
                if include_quality_metrics_evidence else None
            ),
        )
        cvr_decline_threshold = (
            float(selection_config["alert_cvr_gap_pp"])
            if isinstance(selection_config, Mapping) and isinstance(selection_config.get("alert_cvr_gap_pp"), (int, float))
            else None
        )
        rule_counts = {
            "high_pv": 0,
            "new_query": 0,
            "ctr_decline": 0,
            "cvr_decline": 0,
            "rapid_pv_growth": 0,
        }
        for candidate in candidates:
            try:
                parsed_rules = json.loads(candidate.matched_rules_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_rules = []
            raw_rules = parsed_rules if isinstance(parsed_rules, list) else []
            for rule in rule_counts:
                if rule in raw_rules and candidate.selected:
                    rule_counts[rule] += 1
        detected_product_count = 0
        reused_product_count = 0
        irrelevant_product_count = 0
        irrelevant_query_count = 0
        # Relevance detection accuracy counts (query, product) pairs, matching
        # the blacklist action granularity.  The same pair flagged again in a
        # later round of one batch counts once because its state is binary.
        irrelevant_product_pairs: list[dict[str, str]] = []
        seen_irrelevant_pairs: set[tuple[str, str]] = set()
        task_query_text = {task.id: str(task.query_text or "").strip() for task in tasks}
        for task_id, nodes in query_nodes.items():
            relevance_node = next((node for node in nodes if node.get("nodeKey") == "relevance"), None)
            result = relevance_node.get("result") if isinstance(relevance_node, Mapping) else None
            result = result.get("result") if isinstance(result, Mapping) and isinstance(result.get("result"), Mapping) else result
            if not isinstance(result, Mapping):
                continue
            detected = result.get("detected_product_ids") or result.get("detectedProductIds") or []
            reused = result.get("reused_product_ids") or result.get("reusedProductIds") or []
            irrelevant = result.get("blacklist_candidates") or result.get("blacklistCandidates") or []
            detected_product_count += len(detected) if isinstance(detected, list) else 0
            reused_product_count += len(reused) if isinstance(reused, list) else 0
            irrelevant_product_count += len(irrelevant) if isinstance(irrelevant, list) else 0
            if isinstance(irrelevant, list) and irrelevant:
                irrelevant_query_count += 1
                query_text = task_query_text.get(task_id, "")
                for item in irrelevant:
                    if not isinstance(item, Mapping):
                        continue
                    product_id = str(item.get("productId") or item.get("product_id") or "").strip()
                    if not query_text or not product_id:
                        continue
                    pair_key = (query_text, product_id)
                    if pair_key in seen_irrelevant_pairs:
                        continue
                    seen_irrelevant_pairs.add(pair_key)
                    irrelevant_product_pairs.append({"query": query_text, "productId": product_id})
        preference_adjusted_query_count = 0
        relevance_durations: list[float] = []
        preference_durations: list[float] = []
        preference_token_usage: list[int] = []
        preference_token_by_batch: dict[str, int] = {}
        for nodes in query_nodes.values():
            for node in nodes:
                if node.get("nodeKey") == "relevance":
                    telemetry = node.get("telemetry")
                    if isinstance(telemetry, Mapping):
                        execution = telemetry.get("executionSeconds")
                        batch_count = telemetry.get("batchQueryCount")
                        if isinstance(execution, (int, float)) and not isinstance(execution, bool):
                            # A batched model call is persisted on every Query.
                            # Report the per-Query share instead of summing the
                            # same wall-clock call once per Query.
                            if isinstance(batch_count, (int, float)) and not isinstance(batch_count, bool) and batch_count > 1:
                                relevance_durations.append(float(execution) / int(batch_count))
                            else:
                                relevance_durations.append(float(execution))
                            continue
                    # A node duration includes batch coordination and waiting;
                    # it is not a relevance-Agent execution measurement.
                if node.get("nodeKey") == "reorder" and node.get("durationSeconds") is not None:
                    result = node.get("result")
                    if isinstance(result, Mapping) and isinstance(result.get("result"), Mapping):
                        result = result["result"]
                    audit = result.get("preference_audit") if isinstance(result, Mapping) else None
                    if not isinstance(audit, Mapping):
                        audit = result.get("audit") if isinstance(result, Mapping) else None
                    if isinstance(audit, Mapping):
                        duration = audit.get("durationSeconds")
                        tokens = audit.get("tokenUsage")
                        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                            preference_durations.append(float(duration))
                        if _is_token_count(tokens):
                            batch_key = audit.get("batchKey") or (node.get("telemetry") or {}).get("batchKey")
                            if batch_key:
                                preference_token_by_batch.setdefault(str(batch_key), int(tokens))
                            else:
                                preference_token_usage.append(int(tokens))
                    elif node.get("durationSeconds") is not None:
                        # Backward-compatible fallback for old rows without
                        # preference audit telemetry.
                        preference_durations.append(float(node["durationSeconds"]))
            reorder = next((node for node in nodes if node.get("nodeKey") == "reorder"), None)
            result = reorder.get("result") if isinstance(reorder, Mapping) else None
            if isinstance(result, Mapping) and isinstance(result.get("result"), Mapping):
                result = result["result"]
            if isinstance(result, Mapping) and result.get("should_reorder") is True:
                preference_adjusted_query_count += 1
        inventory_records = read_inventory_recovery_tasks(
            session,
            brand=brand,
            updated_after=run.started_at if run is not None else None,
        )
        stock_bucket_reduction_product_count = len({
            str(item.product_id)
            for item in inventory_records
            if item.product_id and item.status in {"monitoring", "auto_stopped"}
        })
        token_usage = session.execute(
            select(QueryGovernanceAgentEvent.token_usage).where(
                QueryGovernanceAgentEvent.task_id.in_([task.id for task in tasks]),
                QueryGovernanceAgentEvent.token_usage.is_not(None),
            )
        ).scalars().all()

        concurrency_values = [
            int(value["maxConcurrency"])
            for value in telemetry_values
            if value.get("maxConcurrency") is not None
        ]
        wall_clock = None
        all_nodes = [node for nodes in query_nodes.values() for node in nodes]
        call_usage = _token_totals(all_nodes, allocate=False)
        has_call_records = any(
            _node_token_audits(node)[0] or "modelCallAudits" in (_node_result(node) or {})
            for node in all_nodes
        )
        if run is not None and run.started_at is not None and run.finished_at is not None:
            wall_clock = round(max(0.0, (run.finished_at - run.started_at).total_seconds()), 3)
        no_candidates = (
            selection_batch is not None and not tasks and run is None
            and not any(candidate.selected or candidate.experiment_id is not None for candidate in candidates)
        )
        inspected_product_count = detected_product_count + reused_product_count
        return {
            "batchId": batch_id,
            "brand": brand,
            "dataDate": _iso(tasks[0].data_date if tasks else selection_batch.data_date if selection_batch is not None else None),
            "status": "skipped" if no_candidates else _aggregate_batch_status(
                [node["status"] for nodes in query_nodes.values() for node in nodes],
                workflow_status=run.status if run is not None else None,
            ),
            "reason": "no_candidates" if no_candidates else None,
            "currentNode": run.current_node if run is not None else None,
            "modelConfigVersion": run.model_config_version if run is not None else None,
            "startedAt": _iso(run.started_at if run is not None else None),
            "completedAt": _iso(run.finished_at if run is not None else None),
            "telemetry": {
                "wallClockSeconds": wall_clock,
                "queryCount": len(tasks),
                "productCount": sum(product_counts) if product_counts else None,
                "batchCount": len(batch_keys) if batch_keys else None,
                "roundCount": round_count or None,
                "queryEndToEnd": _duration_stats(query_durations),
                "independentProcessing": _duration_stats(independent),
                "modelSeconds": unique_metric_total("batchModelSeconds"),
                "actionSeconds": duration_total("actionSeconds"),
                "recheckSeconds": duration_total("recheckSeconds"),
                "maxConcurrency": max(concurrency_values) if concurrency_values else None,
                "modelCallCount": len(model_call_keys) if model_call_keys else None,
                "retryCount": metric_total("retryCount"),
                "truncationCount": truncation_total(),
                "imageSuccessCount": metric_total("imageSuccessCount"),
                "imageFailureCount": metric_total("imageFailureCount"),
                "tokenUsage": int(call_usage["tokenUsage"]) if has_call_records or not token_usage else sum(int(value) for value in token_usage),
                "relevanceTokenUsage": call_usage["relevanceTokenUsage"],
                "preferenceTokenUsage": (sum(preference_token_usage) + sum(preference_token_by_batch.values())) if (preference_token_usage or preference_token_by_batch) else None,
                "detectedProductCount": detected_product_count,
                "reusedProductCount": reused_product_count,
                "inspectedProductCount": inspected_product_count,
                "irrelevantProductCount": irrelevant_product_count,
                "irrelevantProductPairCount": len(irrelevant_product_pairs),
                "irrelevantProductPairs": irrelevant_product_pairs,
                "irrelevantQueryCount": irrelevant_query_count,
                **quality_metrics,
                "qualityMetricsVersion": _QUALITY_METRICS_VERSION,
                "relevanceAccuracy": round((inspected_product_count - irrelevant_product_count) / inspected_product_count, 4) if inspected_product_count else None,
                "irrelevantRate": round(irrelevant_product_count / inspected_product_count, 4) if inspected_product_count else None,
                "preferenceAdjustedQueryCount": preference_adjusted_query_count,
                "highPvQueryCount": rule_counts["high_pv"],
                "topQueryLimit": (
                    int(selection_config["extract_limit"])
                    if isinstance(selection_config, Mapping)
                    and isinstance(selection_config.get("extract_limit"), (int, float))
                    and not isinstance(selection_config.get("extract_limit"), bool)
                    else None
                ),
                "rapidPvGrowthQueryCount": rule_counts["rapid_pv_growth"],
                "newQueryCount": rule_counts["new_query"],
                "ctrDeclineQueryCount": rule_counts["ctr_decline"],
                "cvrDeclineQueryCount": rule_counts["cvr_decline"],
                "ctrDeclineThresholdPp": ctr_decline_threshold,
                "cvrDeclineThresholdPp": cvr_decline_threshold,
                "stockBucketReductionProductCount": stock_bucket_reduction_product_count,
                "relevanceAverageSeconds": round(sum(relevance_durations) / len(relevance_durations), 3) if relevance_durations else None,
                "preferenceAverageSeconds": round(sum(preference_durations) / len(preference_durations), 3) if preference_durations else None,
            },
        }

    @staticmethod
    def _s0_payload(value: Any) -> dict[str, Any] | None:
        snapshot = _json_object(value)
        if snapshot is None:
            return None
        products = snapshot.get("products")
        normalized_products = []
        if isinstance(products, list):
            for product in products:
                if not isinstance(product, Mapping):
                    continue
                normalized_products.append(
                    {
                        "productId": product.get("product_id") or product.get("productId"),
                        "rank": product.get("rank"),
                        "productName": product.get("product_name") or product.get("productName"),
                        "imageUrl": product.get("image_url") or product.get("imageUrl"),
                        "originalPrice": product.get("original_price") or product.get("originalPrice"),
                        "discountPrice": product.get("discount_price") or product.get("discountPrice"),
                        "bucket": product.get("bucket"),
                    }
                )
        count = snapshot.get("result_count")
        if not isinstance(count, int) or isinstance(count, bool):
            count = len(normalized_products)
        return {"resultCount": count, "products": normalized_products}

    @staticmethod
    def _action_payload(action: Any) -> dict[str, Any]:
        request = _json_object(action.request_json)
        execution_mode = (
            request.get("execution_mode") or request.get("executionMode")
            if isinstance(request, Mapping)
            else None
        )
        response = _json_object(action.response_json)
        return {
            "id": action.id,
            "taskId": getattr(action, "task_id", None),
            "nodeKey": action.node_key,
            "checkKey": action.node_key,
            "toolKey": action.action_type,
            "actionType": action.action_type,
            "executionMode": execution_mode or ("human_approval" if action.action_type in {"kg_plan", "query_demote"} else "auto"),
            "executorType": getattr(action, "executor_type", None),
            "executorUserId": getattr(action, "executor_user_id", None),
            "executedAt": _iso(getattr(action, "executed_at", None)),
            "failureStage": getattr(action, "failure_stage", None),
            "status": action.status,
            "reason": action.error_message,
            "request": request,
            "requestPayload": request,
            "response": response,
            "responsePayload": response,
            "errorCode": action.error_code,
            "errorMessage": action.error_message,
            "createdAt": _iso(action.created_at),
            "updatedAt": _iso(action.updated_at),
        }

    @staticmethod
    def _historical_blacklist_actions(session: Any, task: Any, nodes: Sequence[Mapping[str, Any]]) -> list[Any]:
        """Read successful legacy blacklist facts for the governance-plan workbench."""
        relevance = _node_result(next((node for node in nodes if node.get("nodeKey") == "relevance"), {})) or {}
        candidates = relevance.get("blacklist_candidates") or relevance.get("blacklistCandidates") or []
        candidate_ids = {
            str(item.get("product_id") or item.get("productId") or "").strip()
            for item in candidates if isinstance(item, Mapping)
        }
        if not candidate_ids:
            return []
        intents = session.execute(
            select(ActionIntent)
            .where(
                ActionIntent.action_type == "product_blacklist",
                ActionIntent.brand == task.brand,
                ActionIntent.query_text == task.query_text,
                ActionIntent.status == "success",
            )
            .order_by(ActionIntent.created_at.asc(), ActionIntent.id.asc())
        ).scalars().all()
        from types import SimpleNamespace
        actions: list[Any] = []
        for intent in intents:
            payload = _json_object(intent.payload_json) or {}
            payload_ids = {
                str(value).strip()
                for value in (payload.get("product_ids") or payload.get("productIds") or [])
                if str(value).strip()
            }
            if not payload_ids.intersection(candidate_ids):
                continue
            payload = {
                **payload,
                "execution_mode": str(payload.get("execution_mode") or payload.get("executionMode") or "disabled"),
                "source": "historical_action_intent",
            }
            actions.append(SimpleNamespace(
                # ActionIntent ids are numeric and can satisfy the public
                # action contract; the historical-fact provenance is carried
                # in the response payload below.
                id=int(intent.id), node_key="relevance", action_type="product_blacklist",
                status=GovernancePlanStatus.VERIFIED.value, request_json=json.dumps(payload, ensure_ascii=False),
                response_json=json.dumps({"source": "action_intent", "factId": f"fact-{intent.id}", "authoritative": True}, ensure_ascii=False),
                error_code=None, error_message=None,
                executor_type=GovernancePlanExecutorType.AI_AUTO.value,
                executor_user_id=None,
                executed_at=intent.updated_at,
                failure_stage=None,
                created_at=intent.created_at, updated_at=intent.updated_at,
            ))
        return actions

    @classmethod
    def _relevance_payload(
        cls,
        nodes: Sequence[Mapping[str, Any]],
        query_text: str,
        async_tasks: Sequence[Any] = (),
        actions: Sequence[Any] = (),
        task_id: int | None = None,
    ) -> dict[str, Any]:
        result = next(
            (
                node.get("result")
                for node in nodes
                if node.get("nodeKey") == "relevance" and isinstance(node.get("result"), Mapping)
            ),
            None,
        )
        payload = dict(result) if isinstance(result, Mapping) else {}
        if isinstance(payload.get("result"), Mapping):
            payload = dict(payload["result"])
        blacklist = payload.get("blacklist_candidates")
        if not isinstance(blacklist, list):
            blacklist = []
        warnings = payload.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
        relevant_ids = payload.get("relevant_product_ids")
        if not isinstance(relevant_ids, list):
            relevant_ids = []
        reused_ids = payload.get("reused_product_ids")
        if not isinstance(reused_ids, list):
            reused_ids = []
        detected_ids = payload.get("detected_product_ids")
        if not isinstance(detected_ids, list):
            detected_ids = []
        requested_ids = payload.get("requested_product_ids")
        if not isinstance(requested_ids, list):
            requested_ids = []
        product_count = payload.get("product_count")
        if not isinstance(product_count, int) or isinstance(product_count, bool):
            product_count = len(requested_ids)
        cause_groups = payload.get("cause_groups") or payload.get("causeGroups") or []
        if not isinstance(cause_groups, list):
            cause_groups = []
        action, async_task = _relevance_plan_source(actions, async_tasks, task_id)
        if action is not None or async_task is not None:
            response = _json_object(getattr(action, "response_json", None)) if action is not None else _json_object(getattr(async_task, "plan_json", None))
            plan = (response.get("plan") if isinstance(response.get("plan"), Mapping) else response) if isinstance(response, Mapping) else {}
            candidate_groups = plan.get("causeGroups") or plan.get("cause_groups") or []
            cause_groups = [dict(item) for item in candidate_groups if isinstance(item, Mapping)] if isinstance(candidate_groups, list) else []
        return {
            "query": str(payload.get("query") or query_text),
            "productCount": product_count,
            "requestedProductIds": [str(item) for item in requested_ids if item],
            "reusedProductIds": [str(item) for item in reused_ids if item],
            "detectedProductIds": [str(item) for item in detected_ids if item],
            "relevantProductIds": [str(item) for item in relevant_ids if item],
            "blacklistCandidates": [dict(item) for item in blacklist if isinstance(item, Mapping)],
            "causeGroups": [dict(item) for item in cause_groups if isinstance(item, Mapping)],
            "warnings": [dict(item) for item in warnings if isinstance(item, Mapping)],
            "detectedProductCount": len({str(item) for item in detected_ids if item}),
        }

    @staticmethod
    def _inventory_payload(item: Any, *, checks: Sequence[Mapping[str, Any]] = (), image_url: str | None = None) -> dict[str, Any]:
        ordered_checks = sorted(checks, key=lambda check: str(check.get("checked_at") or ""))
        monitoring_rates = [check.get("broken_rate_percent") for check in ordered_checks if str(check.get("status") or "").lower() in {"monitoring", "auto_stopped"} and check.get("broken_rate_percent") is not None]
        payload = {
            "id": item.id,
            "productId": str(item.product_id or ""),
            "productName": item.product_name,
            "query": item.original_query,
            "lastBrokenRate": item.last_check_stock,
            "status": item.status,
            "createdAt": _iso(item.created_at),
            "updatedAt": _iso(item.updated_at),
        }
        if image_url:
            payload["imageUrl"] = image_url
        if ordered_checks:
            checked_at = ordered_checks[-1].get("checked_at")
            payload["checkedAt"] = checked_at.isoformat() if hasattr(checked_at, "isoformat") else str(checked_at or "")
        if str(item.status or "").lower() == "recovered" and monitoring_rates:
            payload["brokenRateBefore"] = monitoring_rates[-1]
            payload["recoveredAt"] = _iso(item.updated_at)
        return payload

    @staticmethod
    def _event_payload(event: Any) -> dict[str, Any]:
        return {
            "id": event.id,
            "agentName": event.agent_name,
            "nodeKey": event.node_key,
            "eventType": event.event_type,
            "input": _json_object(event.input_json),
            "output": _json_object(event.output_json),
            "errorCode": event.error_code or event.model_error_code,
            "durationSeconds": event.duration_seconds,
            "modelName": event.model_name,
            "modelRole": event.model_role,
            "fallbackUsed": event.fallback_used,
            "tokenUsage": event.token_usage,
            "createdAt": _iso(event.created_at),
        }

    @staticmethod
    def _experiment_payload(session: Any, task: Any) -> dict[str, Any] | None:
        if task.experiment_id is None:
            return None
        experiment = session.get(QueryGovernanceExperiment, task.experiment_id)
        if experiment is None:
            return None
        sample = session.execute(
            select(QueryGovernanceExperimentSample)
            .where(
                QueryGovernanceExperimentSample.experiment_id == experiment.id,
                QueryGovernanceExperimentSample.query_text == task.query_text,
            )
            .limit(1)
        ).scalar_one_or_none()
        return {
            "id": experiment.id,
            "name": experiment.experiment_name,
            "status": experiment.status,
            "groupType": task.group_type or (sample.group_type if sample is not None else None),
            "sampleRank": sample.sample_rank if sample is not None else None,
            "sampleSearchPv": sample.sample_search_pv if sample is not None else None,
            "sampleCtr": sample.sample_ctr if sample is not None else None,
            "sampleCvr": sample.sample_cvr if sample is not None else None,
        }

    @staticmethod
    def _model_config_payload(session: Any, run: Any) -> dict[str, Any] | None:
        if run is None or run.model_config_version is None:
            return None
        record = session.execute(
            select(NodeModelConfigVersion).where(
                NodeModelConfigVersion.version == run.model_config_version
            )
        ).scalar_one_or_none()
        if record is None:
            return {"version": run.model_config_version, "nodes": []}
        return {
            "version": record.version,
            "status": record.status,
            "changeNote": record.change_note,
            "nodes": [
                {
                    "nodeKey": entry.node_key,
                    "channel": entry.channel,
                    "primaryModel": entry.primary_model,
                    "fallbackModel": entry.fallback_model,
                }
                for entry in record.entries
            ],
        }

    @staticmethod
    def _async_task_payload(task: Any) -> dict[str, Any]:
        return {
            "id": task.id,
            "triggerSource": task.trigger_source,
            "status": task.status,
            "evidence": _json_object(task.evidence_json),
            "plan": _json_object(task.plan_json),
            "errorCode": task.error_code,
            "errorMessage": task.error_message,
        }

    @staticmethod
    def _context_payload(context: Any) -> dict[str, Any]:
        return {
            "contextType": context.context_type,
            "payload": _json_object(context.payload),
            "capturedAt": _iso(context.captured_at),
        }

    @staticmethod
    def _brand_slots() -> dict[str, Any]:
        return {
            "kgAgent": {"available": True},
            "sharedFacts": {"available": True},
            "preference": {"available": True},
            "relevance": {"available": True},
            "inventory": {"available": True, "source": "independent_job"},
            "modelConfig": {"available": True},
            "abTest": {"available": True},
        }

    @staticmethod
    def _live_error(task_id: int, code: str, message: str) -> dict[str, Any]:
        return {
            "taskId": task_id,
            "status": NodeStatus.ERROR.value,
            "resultCount": None,
            "resultCountKnown": False,
            "products": [],
            "errorCode": code,
            "errorMessage": message,
        }


__all__ = [
    "QueryGovernanceNotFoundError",
    "QueryGovernanceReadError",
    "QueryGovernanceReadService",
]
