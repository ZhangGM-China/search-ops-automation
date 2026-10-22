"""Query sorting actions for the deterministic reorder node.

The vector node produces an expected order, while this module owns the
external sorting write, captured pre-write state, and the common action-fact
gateway integration.  No legacy 15-product constraint is used here.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.governance.action_gateway import (
    ActionAuthorizationError,
    ActionConflictError,
    ActionExecutionFailedError,
    ActionGateway as FactActionGateway,
    ActionInProgressError,
    ActionIntent,
    ExecutionContext,
)

from .models import (
    ActionRecoveryResult,
    GovernancePlanExecutorType,
    GovernancePlanFailureStage,
    GovernancePlanStatus,
    NodeResult,
    NodeStatus,
    QueryGovernanceActionPlan,
    QueryGovernanceTask,
    QuerySpec,
    _utc_now_naive,
)


class QueryReorderActionError(RuntimeError):
    """Raised when a sorting action cannot be validated or restored."""


class QueryReorderActionExecutor:
    """Write and restore one Query's 2.10 custom sorting configuration."""

    def __init__(self, operational_service: Any) -> None:
        self.operational = operational_service

    def execute(
        self, spec: QuerySpec, action: Mapping[str, Any], context: Mapping[str, Any]
    ) -> dict[str, Any]:
        desired_ids = _required_product_ids(action)
        filters = action.get("filters")
        existing = self.operational.find_query_sort_config(
            spec.brand, spec.query, filters
        )
        created_config = existing is None
        config_id: str | None = None
        before_products: list[Mapping[str, Any]] = []
        execution: dict[str, Any] = {
            "request_id": str(context.get("external_request_id") or "").strip()
            or str(action.get("idempotency_key") or "").strip(),
            "brand": spec.brand,
            "query": spec.query,
            "product_ids": desired_ids,
            "before_product_ids": [],
            "created_config": created_config,
        }
        try:
            config_id = (
                str(existing.get("id"))
                if isinstance(existing, Mapping) and existing.get("id") is not None
                else None
            )
            if not config_id:
                config_id = str(
                    self.operational.create_query_sort_config(
                        spec.brand, spec.query, filters
                    )
                ).strip()
                if not config_id:
                    raise QueryReorderActionError("排序配置创建未返回有效 ID")

            before_products = list(
                self.operational.get_query_sort_products(
                    spec.brand, config_id, spec.query
                )
                or []
            )
            before_ids = _product_ids_from_products(before_products)
            execution.update(
                {
                    "config_id": config_id,
                    "before_products": [dict(item) for item in before_products],
                    "before_product_ids": before_ids,
                }
            )
            before_by_id = {
                str(item.get("taggingUid") or "").strip().upper(): dict(item)
                for item in before_products
                if str(item.get("taggingUid") or "").strip()
            }
            desired_products = []
            for product_id in desired_ids:
                product = dict(before_by_id.get(product_id, {"taggingUid": product_id}))
                product["taggingUid"] = product_id
                desired_products.append(product)
            self.operational.save_query_sort_products(
                spec.brand, config_id, desired_products, spec.query
            )
            execution["status"] = "success"
            execution["write_status"] = "success"
            return execution
        except Exception as exc:
            # The external service is not transactional.  If any write step
            # fails after a config was identified/created, restore the state
            # captured above before surfacing the failure to the action facts.
            if config_id:
                try:
                    rollback = self.rollback(spec, action, execution, context)
                    execution["rollback"] = rollback
                except Exception as rollback_exc:
                    execution["rollback"] = {
                        "status": "error",
                        "error": _error_text(rollback_exc),
                    }
                    raise QueryReorderActionError(
                        f"排序写入失败且回滚失败：{_error_text(exc)}；{_error_text(rollback_exc)}"
                    ) from exc
            raise

    def rollback(
        self,
        spec: QuerySpec,
        action: Mapping[str, Any],
        execution: Mapping[str, Any] | None,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        del action, context
        if not isinstance(execution, Mapping):
            return {"status": "not_required"}
        config_id = str(execution.get("config_id") or "").strip()
        if not config_id:
            return {"status": "not_required"}
        before_products = execution.get("before_products")
        before_ids = _product_ids_from_products(
            before_products if isinstance(before_products, Sequence) else []
        )
        if execution.get("created_config") is True and not before_products:
            self.operational.delete_query_sort_config(
                spec.brand, config_id, spec.query
            )
        elif isinstance(before_products, Sequence) and not isinstance(before_products, (str, bytes)):
            self.operational.save_query_sort_products(
                spec.brand, config_id, list(before_products), spec.query
            )
        return {
            "status": "success",
            "config_id": config_id,
            "restored_product_ids": before_ids,
        }


class QueryReorderActionVerifier:
    """Verify the expected order against the authoritative Mixed response."""

    def __call__(
        self,
        spec: QuerySpec,
        action: Mapping[str, Any],
        products: list[Mapping[str, Any]],
        execution: Any,
    ) -> dict[str, Any]:
        del spec, execution
        expected = _required_product_ids(action)
        actual = _product_ids_from_products(products)
        expected_count = len(expected)
        matched_positions = [
            position
            for position, expected_id in enumerate(expected, start=1)
            if position <= len(actual) and actual[position - 1] == expected_id
        ]
        unmatched_positions = [
            position
            for position in range(1, expected_count + 1)
            if position not in matched_positions
        ]
        matched_count = len(matched_positions)
        verification = {
            "matched": matched_count > 0,
            "partial": 0 < matched_count < expected_count,
            "matched_count": matched_count,
            "expected_count": expected_count,
            "matched_positions": matched_positions,
            "unmatched_positions": unmatched_positions,
            "expected_product_ids": expected,
            "actual_product_ids": actual,
        }
        return verification


class QueryReorderActionPlanRepository:
    """Project shared action facts into the Query-governance read model."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def create_or_get(
        self,
        spec: QuerySpec,
        action: Mapping[str, Any],
        idempotency_key: str,
    ) -> int | None:
        batch_id = str(spec.batch_id or action.get("batch_id") or "").strip()
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
                node_key="reorder",
                action_type="query_reorder",
                idempotency_key=idempotency_key,
                status=GovernancePlanStatus.EXECUTING.value,
                executor_type=GovernancePlanExecutorType.AI_AUTO.value,
                request_json=_canonical_json(action),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.execute(
                    select(QueryGovernanceActionPlan).where(
                        QueryGovernanceActionPlan.idempotency_key
                        == idempotency_key
                    )
                ).scalar_one()
                return int(existing.id)
            session.refresh(row)
            return int(row.id)

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
                raise KeyError(f"Query reorder action plan not found: {plan_id}")
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
                _canonical_json(response) if response is not None else None
            )
            row.error_code = error_code
            row.error_message = error_message
            session.commit()


class QueryReorderActionGateway:
    """Bridge Query node actions to the shared durable action-fact gateway."""

    def __init__(
        self,
        *,
        search: Any,
        executor: QueryReorderActionExecutor,
        verifier: QueryReorderActionVerifier,
        fact_gateway: FactActionGateway | None = None,
        operator: str = "scheduler:query_governance",
        release: str | None = None,
        permission: str = "execute_governance_actions",
        action_plan_repository: QueryReorderActionPlanRepository | None = None,
        readback_attempts: int = 3,
        readback_interval_seconds: float = 0.1,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.search = search
        self.executor = executor
        self.verifier = verifier
        self.fact_gateway = fact_gateway or FactActionGateway(
            authorizer=lambda _intent: True
        )
        self.operator = operator
        self.release = release or str(os.getenv("AIOPS_RELEASE") or "dev")
        self.permission = permission
        if isinstance(readback_attempts, bool) or readback_attempts <= 0:
            raise ValueError("readback_attempts must be a positive integer")
        if readback_interval_seconds < 0:
            raise ValueError("readback_interval_seconds cannot be negative")
        self.action_plan_repository = action_plan_repository or (
            QueryReorderActionPlanRepository(self.fact_gateway.session_factory)
        )
        self.readback_attempts = int(readback_attempts)
        self.readback_interval_seconds = float(readback_interval_seconds)
        self.sleeper = sleeper

    def execute_and_verify(
        self,
        spec: QuerySpec,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> NodeResult:
        payload = dict(action)
        _required_product_ids(payload)
        idempotency_key = _idempotency_key(spec, payload)
        payload["idempotency_key"] = idempotency_key
        intent = ActionIntent(
            action_type="query_reorder",
            idempotency_key=idempotency_key,
            payload=payload,
            operator=self.operator,
            brand=spec.brand,
            release=self.release,
            permission=self.permission,
            query_text=spec.query,
            target=f"{spec.brand}:{spec.query}",
        )
        try:
            action_plan_id = self.action_plan_repository.create_or_get(
                spec, payload, idempotency_key
            )
        except Exception as exc:
            return NodeResult.error("ACTION_PLAN_PERSIST_FAILED", _error_text(exc))
        execution_holder: dict[str, Any] = {}
        readback_holder: dict[str, Any] = {}

        def execute(execution_context: ExecutionContext) -> Any:
            execution = self.executor.execute(
                spec,
                payload,
                {
                    **dict(context),
                    "external_request_id": execution_context.external_request_id,
                    "intent_id": execution_context.intent_id,
                    "attempt_id": execution_context.attempt_id,
                },
            )
            execution_holder["value"] = execution
            return execution

        def rollback() -> dict[str, Any]:
            try:
                return self.executor.rollback(
                    spec,
                    payload,
                    execution_holder.get("value"),
                    context,
                )
            except Exception as exc:
                return {"status": "error", "error": _error_text(exc)}

        def readback(execution_context: ExecutionContext) -> dict[str, Any]:
            del execution_context
            mixed_error: Exception | None = None
            verification_error: Exception | None = None
            verification: dict[str, Any] | None = None
            for attempt in range(1, self.readback_attempts + 1):
                try:
                    products = list(self.search.mixed(spec.brand, spec.query))
                except Exception as exc:
                    mixed_error = exc
                    verification = None
                    verification_error = None
                else:
                    try:
                        verification = dict(
                            self.verifier(
                                spec,
                                payload,
                                products,
                                execution_holder.get("value"),
                            )
                        )
                    except Exception as exc:
                        verification_error = exc
                    else:
                        if verification.get("matched") is True:
                            observed = {
                                "status": "success",
                                "attempt": attempt,
                                "verification": verification,
                            }
                            readback_holder["value"] = observed
                            return observed
                if attempt < self.readback_attempts:
                    self.sleeper(self.readback_interval_seconds)
            if mixed_error is not None and verification is None and verification_error is None:
                observed = {
                    "status": "inconclusive",
                    "reason": "readback_unavailable",
                    "error": _error_text(mixed_error),
                }
                readback_holder["value"] = observed
                raise mixed_error
            if verification_error is not None and verification is None:
                observed = {
                    "status": "failed",
                    "reason": "readback_failed",
                    "error": _error_text(verification_error),
                    "rollback": rollback(),
                }
                readback_holder["value"] = observed
                return observed
            observed = {
                "status": "failed",
                "reason": "verification_failed",
                "verification": verification or {},
                "rollback": rollback(),
            }
            readback_holder["value"] = observed
            return observed

        try:
            gateway_result = self.fact_gateway.execute(
                intent,
                execute,
                readback=readback,
                authoritative_readback_on_success=True,
            )
        except ActionExecutionFailedError as exc:
            failure = exc.result if isinstance(exc.result, Mapping) else {}
            self.action_plan_repository.record_result(
                action_plan_id,
                status=GovernancePlanStatus.FAILED.value,
                response=dict(failure),
                error_code="ACTION_EXECUTION_FAILED",
                error_message=str(failure.get("error") or "排序写入失败"),
                failure_stage=GovernancePlanFailureStage.EXECUTION.value,
            )
            return NodeResult(
                status=NodeStatus.ERROR,
                result={"gateway": dict(failure)},
                error_code="ACTION_EXECUTION_FAILED",
                error_message=str(failure.get("error") or "排序写入失败"),
                action=payload,
            )
        except ActionConflictError as exc:
            self._record_plan_error(action_plan_id, "ACTION_IDEMPOTENCY_CONFLICT", exc)
            return NodeResult.error("ACTION_IDEMPOTENCY_CONFLICT", _error_text(exc))
        except ActionInProgressError as exc:
            self._record_plan_error(action_plan_id, "ACTION_IN_PROGRESS", exc)
            return NodeResult.error("ACTION_IN_PROGRESS", _error_text(exc))
        except ActionAuthorizationError as exc:
            self._record_plan_error(action_plan_id, "ACTION_AUTHORIZATION_FAILED", exc)
            return NodeResult.error("ACTION_AUTHORIZATION_FAILED", _error_text(exc))
        except Exception as exc:
            self._record_plan_error(action_plan_id, "ACTION_GATEWAY_FAILED", exc)
            return NodeResult.error("ACTION_GATEWAY_FAILED", _error_text(exc))

        if not isinstance(gateway_result, Mapping):
            self.action_plan_repository.record_result(
                action_plan_id,
                status=GovernancePlanStatus.FAILED.value,
                error_code="ACTION_RESULT_INVALID",
                error_message="排序动作网关返回值无效",
                failure_stage=GovernancePlanFailureStage.EXECUTION.value,
            )
            return NodeResult.error(
                "ACTION_RESULT_INVALID", "排序动作网关返回值无效"
            )
        status = str(gateway_result.get("status") or "").strip().lower()
        if status == "success":
            response = {
                "gateway": dict(gateway_result),
                "verification": readback_holder.get("value"),
                "idempotency_key": idempotency_key,
            }
            self.action_plan_repository.record_result(
                action_plan_id,
                status=GovernancePlanStatus.VERIFIED.value,
                response=response,
            )
            return NodeResult.executed(
                response,
                action=payload,
            )
        if status in {"failed", "inconclusive"}:
            code = (
                "ACTION_READBACK_FAILED"
                if status == "failed"
                else "ACTION_READBACK_INCONCLUSIVE"
            )
            response = {
                "gateway": dict(gateway_result),
                "verification": readback_holder.get("value"),
                "idempotency_key": idempotency_key,
            }
            readback_result = readback_holder.get("value")
            rollback_result = (
                readback_result.get("rollback")
                if isinstance(readback_result, Mapping)
                and isinstance(readback_result.get("rollback"), Mapping)
                else {}
            )
            rollback_status = str(rollback_result.get("status") or "").lower()
            if rollback_status == "error":
                plan_status = GovernancePlanStatus.ROLLBACK_FAILED.value
                failure_stage = GovernancePlanFailureStage.ROLLBACK.value
                code = "ACTION_ROLLBACK_FAILED"
            elif rollback_status == "success":
                plan_status = GovernancePlanStatus.ROLLED_BACK.value
                failure_stage = GovernancePlanFailureStage.VERIFICATION.value
            else:
                plan_status = GovernancePlanStatus.EXECUTION_UNKNOWN.value
                failure_stage = GovernancePlanFailureStage.VERIFICATION.value
            self.action_plan_repository.record_result(
                action_plan_id,
                status=plan_status,
                response=response,
                error_code=code,
                error_message="排序动作权威回读未确认成功",
                failure_stage=failure_stage,
            )
            return NodeResult(
                status=NodeStatus.ERROR,
                result=response,
                error_code=code,
                error_message="排序动作权威回读未确认成功",
                action=payload,
            )
        self.action_plan_repository.record_result(
            action_plan_id,
            status=GovernancePlanStatus.FAILED.value,
            response={"gateway": dict(gateway_result)},
            error_code="ACTION_RESULT_INVALID",
            error_message="排序动作网关未返回可识别的终态",
            failure_stage=GovernancePlanFailureStage.EXECUTION.value,
        )
        return NodeResult.error("ACTION_RESULT_INVALID", "排序动作网关未返回可识别的终态")

    def _record_plan_error(
        self, plan_id: int | None, error_code: str, exc: Exception
    ) -> None:
        self.action_plan_repository.record_result(
            plan_id,
            status=GovernancePlanStatus.FAILED.value,
            response={"error": _error_text(exc)},
            error_code=error_code,
            error_message=_error_text(exc),
            failure_stage=GovernancePlanFailureStage.EXECUTION.value,
        )

    def recover_interrupted(
        self,
        spec: QuerySpec,
        node: str,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> ActionRecoveryResult:
        del node, context
        try:
            products = list(self.search.mixed(spec.brand, spec.query))
            verification = dict(self.verifier(spec, action, products, None))
        except Exception as exc:
            return ActionRecoveryResult.unresolved(
                "ACTION_RECOVERY_READBACK_FAILED", _error_text(exc)
            )
        if verification.get("matched") is True:
            return ActionRecoveryResult.completed(
                NodeResult.executed(
                    {"verification": verification, "recovered": True},
                    action=dict(action),
                )
            )
        return ActionRecoveryResult.safe_to_retry()


def build_query_reorder_action(
    spec: QuerySpec, result: Mapping[str, Any]
) -> dict[str, Any]:
    """Create the automatic sorting action emitted by the vector node."""

    action = {
        "type": "query_reorder",
        "batch_id": spec.batch_id,
        "query_id": spec.query_id,
        "expected_product_ids": list(result.get("product_ids") or []),
        "preference_description": result.get("preference_description"),
        "request": dict(result.get("request") or {}),
    }
    products = result.get("products")
    if isinstance(products, Sequence) and any(
        _stock_value(product) is not None
        for product in products
        if isinstance(product, Mapping)
    ):
        action["expected_products"] = [dict(product) for product in products if isinstance(product, Mapping)]
    return action


def _required_product_ids(action: Mapping[str, Any]) -> list[str]:
    values = action.get("expected_product_ids")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise QueryReorderActionError("排序动作缺少 expected_product_ids")
    normalized = [str(value).strip().upper() for value in values if str(value).strip()]
    if not normalized:
        raise QueryReorderActionError("排序动作至少需要一个商品")
    return normalized


def _product_ids_from_products(products: Sequence[Any]) -> list[str]:
    ids: list[str] = []
    for product in products:
        if not isinstance(product, Mapping):
            continue
        for key in (
            "taggingUid",
            "tagging_uid",
            "productId",
            "product_id",
            "spuId",
            "spu",
            "skuId",
        ):
            value = product.get(key)
            if value is not None and str(value).strip():
                ids.append(str(value).strip().upper())
                break
    return ids


def _stock_value(product: Mapping[str, Any] | None) -> float | None:
    if not isinstance(product, Mapping):
        return None
    for key in ("stock", "inventory", "stockQuantity", "inventoryQuantity"):
        value = product.get(key)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def _idempotency_key(spec: QuerySpec, action: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {
            "batch_id": spec.batch_id,
            "query_id": spec.query_id,
            "brand": spec.brand,
            "query": spec.query,
            "expected_product_ids": _required_product_ids(action),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"query-reorder:{digest}"


def _error_text(exc: Exception) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "QueryReorderActionError",
    "QueryReorderActionExecutor",
    "QueryReorderActionGateway",
    "QueryReorderActionPlanRepository",
    "QueryReorderActionVerifier",
    "build_query_reorder_action",
]
