"""Governed execution of structured Query Governance KG plans."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select

from src.governance.action_gateway import (
    ActionAuthorizationError,
    ActionConflictError,
    ActionExecutionFailedError,
    ActionGateway,
    ActionInProgressError,
    ActionIntent,
    ExecutionContext,
)

from .models import (
    GovernancePlanExecutorType,
    GovernancePlanFailureStage,
    GovernancePlanStatus,
    QueryGovernanceActionPlan,
)
from .relevance_remediation import SUPPORTED_KG_ACTIONS
from .kg_action_contract import normalize_kg_action


_ACTION_TYPE_TO_CLIENT_ACTION = {
    "compose_word": "update_tag",
    "secondary_segmentation": "update_tag",
    "homophone": "update_tag",
}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _success(response: Any) -> bool:
    if response is True:
        return True
    if not isinstance(response, Mapping):
        return False
    code = response.get("code")
    message = str(response.get("message") or response.get("msg") or "").strip().lower()
    return code in {0, 1, 200} or message in {"success", "ok", "dry_run_success"}


def _step_reference(value: Any) -> int | None:
    if isinstance(value, str) and value.startswith("@step:"):
        raw = value.removeprefix("@step:")
        if not raw.isdigit() or int(raw) < 1:
            raise ValueError("invalid step reference")
        return int(raw)
    return None


def _resolve_step_references(value: Any, completed: Sequence[Any]) -> Any:
    reference = _step_reference(value)
    if reference is not None:
        if reference < 1 or reference > len(completed):
            raise ValueError(f"step reference @{reference} has no completed result")
        result = completed[reference - 1]
        readback = result.get("readback") if isinstance(result, Mapping) else None
        identity = readback.get("tagNodeId") if isinstance(readback, Mapping) else None
        if not identity:
            raise ValueError(f"step reference @{reference} has no authoritative tag ID")
        return str(identity)
    if isinstance(value, Mapping):
        return {key: _resolve_step_references(item, completed) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_step_references(item, completed) for item in value]
    return value


def _step_references(value: Any) -> set[int]:
    if isinstance(value, Mapping):
        return {ref for item in value.values() for ref in _step_references(item)}
    if isinstance(value, list):
        return {ref for item in value for ref in _step_references(item)}
    reference = _step_reference(value)
    return {reference} if reference is not None else set()


def _node_id(value: Any) -> str:
    return str(value or "").removeprefix("kg_tag/")


def _verify_readback(request: Mapping[str, Any], observed: Any) -> dict[str, Any]:
    """Compare queried graph state with the write intent, never the write receipt."""
    if not isinstance(observed, Mapping) or observed.get("snapshotSource"):
        raise ValueError("KG readback has no authoritative graph query")
    if "code" in observed and not _success(observed):
        raise ValueError("KG readback query failed")
    rows = observed.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("KG readback is not a record list")
    if int(observed.get("pages") or 1) > 1 or int(observed.get("count") or len(rows)) > len(rows):
        raise ValueError("KG readback query is incomplete")
    if len(rows) >= 30 and "pages" not in observed and "count" not in observed:
        raise ValueError("KG readback pagination is unknown")
    params = request["params"]
    action_type = request["actionType"]
    is_tag = action_type in {"add_tag", "update_tag", "blacklist_tag"}
    if params.get("id"):
        matches = [row for row in rows if _node_id(row.get("id")) == _node_id(params["id"])]
    elif is_tag:
        matches = [row for row in rows if (row.get("name") or row.get("tagName")) == params.get("name")]
    else:
        matches = [row for row in rows if row.get("tagName") == params.get("tagName")
                   and row.get("responseTagName") == params.get("responseTagName")]
    if action_type == "delete_relationship":
        if matches:
            raise ValueError("KG relationship still exists after delete")
        return {"status": "success", "snapshot": observed}
    if len(matches) != 1:
        raise ValueError("KG readback requires exactly one matching record")
    row = matches[0]
    aliases = {"name": "tagName", "tagId": "parent", "responseTagId": "child",
               "dimensionNames": "tagDimensions", "privateIds": "privateId"}
    for key, expected in params.items():
        if key in {"id", "brand"}:
            continue
        actual = row.get(key)
        if actual is None:
            actual = row.get(aliases.get(key, key))
        if key in {"tagId", "responseTagId"} and not is_tag:
            actual, expected = _node_id(actual), _node_id(expected)
        if isinstance(actual, list) and not isinstance(expected, list):
            expected = [expected]
        if isinstance(expected, list):
            equal = isinstance(actual, list) and sorted(map(str, actual)) == sorted(map(str, expected))
        else:
            equal = actual is not None and str(actual) == str(expected)
        if not equal:
            raise ValueError(f"KG readback field mismatch: {key}")
    result = {"status": "success", "snapshot": observed}
    if action_type == "add_tag":
        identity = _node_id(row.get("id") or row.get("key") or row.get("_key"))
        if not identity:
            raise ValueError("KG readback has no graph node ID")
        result["tagNodeId"] = identity
    return result


class QueryGovernanceKGActionExecutor:
    """Run one persisted KG plan through the shared action-fact gateway.

    A plan can contain several cause groups.  ``group_id`` makes the UI's
    per-group button idempotent and lets the plan keep independent group
    statuses until all groups reach a terminal state.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        kg_client: Any,
        authorizer: Callable[[ActionIntent], bool] | None = None,
        clock: Callable[[], datetime] = datetime.now,
        operator: str = "scheduler:query_governance",
        release: str | None = None,
        permission: str = "execute_governance_actions",
        plan_guard: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.kg_client = kg_client
        self.clock = clock
        self.operator = operator
        self.release = release or str(os.getenv("AIOPS_RELEASE") or "dev")
        self.permission = permission
        self.plan_guard = plan_guard
        self.fact_gateway = ActionGateway(
            session_factory=session_factory,
            authorizer=authorizer or (lambda _intent: True),
        )

    def execute(
        self,
        plan_id: int,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
        executor_type: str = GovernancePlanExecutorType.HUMAN.value,
    ) -> dict[str, Any]:
        plan = self._read_plan(plan_id)
        response = _json_object(plan.response_json)
        groups = response.get("causeGroups") or response.get("cause_groups") or []
        if not isinstance(groups, list):
            groups = []
        group = self._select_group(groups, group_id)
        mode = (group or response).get("executionMode") or (group or response).get("execution_mode")
        if mode == "disabled":
            return {"status": "skipped", "reason": "execution_disabled", "groupId": self._group_id(group)}
        if mode in {"human_approval", "dry_run"} and executor_type == GovernancePlanExecutorType.AI_AUTO.value:
            return {"status": "skipped", "reason": "human_approval_required", "groupId": self._group_id(group)}
        if group is not None and str(group.get("rootCause") or group.get("root_cause")) == "individual":
            return {"status": "skipped", "reason": "individual_requires_blacklist_only", "groupId": group.get("groupId") or group.get("group_id")}
        if group is not None and str(group.get("rootCause") or group.get("root_cause")) == "uncertain":
            return {"status": "skipped", "reason": "root_cause_requires_confirmation", "groupId": group.get("groupId") or group.get("group_id")}

        actions = self._actions_for_group(response, group)
        if not actions:
            return {"status": "skipped", "reason": "no_kg_actions", "groupId": self._group_id(group)}
        if self.plan_guard is not None:
            guard_payload = {**response, "brand": plan.brand}
            if group is not None:
                guard_payload.update({
                    "core_tags": group.get("coreTags") or group.get("core_tags"),
                })
                group_digest = group.get("graphContextDigest") or group.get("graph_context_digest")
                group_context = group.get("graphContext") or group.get("graph_context")
                if group_digest:
                    guard_payload["graph_context_digest"] = group_digest
                elif group_context:
                    guard_payload["graph_context"] = group_context
            try:
                self.plan_guard.assert_current(guard_payload)
            except Exception as exc:
                return self._fail(
                    plan_id,
                    group_id=self._group_id(group),
                    executor_type=executor_type,
                    executor_user_id=user_id,
                    error_code=str(getattr(exc, "error_code", None) or "KG_PLAN_CONFLICT"),
                    error_message=str(exc).strip() or "知识图谱状态已变化",
                    execution_rows=[],
                    failure_stage=GovernancePlanFailureStage.VERIFICATION.value,
                )
        normalized = self._normalize_actions(actions)
        self._set_group_status(
            plan_id,
            group_id=self._group_id(group),
            status=GovernancePlanStatus.EXECUTING.value,
            executor_type=executor_type,
            executor_user_id=user_id,
        )

        execution_rows: list[dict[str, Any]] = []
        completed_results: list[Any] = []
        for step_index, action in enumerate(normalized, start=1):
            action_type = str(action["actionType"])
            client_action_type = _ACTION_TYPE_TO_CLIENT_ACTION.get(action_type, action_type)
            try:
                resolved_params = _resolve_step_references(action["params"], completed_results)
            except ValueError as exc:
                return self._fail(
                    plan_id, group_id=self._group_id(group), executor_type=executor_type,
                    executor_user_id=user_id, error_code="KG_ACTION_REFERENCE_FAILED",
                    error_message=str(exc), execution_rows=execution_rows,
                    failure_stage=GovernancePlanFailureStage.VERIFICATION.value,
                )
            payload = {"brand": plan.brand, "actionType": client_action_type, "params": resolved_params}
            if action_type == "compose_word":
                payload["params"].setdefault("isCompose", "1")
            elif action_type == "secondary_segmentation":
                payload["params"].setdefault("segmentingAgain", "1")
            elif action_type == "homophone":
                payload["params"].setdefault("homophonic", payload["params"].get("homophone", "1"))
            key = f"query-governance:kg:{plan.id}:{self._group_id(group) or 'plan'}:{step_index}"
            intent = ActionIntent(
                action_type=f"kg_{client_action_type}",
                idempotency_key=key,
                payload=payload,
                idempotency_payload={
                    "actionType": client_action_type,
                    "brand": plan.brand,
                    "params": dict(payload["params"]),
                },
                operator=self.operator if executor_type == GovernancePlanExecutorType.AI_AUTO.value else str(user_id or self.operator),
                brand=plan.brand,
                release=self.release,
                permission=self.permission,
                query_text=plan.query_text,
                target=f"{plan.brand}:{plan.query_text}",
            )

            step_result: dict[str, Any] = {}

            def execute_one(context: ExecutionContext, request=payload) -> dict[str, Any]:
                result = self.kg_client.execute(request, external_request_id=context.external_request_id)
                if not _success(result):
                    raise RuntimeError(str((result or {}).get("message") if isinstance(result, Mapping) else result))
                step_result.update({"status": "success", "response": dict(result) if isinstance(result, Mapping) else result})
                return step_result

            def readback(_context: ExecutionContext, request=payload) -> dict[str, Any]:
                try:
                    observed = self.kg_client.snapshot(request)
                    verified = _verify_readback(request, observed)
                except Exception as exc:
                    return {"status": "failed", "reason": str(exc) or exc.__class__.__name__}
                step_result["readback"] = verified
                return {**verified, "readback": verified}

            try:
                result = self.fact_gateway.execute(
                    intent,
                    execute_one,
                    readback=readback,
                    authoritative_readback_on_success=True,
                )
            except ActionExecutionFailedError as exc:
                error = _json_object(getattr(exc, "result", None))
                return self._fail(
                    plan_id,
                    group_id=self._group_id(group),
                    executor_type=executor_type,
                    executor_user_id=user_id,
                    error_code="KG_ACTION_EXECUTION_FAILED",
                    error_message=str(error.get("error") or "知识图谱动作执行失败"),
                    execution_rows=execution_rows,
                )
            except (ActionAuthorizationError, ActionConflictError, ActionInProgressError) as exc:
                return self._fail(
                    plan_id,
                    group_id=self._group_id(group),
                    executor_type=executor_type,
                    executor_user_id=user_id,
                    error_code="KG_ACTION_EXECUTION_UNKNOWN" if isinstance(exc, ActionInProgressError) else "KG_ACTION_GATEWAY_FAILED",
                    error_message=str(exc),
                    execution_rows=execution_rows,
                    status=GovernancePlanStatus.EXECUTION_UNKNOWN.value if isinstance(exc, ActionInProgressError) else GovernancePlanStatus.FAILED.value,
                )
            if not isinstance(result, Mapping) or str(result.get("status") or "") != "success":
                return self._fail(
                    plan_id,
                    group_id=self._group_id(group),
                    executor_type=executor_type,
                    executor_user_id=user_id,
                    error_code="KG_ACTION_READBACK_FAILED",
                    error_message="知识图谱动作权威回读未确认成功",
                    execution_rows=execution_rows,
                    status=GovernancePlanStatus.EXECUTION_UNKNOWN.value,
                )
            execution_rows.append({"step": step_index, "actionType": action_type, "result": dict(result)})
            completed_results.append(result)

        return self._succeed(
            plan_id,
            group_id=self._group_id(group),
            executor_type=executor_type,
            executor_user_id=user_id,
            execution_rows=execution_rows,
            groups=groups,
        )

    @staticmethod
    def _group_id(group: Mapping[str, Any] | None) -> str | None:
        if group is None:
            return None
        return str(group.get("groupId") or group.get("group_id") or "").strip() or None

    @classmethod
    def _select_group(cls, groups: Sequence[Any], group_id: str | None) -> Mapping[str, Any] | None:
        valid = [item for item in groups if isinstance(item, Mapping)]
        if group_id:
            for item in valid:
                if cls._group_id(item) == str(group_id):
                    return item
            raise ValueError("KG cause group not found")
        if len(valid) == 1:
            return valid[0]
        return None

    @staticmethod
    def _actions_for_group(response: Mapping[str, Any], group: Mapping[str, Any] | None) -> list[Any]:
        if group is not None:
            actions = group.get("actions") or group.get("steps")
            return actions if isinstance(actions, list) else []
        actions = response.get("actions")
        return actions if isinstance(actions, list) else []

    @staticmethod
    def _normalize_actions(actions: Sequence[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(actions, start=1):
            if not isinstance(raw, Mapping):
                raise ValueError(f"KG action {index} must be an object")
            action_type = str(raw.get("actionType") or raw.get("action_type") or "").strip()
            if action_type not in SUPPORTED_KG_ACTIONS:
                raise ValueError(f"unsupported KG action: {action_type or 'empty'}")
            params = raw.get("params")
            if not isinstance(params, Mapping) or not params:
                raise ValueError(f"KG action {index} requires params")
            if action_type == "blacklist_tag":
                normalize_kg_action({"actionType": action_type, "params": params})
            depends_on = raw.get("dependsOn") or raw.get("depends_on") or []
            if isinstance(depends_on, (str, bytes)):
                depends_on = [depends_on]
            dependency_ids = [int(item) for item in depends_on if str(item).isdigit()]
            if any(item >= index or item < 1 for item in dependency_ids):
                raise ValueError(f"KG action {index} has invalid dependencies")
            references = _step_references(params)
            if any(reference not in dependency_ids or reference >= index for reference in references):
                raise ValueError(f"KG action {index} has an undeclared step reference")
            if any(normalized[reference - 1]["actionType"] != "add_tag" for reference in references):
                raise ValueError(f"KG action {index} must reference an add_tag step")
            normalized.append({"actionType": action_type, "params": dict(params), "dependsOn": dependency_ids})
        return normalized

    def _read_plan(self, plan_id: int) -> QueryGovernanceActionPlan:
        with self.session_factory() as session:
            plan = session.get(QueryGovernanceActionPlan, int(plan_id))
            if plan is None or str(plan.action_type) not in {"kg_plan", "relevance_plan"}:
                raise KeyError(f"KG action plan not found: {plan_id}")
            session.expunge(plan)
            return plan

    def _set_group_status(self, plan_id: int, *, group_id: str | None, status: str, executor_type: str, executor_user_id: str | None) -> None:
        with self.session_factory() as session:
            plan = session.get(QueryGovernanceActionPlan, int(plan_id))
            if plan is None:
                raise KeyError(f"KG action plan not found: {plan_id}")
            response = _json_object(plan.response_json)
            groups = response.get("causeGroups") or response.get("cause_groups") or []
            if isinstance(groups, list) and group_id:
                for group in groups:
                    if isinstance(group, dict) and self._group_id(group) == group_id:
                        group["status"] = status
                        group["executorType"] = executor_type
                        group["executorUserId"] = executor_user_id
            plan.status = status
            plan.executor_type = executor_type
            plan.executor_user_id = executor_user_id
            plan.response_json = json.dumps(response, ensure_ascii=False, sort_keys=True)
            session.commit()

    def _succeed(self, plan_id: int, *, group_id: str | None, executor_type: str, executor_user_id: str | None, execution_rows: list[dict[str, Any]], groups: Sequence[Any]) -> dict[str, Any]:
        with self.session_factory() as session:
            plan = session.get(QueryGovernanceActionPlan, int(plan_id))
            if plan is None:
                raise KeyError(f"KG action plan not found: {plan_id}")
            response = _json_object(plan.response_json)
            cause_groups = response.get("causeGroups") or response.get("cause_groups") or []
            for group in cause_groups if isinstance(cause_groups, list) else []:
                if isinstance(group, dict) and self._group_id(group) == group_id:
                    group["status"] = GovernancePlanStatus.VERIFIED.value
                    group["executorType"] = executor_type
                    group["executorUserId"] = executor_user_id
            response["execution"] = {"status": "success", "groupId": group_id, "actions": execution_rows}
            plan.response_json = json.dumps(response, ensure_ascii=False, sort_keys=True)
            kg_groups = [
                item
                for item in cause_groups
                if isinstance(item, Mapping)
                and str(item.get("rootCause") or item.get("root_cause") or "") == "kg_issue"
            ]
            plan.status = self._aggregate_group_status(kg_groups)
            plan.executor_type = executor_type
            plan.executor_user_id = executor_user_id
            plan.executed_at = self.clock().replace(tzinfo=None)
            plan.error_code = None
            plan.error_message = None
            plan.failure_stage = None
            plan_status = plan.status
            session.commit()
        return {
            "status": GovernancePlanStatus.VERIFIED.value,
            "groupId": group_id,
            "actions": execution_rows,
            "planStatus": plan_status,
        }

    def _fail(self, plan_id: int, *, group_id: str | None, executor_type: str, executor_user_id: str | None, error_code: str, error_message: str, execution_rows: list[dict[str, Any]], status: str = GovernancePlanStatus.FAILED.value, failure_stage: str = GovernancePlanFailureStage.EXECUTION.value) -> dict[str, Any]:
        with self.session_factory() as session:
            plan = session.get(QueryGovernanceActionPlan, int(plan_id))
            if plan is None:
                raise KeyError(f"KG action plan not found: {plan_id}")
            response = _json_object(plan.response_json)
            groups = response.get("causeGroups") or response.get("cause_groups") or []
            if isinstance(groups, list) and group_id:
                for group in groups:
                    if isinstance(group, dict) and self._group_id(group) == group_id:
                        group["status"] = status
                        group["executorType"] = executor_type
                        group["executorUserId"] = executor_user_id
            response["execution"] = {"status": status, "groupId": group_id, "actions": execution_rows}
            plan.response_json = json.dumps(response, ensure_ascii=False, sort_keys=True)
            kg_groups = [
                item
                for item in groups
                if isinstance(item, Mapping)
                and str(item.get("rootCause") or item.get("root_cause") or "") == "kg_issue"
            ]
            plan.status = self._aggregate_group_status(kg_groups, fallback=status)
            plan.executor_type = executor_type
            plan.executor_user_id = executor_user_id
            plan.executed_at = self.clock().replace(tzinfo=None)
            plan.error_code = error_code
            plan.error_message = error_message
            plan.failure_stage = failure_stage
            session.commit()
        return {"status": status, "groupId": group_id, "errorCode": error_code, "errorMessage": error_message, "actions": execution_rows}

    @staticmethod
    def _aggregate_group_status(
        groups: Sequence[Mapping[str, Any]],
        *,
        fallback: str = GovernancePlanStatus.PENDING.value,
    ) -> str:
        if not groups:
            return fallback
        statuses = [str(group.get("status") or GovernancePlanStatus.PENDING.value) for group in groups]
        if any(status in {GovernancePlanStatus.PENDING.value, GovernancePlanStatus.EXECUTING.value} for status in statuses):
            return GovernancePlanStatus.PENDING.value
        if all(status == GovernancePlanStatus.VERIFIED.value for status in statuses):
            return GovernancePlanStatus.VERIFIED.value
        if any(status == GovernancePlanStatus.EXECUTION_UNKNOWN.value for status in statuses):
            return GovernancePlanStatus.EXECUTION_UNKNOWN.value
        if any(status == GovernancePlanStatus.ROLLBACK_FAILED.value for status in statuses):
            return GovernancePlanStatus.ROLLBACK_FAILED.value
        if any(status == GovernancePlanStatus.FAILED.value for status in statuses):
            return GovernancePlanStatus.FAILED.value
        if any(status == GovernancePlanStatus.ROLLED_BACK.value for status in statuses):
            return GovernancePlanStatus.ROLLED_BACK.value
        return fallback


__all__ = ["QueryGovernanceKGActionExecutor"]
