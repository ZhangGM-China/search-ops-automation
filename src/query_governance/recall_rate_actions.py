"""Recall-rate whitelist remediation action gateway with execution modes.

Two durable action types follow the relevance-blacklist gateway contract:
``whitelist_tag`` (whiteListTag 打标 + retag) and ``query_whitelist``
(Query 白名单).  ``auto`` mode executes inline through the fact gateway;
``human_approval`` persists a pending plan executed later from the governed
API endpoint; ``disabled`` records the proposal without any external write.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.governance.action_gateway import (
    ActionConflictError,
    ActionExecutionFailedError,
    ActionGateway as FactActionGateway,
    ActionInProgressError,
    ActionIntent,
    ExecutionContext,
)
from src.governance.query_policy import assert_query_governance_allowed

from .models import (
    GovernancePlanExecutorType,
    GovernancePlanFailureStage,
    GovernancePlanStatus,
    NodeResult,
    NodeStatus,
    QuerySpec,
    QueryGovernanceActionPlan,
    QueryGovernanceTask,
    _utc_now_naive,
)


SUPPORTED_RECALL_RATE_ACTIONS = frozenset({"whitelist_tag", "query_whitelist"})
SUPPORTED_RECALL_RATE_MODES = frozenset({"auto", "human_approval", "disabled"})


def recall_rate_action_mode(config: Mapping[str, Any] | None, action_type: str) -> str:
    """Resolve the execution mode for one recall-rate remediation action."""
    modes = (config or {}).get("top_query_tool_modes")
    modes = modes if isinstance(modes, Mapping) else {}
    mode = str(modes.get(action_type) or "auto").strip().lower()
    return mode if mode in SUPPORTED_RECALL_RATE_MODES else "auto"


def _normalize_action(spec: QuerySpec, action: Mapping[str, Any]) -> dict[str, Any]:
    action_type = str(action.get("type") or action.get("action_type") or "").strip()
    if action_type not in SUPPORTED_RECALL_RATE_ACTIONS:
        raise ValueError(f"unsupported recall-rate action type: {action_type or 'empty'}")
    product_ids = [
        str(item).strip()
        for item in (action.get("product_ids") or [])
        if str(item or "").strip()
    ]
    if not product_ids:
        raise ValueError(f"{action_type} requires product_ids")
    tag = str(action.get("tag") or "").strip()
    if action_type == "whitelist_tag" and not tag:
        raise ValueError("whitelist_tag requires tag")
    payload: dict[str, Any] = {
        "type": action_type,
        "brand": spec.brand,
        "query": spec.query,
        "product_ids": list(dict.fromkeys(product_ids)),
    }
    if tag:
        payload["tag"] = tag
    return payload


def _idempotency_key(spec: QuerySpec, payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        {
            "brand": spec.brand,
            "query": spec.query,
            "batch_id": spec.batch_id,
            "type": payload["type"],
            "tag": payload.get("tag") or "",
            "product_ids": payload["product_ids"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "recall-rate:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RecallRateActionPlanRepository:
    """Project recall-rate remediation actions into the query read model."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def create_or_get(
        self,
        spec: QuerySpec,
        action: Mapping[str, Any],
        idempotency_key: str,
        *,
        status: str = GovernancePlanStatus.EXECUTING.value,
    ) -> int | None:
        batch_id = str(spec.batch_id or "").strip()
        if not batch_id:
            return None
        with self._session_factory() as session:
            existing = session.execute(
                select(QueryGovernanceActionPlan).where(
                    QueryGovernanceActionPlan.idempotency_key == idempotency_key
                )
            ).scalar_one_or_none()
            if existing is not None:
                return int(existing.id)
            task = session.execute(
                select(QueryGovernanceTask).where(
                    QueryGovernanceTask.batch_id == batch_id,
                    QueryGovernanceTask.brand == spec.brand,
                    QueryGovernanceTask.query_text == spec.query,
                )
            ).scalar_one_or_none()
            if task is None:
                return None
            row = QueryGovernanceActionPlan(
                task_id=task.id,
                batch_id=batch_id,
                brand=spec.brand,
                query_text=spec.query,
                node_key="recall",
                action_type=str(action["type"]),
                idempotency_key=idempotency_key,
                status=status,
                executor_type=(
                    GovernancePlanExecutorType.HUMAN.value
                    if status == GovernancePlanStatus.PENDING.value
                    else GovernancePlanExecutorType.AI_AUTO.value
                ),
                request_json=json.dumps(
                    dict(action), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.execute(
                    select(QueryGovernanceActionPlan).where(
                        QueryGovernanceActionPlan.idempotency_key == idempotency_key
                    )
                ).scalar_one_or_none()
                return int(existing.id) if existing is not None else None
            session.refresh(row)
            return int(row.id)

    def create_pending(self, spec: QuerySpec, action: Mapping[str, Any]) -> int | None:
        """Persist a human-approval plan without invoking any external write."""
        payload = dict(action)
        payload["execution_mode"] = "human_approval"
        key = "recall-rate-pending:" + _idempotency_key(spec, payload)[len("recall-rate:"):]
        return self.create_or_get(
            spec, payload, key, status=GovernancePlanStatus.PENDING.value
        )

    def record_result(
        self,
        plan_id: int | None,
        *,
        status: str,
        response: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        executor_type: str | None = None,
        executor_user_id: str | None = None,
        failure_stage: str | None = None,
    ) -> None:
        if plan_id is None:
            return
        with self._session_factory() as session:
            row = session.get(QueryGovernanceActionPlan, plan_id)
            if row is None:
                return
            row.status = GovernancePlanStatus(status).value
            if executor_type is not None:
                row.executor_type = GovernancePlanExecutorType(executor_type).value
            if executor_user_id is not None:
                row.executor_user_id = str(executor_user_id)
            if failure_stage is not None:
                row.failure_stage = GovernancePlanFailureStage(failure_stage).value
            if row.status not in {
                GovernancePlanStatus.PENDING.value,
                GovernancePlanStatus.EXECUTING.value,
                GovernancePlanStatus.ROLLBACK_PENDING.value,
            }:
                row.executed_at = _utc_now_naive()
            row.response_json = (
                json.dumps(dict(response), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if response is not None
                else None
            )
            row.error_code = error_code
            row.error_message = error_message
            session.commit()


class RecallRateActionGateway:
    """Execute or defer one recall-rate whitelist remediation action."""

    def __init__(
        self,
        *,
        operational_service: Any,
        fact_gateway: FactActionGateway | None = None,
        action_plan_repository: RecallRateActionPlanRepository | None = None,
        operator: str = "scheduler:query_governance",
        permission: str = "execute_governance_actions",
    ) -> None:
        self.operational = operational_service
        self.fact_gateway = fact_gateway or FactActionGateway(
            authorizer=lambda _intent: True
        )
        self.action_plan_repository = action_plan_repository
        self.operator = operator
        self.permission = permission

    def execute_and_verify(
        self,
        spec: QuerySpec,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        plan_id: int | None = None,
        executor_type: str | None = None,
        executor_user_id: str | None = None,
    ) -> NodeResult:
        del context
        executor_kwargs = {
            "executor_type": executor_type,
            "executor_user_id": executor_user_id,
        }
        try:
            payload = _normalize_action(spec, action)
        except ValueError as exc:
            return NodeResult.error("RECALL_RATE_ACTION_INVALID", str(exc))
        idempotency_key = _idempotency_key(spec, payload)
        payload["idempotency_key"] = idempotency_key
        if plan_id is None:
            try:
                plan_id = (
                    self.action_plan_repository.create_or_get(spec, payload, idempotency_key)
                    if self.action_plan_repository
                    else None
                )
            except Exception as exc:
                return NodeResult.error("ACTION_PLAN_PERSIST_FAILED", _error_text(exc))

        intent = ActionIntent(
            action_type=payload["type"],
            idempotency_key=idempotency_key,
            payload=payload,
            operator=self.operator,
            brand=spec.brand,
            release=str(os.getenv("AIOPS_RELEASE") or "dev"),
            permission=self.permission,
            query_text=spec.query,
            target=f"{spec.brand}:{spec.query}",
            idempotency_payload={
                "type": payload["type"],
                "brand": payload["brand"],
                "query": payload["query"],
                "product_ids": payload["product_ids"],
                "batch_id": spec.batch_id,
            },
        )

        def execute(execution_context: ExecutionContext) -> Mapping[str, Any]:
            started = time.perf_counter()
            assert_query_governance_allowed(spec.brand, spec.query)
            if payload["type"] == "whitelist_tag":
                write_ok = self.operational.add_whitelist_tags(
                    spec.brand,
                    ";".join(payload["product_ids"]),
                    [payload["tag"]],
                    external_request_id=execution_context.external_request_id,
                )
                if write_ok is not True:
                    raise RuntimeError("白名单标签接口执行失败")
                # retag 让标签即时生效；失败不否定已完成的打标写回。
                retag_ok = self.operational.retag_selected_products(
                    spec.brand,
                    ";".join(payload["product_ids"]),
                    external_request_id=execution_context.external_request_id,
                )
                response: dict[str, Any] = {
                    "status": "success",
                    "action_type": "whitelist_tag",
                    "tag": payload["tag"],
                    "product_ids": list(payload["product_ids"]),
                    "retag_status": "success" if retag_ok else "skipped_or_failed",
                    "external_request_id": execution_context.external_request_id,
                    "writeSeconds": round(max(0.0, time.perf_counter() - started), 6),
                }
                return response
            write_ok = self.operational.add_to_whitelist(
                spec.brand,
                ";".join(payload["product_ids"]),
                [spec.query],
                external_request_id=execution_context.external_request_id,
            )
            if write_ok is not True:
                raise RuntimeError("Query 白名单接口执行失败")
            return {
                "status": "success",
                "action_type": "query_whitelist",
                "query": spec.query,
                "product_ids": list(payload["product_ids"]),
                "external_request_id": execution_context.external_request_id,
                "writeSeconds": round(max(0.0, time.perf_counter() - started), 6),
            }

        try:
            result = self.fact_gateway.execute(intent, execute)
        except ActionExecutionFailedError as exc:
            failure = exc.result if isinstance(exc.result, Mapping) else {}
            self._record(
                plan_id,
                status=GovernancePlanStatus.FAILED.value,
                response=dict(failure),
                error_code="RECALL_RATE_ACTION_FAILED",
                error_message=str(failure.get("error") or "召回率治理动作执行失败"),
                failure_stage=GovernancePlanFailureStage.EXECUTION.value,
                **executor_kwargs,
            )
            return NodeResult(
                status=NodeStatus.ERROR,
                result={"gateway": dict(failure)},
                error_code="RECALL_RATE_ACTION_FAILED",
                error_message=str(failure.get("error") or "召回率治理动作执行失败"),
                action=payload,
            )
        except (ActionConflictError, ActionInProgressError) as exc:
            self._record(
                plan_id,
                status=GovernancePlanStatus.FAILED.value,
                error_code="RECALL_RATE_ACTION_IDEMPOTENCY_CONFLICT",
                error_message=str(exc),
                failure_stage=GovernancePlanFailureStage.EXECUTION.value,
                **executor_kwargs,
            )
            return NodeResult.error("RECALL_RATE_ACTION_IDEMPOTENCY_CONFLICT", str(exc))
        except Exception as exc:
            self._record(
                plan_id,
                status=GovernancePlanStatus.FAILED.value,
                error_code="RECALL_RATE_ACTION_FAILED",
                error_message=_error_text(exc),
                failure_stage=GovernancePlanFailureStage.EXECUTION.value,
                **executor_kwargs,
            )
            return NodeResult.error("RECALL_RATE_ACTION_FAILED", _error_text(exc))

        if not isinstance(result, Mapping) or str(result.get("status") or "") != "success":
            failure = dict(result) if isinstance(result, Mapping) else {}
            self._record(
                plan_id,
                status=GovernancePlanStatus.EXECUTION_UNKNOWN.value,
                response=failure or None,
                error_code="RECALL_RATE_ACTION_RESULT_INVALID",
                error_message="召回率治理动作结果无效",
                failure_stage=GovernancePlanFailureStage.VERIFICATION.value,
                **executor_kwargs,
            )
            return NodeResult.error(
                "RECALL_RATE_ACTION_RESULT_INVALID", "召回率治理动作结果无效"
            )
        self._record(
            plan_id,
            status=GovernancePlanStatus.VERIFIED.value,
            response=dict(result),
            **executor_kwargs,
        )
        return NodeResult.executed(
            {"gateway": dict(result), "idempotency_key": idempotency_key, "plan_id": plan_id},
            action=payload,
        )

    def defer(self, spec: QuerySpec, action: Mapping[str, Any], context: Mapping[str, Any]) -> NodeResult:
        del context
        try:
            payload = _normalize_action(spec, action)
            plan_id = (
                self.action_plan_repository.create_pending(spec, payload)
                if self.action_plan_repository
                else None
            )
            return NodeResult.executed(
                {"status": "pending_approval", "plan_id": plan_id}, action=payload
            )
        except Exception as exc:
            return NodeResult.error("ACTION_PLAN_PERSIST_FAILED", _error_text(exc))

    def _record(self, plan_id: int | None, **kwargs: Any) -> None:
        if self.action_plan_repository:
            try:
                self.action_plan_repository.record_result(plan_id, **kwargs)
            except Exception:
                # 计划状态记录失败不回滚已发生的受控外部写；结果由回读核对。
                pass


def execute_pending_recall_rate_action(
    action_id: int,
    *,
    session_factory: Callable[[], Any],
    operational_service: Any,
    operator: str,
    user_id: str | None = None,
    gateway: RecallRateActionGateway | None = None,
) -> dict[str, Any]:
    """Execute one pending human-approval plan through the same governed path.

    Called by the ``/api/query-governance/actions/{action_id}/execute``
    endpoint after an operator confirms a deferred whitelist remediation.
    """
    with session_factory() as session:
        plan = session.get(QueryGovernanceActionPlan, action_id)
        if plan is None or str(plan.action_type or "") not in SUPPORTED_RECALL_RATE_ACTIONS:
            raise KeyError(f"recall-rate action plan not found: {action_id}")
        status = str(plan.status or "").strip().lower()
        if status not in {
            GovernancePlanStatus.PENDING.value,
            GovernancePlanStatus.EXECUTING.value,
        }:
            raise ValueError(
                f"action cannot be executed from status: {status or 'unknown'}"
            )
        request = json.loads(plan.request_json or "{}")
        if not isinstance(request, Mapping):
            raise ValueError("recall-rate action request payload invalid")
        task = session.get(QueryGovernanceTask, plan.task_id)
        spec = QuerySpec(
            query_id=str(plan.task_id),
            brand=str(plan.brand or (task.brand if task else "") or ""),
            query=str(plan.query_text or (task.query_text if task else "") or ""),
            batch_id=str(plan.batch_id or ""),
        )
    plan_gateway = gateway or RecallRateActionGateway(
        operational_service=operational_service,
        action_plan_repository=RecallRateActionPlanRepository(session_factory),
        operator=operator,
    )
    outcome = plan_gateway.execute_and_verify(
        spec,
        dict(request),
        {},
        plan_id=action_id,
        executor_type=GovernancePlanExecutorType.HUMAN.value,
        executor_user_id=user_id,
    )
    if outcome.status is NodeStatus.ERROR:
        raise RuntimeError(outcome.error_message or "召回率治理动作执行失败")
    return {
        "status": "success",
        "action_id": action_id,
        "action_type": request.get("type"),
        "product_ids": list(request.get("product_ids") or []),
    }


def _error_text(exc: Exception) -> str:
    return str(exc).strip() or exc.__class__.__name__


__all__ = [
    "RecallRateActionGateway",
    "RecallRateActionPlanRepository",
    "SUPPORTED_RECALL_RATE_ACTIONS",
    "execute_pending_recall_rate_action",
    "recall_rate_action_mode",
]
