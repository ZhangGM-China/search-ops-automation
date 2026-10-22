"""Authoritative product-blacklist action gateway for relevance governance."""

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
    ActionAuthorizationError,
    ActionConflictError,
    ActionExecutionFailedError,
    ActionGateway as FactActionGateway,
    ActionInProgressError,
    ActionIntent,
    ExecutionContext,
)
from src.governance.query_policy import assert_query_governance_allowed

from .models import (
    ActionRecoveryResult,
    GovernancePlanExecutorType,
    GovernancePlanFailureStage,
    GovernancePlanStatus,
    NodeResult,
    NodeStatus,
    QuerySpec,
    _utc_now_naive,
)
from src.persistence.repositories import save_action_log
from .models import QueryGovernanceActionPlan, QueryGovernanceTask


class RelevanceActionPlanRepository:
    """Project authoritative relevance actions into the query read model."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def create_or_get(self, spec: QuerySpec, action: Mapping[str, Any], idempotency_key: str, *, status: str = GovernancePlanStatus.EXECUTING.value) -> int | None:
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
                node_key="relevance",
                action_type="product_blacklist",
                idempotency_key=idempotency_key,
                status=status,
                executor_type=(GovernancePlanExecutorType.HUMAN.value if status == GovernancePlanStatus.PENDING.value else GovernancePlanExecutorType.AI_AUTO.value),
                request_json=json.dumps(dict(action), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
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

    def record_result(self, plan_id: int | None, *, status: str, response: Mapping[str, Any] | None = None,
                      error_code: str | None = None, error_message: str | None = None,
                      executor_type: str | None = None, executor_user_id: str | None = None,
                      failure_stage: str | None = None) -> None:
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
            row.response_json = json.dumps(dict(response), ensure_ascii=False, sort_keys=True, separators=(",", ":")) if response is not None else None
            row.error_code = error_code
            row.error_message = error_message
            session.commit()

    def create_pending(self, spec: QuerySpec, action: Mapping[str, Any]) -> int | None:
        """Persist a human-approval plan without invoking any external write."""
        payload = dict(action)
        payload["execution_mode"] = "human_approval"
        return self.create_or_get(spec, payload, self._pending_idempotency_key(spec, payload), status=GovernancePlanStatus.PENDING.value)

    @staticmethod
    def _pending_idempotency_key(spec: QuerySpec, action: Mapping[str, Any]) -> str:
        raw = json.dumps(
            {"brand": spec.brand, "query": spec.query, "batch_id": spec.batch_id, "product_ids": action.get("product_ids") or []},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return "relevance-blacklist-pending:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RelevanceBlacklistActionGateway:
    """Execute one blacklist action with shared idempotency and readback facts."""

    def __init__(
        self,
        *,
        operational_service: Any,
        fact_gateway: FactActionGateway | None = None,
        operator: str = "scheduler:query_governance",
        release: str | None = None,
        permission: str = "execute_governance_actions",
        action_plan_repository: RelevanceActionPlanRepository | None = None,
    ) -> None:
        self.operational = operational_service
        self.fact_gateway = fact_gateway or FactActionGateway(authorizer=lambda _intent: True)
        self.operator = operator
        self.release = release or str(os.getenv("AIOPS_RELEASE") or "dev")
        self.permission = permission
        if action_plan_repository is not None:
            self.action_plan_repository = action_plan_repository
        elif getattr(self.fact_gateway, "session_factory", None) is not None:
            self.action_plan_repository = RelevanceActionPlanRepository(self.fact_gateway.session_factory)
        else:
            self.action_plan_repository = None

    def execute_and_verify(
        self,
        spec: QuerySpec,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> NodeResult:
        payload = self._normalize_action(spec, action)
        idempotency_key = self._idempotency_key(spec, payload)
        payload["idempotency_key"] = idempotency_key
        idempotency_payload = {
            "type": payload["type"],
            "brand": payload["brand"],
            "query": payload["query"],
            "product_ids": payload["product_ids"],
            "batch_id": spec.batch_id,
        }
        intent = ActionIntent(
            action_type="product_blacklist",
            idempotency_key=idempotency_key,
            payload=payload,
            operator=self.operator,
            brand=spec.brand,
            release=self.release,
            permission=self.permission,
            query_text=spec.query,
            target=f"{spec.brand}:{spec.query}",
            idempotency_payload=idempotency_payload,
        )
        try:
            action_plan_id = self.action_plan_repository.create_or_get(spec, payload, idempotency_key) if self.action_plan_repository else None
        except Exception as exc:
            return NodeResult.error("ACTION_PLAN_PERSIST_FAILED", self._error_text(exc))

        def execute(execution_context: ExecutionContext) -> Mapping[str, Any]:
            started = time.perf_counter()
            assert_query_governance_allowed(spec.brand, spec.query)
            unpin_info = self._unpin_top_products_if_needed(spec, payload)
            try:
                success = self.operational.add_to_blacklist(
                    spec.brand,
                    ";".join(payload["product_ids"]),
                    [spec.query],
                    external_request_id=execution_context.external_request_id,
                )
                if success is not True:
                    raise RuntimeError("商品拉黑接口执行失败")
            except Exception as exc:
                if unpin_info.get("unpinned") is True:
                    try:
                        self._rollback_unpin(spec, unpin_info)
                    except Exception as rollback_exc:
                        raise RuntimeError(
                            f"商品拉黑执行失败且置顶回滚失败：{self._error_text(exc)}；{self._error_text(rollback_exc)}"
                        ) from exc
                raise
            response: dict[str, Any] = {
                "status": "success",
                "product_ids": list(payload["product_ids"]),
                "external_request_id": execution_context.external_request_id,
                "writeSeconds": round(max(0.0, time.perf_counter() - started), 6),
            }
            if unpin_info.get("unpinned") is True:
                response["unpinned_product_ids"] = list(
                    unpin_info.get("unpinned_product_ids") or []
                )
                response["unpinned_config_id"] = unpin_info.get("config_id")
                response["deleted_sort_config"] = bool(
                    unpin_info.get("deleted_config")
                )
            return response

        def readback(execution_context: ExecutionContext) -> Mapping[str, Any]:
            started = time.perf_counter()
            observed = self.operational.get_top_query_action_status(
                action_type="product_blacklist",
                external_request_id=execution_context.external_request_id,
                payload={
                    "brand": spec.brand,
                    "query": spec.query,
                    "product_ids": payload["product_ids"],
                },
            )
            if not isinstance(observed, Mapping):
                return {"status": "failed", "reason": "invalid_readback"}
            status = str(observed.get("status") or "").strip().lower()
            if status == "completed":
                return {"status": "success", "readback": dict(observed), "readbackSeconds": round(max(0.0, time.perf_counter() - started), 6)}
            return {"status": "failed", "readback": dict(observed), "readbackSeconds": round(max(0.0, time.perf_counter() - started), 6)}

        try:
            result = self.fact_gateway.execute(
                intent,
                execute,
                readback=readback,
                authoritative_readback_on_success=True,
            )
        except ActionExecutionFailedError as exc:
            failure = exc.result if isinstance(exc.result, Mapping) else {}
            if self.action_plan_repository:
                self.action_plan_repository.record_result(action_plan_id, status=GovernancePlanStatus.FAILED.value, response=dict(failure), error_code="RELEVANCE_BLACKLIST_FAILED", error_message=str(failure.get("error") or "商品拉黑执行失败"), failure_stage=GovernancePlanFailureStage.EXECUTION.value)
            return NodeResult(
                status=NodeStatus.ERROR,
                result={"gateway": dict(failure)},
                error_code="RELEVANCE_BLACKLIST_FAILED",
                error_message=str(failure.get("error") or "商品拉黑执行失败"),
                action=payload,
            )
        except ActionConflictError as exc:
            if self.action_plan_repository:
                self.action_plan_repository.record_result(action_plan_id, status=GovernancePlanStatus.FAILED.value, error_code="RELEVANCE_BLACKLIST_IDEMPOTENCY_CONFLICT", error_message=str(exc), failure_stage=GovernancePlanFailureStage.EXECUTION.value)
            return NodeResult.error("RELEVANCE_BLACKLIST_IDEMPOTENCY_CONFLICT", str(exc))
        except ActionInProgressError as exc:
            if self.action_plan_repository:
                self.action_plan_repository.record_result(action_plan_id, status=GovernancePlanStatus.FAILED.value, error_code="RELEVANCE_BLACKLIST_IN_PROGRESS", error_message=str(exc), failure_stage=GovernancePlanFailureStage.EXECUTION.value)
            return NodeResult.error("RELEVANCE_BLACKLIST_IN_PROGRESS", str(exc))
        except ActionAuthorizationError as exc:
            if self.action_plan_repository:
                self.action_plan_repository.record_result(action_plan_id, status=GovernancePlanStatus.FAILED.value, error_code="RELEVANCE_BLACKLIST_AUTHORIZATION_FAILED", error_message=str(exc), failure_stage=GovernancePlanFailureStage.EXECUTION.value)
            return NodeResult.error("RELEVANCE_BLACKLIST_AUTHORIZATION_FAILED", str(exc))
        except Exception as exc:
            if self.action_plan_repository:
                self.action_plan_repository.record_result(action_plan_id, status=GovernancePlanStatus.FAILED.value, error_code="RELEVANCE_BLACKLIST_FAILED", error_message=self._error_text(exc), failure_stage=GovernancePlanFailureStage.EXECUTION.value)
            return NodeResult.error("RELEVANCE_BLACKLIST_FAILED", self._error_text(exc))

        if not isinstance(result, Mapping):
            if self.action_plan_repository:
                self.action_plan_repository.record_result(action_plan_id, status=GovernancePlanStatus.FAILED.value, error_code="RELEVANCE_BLACKLIST_RESULT_INVALID", error_message="商品拉黑网关返回值无效", failure_stage=GovernancePlanFailureStage.EXECUTION.value)
            return NodeResult.error("RELEVANCE_BLACKLIST_RESULT_INVALID", "商品拉黑网关返回值无效")
        status = str(result.get("status") or "").strip().lower()
        if status != "success":
            # 权威回读虽未全量匹配，但对于其中已确认生效的商品，先行持久化本地 ActionLog，
            # 避免整批正常生效商品因个别异常而被连带标记为未拉黑。
            readback_data = (result.get("readback") or {}) if isinstance(result.get("readback"), Mapping) else {}
            readback_inner = (readback_data.get("response") or {}) if isinstance(readback_data.get("response"), Mapping) else {}
            matched_product_ids = readback_inner.get("matched_product_ids") or []
            if isinstance(matched_product_ids, list):
                product_images = (
                    payload.get("product_images")
                    if isinstance(payload.get("product_images"), dict)
                    else {}
                )
                for product_id in matched_product_ids:
                    if product_id in payload.get("product_ids", []):
                        save_action_log({
                            "brand": spec.brand,
                            "query": spec.query,
                            "spu": product_id,
                            "name": payload["product_names"].get(product_id, ""),
                            "image_url": product_images.get(product_id, ""),
                            "type": 1,
                            "reason": payload["reasons"].get(product_id, ""),
                        })

            if self.action_plan_repository:
                self.action_plan_repository.record_result(action_plan_id, status=GovernancePlanStatus.EXECUTION_UNKNOWN.value, response=dict(result), error_code="RELEVANCE_BLACKLIST_READBACK_FAILED", error_message="商品拉黑权威回读未确认成功", failure_stage=GovernancePlanFailureStage.VERIFICATION.value)
            return NodeResult(
                status=NodeStatus.ERROR,
                result={"gateway": dict(result)},
                error_code="RELEVANCE_BLACKLIST_READBACK_FAILED",
                error_message="商品拉黑权威回读未确认成功",
                action=payload,
            )
        if self.action_plan_repository:
            self.action_plan_repository.record_result(action_plan_id, status=GovernancePlanStatus.VERIFIED.value, response=dict(result))
        for product_id in payload["product_ids"]:
            save_action_log({
                "brand": spec.brand,
                "query": spec.query,
                "spu": product_id,
                "name": payload["product_names"].get(product_id, ""),
                "image_url": (payload.get("product_images") or {}).get(product_id, "") if isinstance(payload.get("product_images"), dict) else "",
                "type": 1,
                "reason": payload["reasons"].get(product_id, ""),
            })
        return NodeResult.executed(
            {"gateway": dict(result), "idempotency_key": idempotency_key},
            action=payload,
        )

    def defer(self, spec: QuerySpec, action: Mapping[str, Any], context: Mapping[str, Any]) -> NodeResult:
        del context
        try:
            payload = self._normalize_action(spec, action)
            payload["execution_mode"] = "human_approval"
            plan_id = self.action_plan_repository.create_pending(spec, payload) if self.action_plan_repository else None
            return NodeResult.executed({"status": "pending_approval", "plan_id": plan_id}, action=payload)
        except Exception as exc:
            return NodeResult.error("ACTION_PLAN_PERSIST_FAILED", self._error_text(exc))

    def recover_interrupted(
        self,
        spec: QuerySpec,
        node: str,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> ActionRecoveryResult:
        """Reconcile an action saved before a worker interruption."""
        del node, context
        checkpoint_state = (
            dict(action.get("_relevance_checkpoint"))
            if isinstance(action, Mapping)
            and isinstance(action.get("_relevance_checkpoint"), Mapping)
            else None
        )
        try:
            payload = self._normalize_action(spec, action)
            request_id = self._idempotency_key(spec, payload)
            observed = self.operational.get_top_query_action_status(
                action_type="product_blacklist",
                external_request_id=request_id,
                payload={
                    "brand": spec.brand,
                    "query": spec.query,
                    "product_ids": payload["product_ids"],
                },
            )
        except Exception as exc:
            return ActionRecoveryResult.unresolved(
                "RELEVANCE_BLACKLIST_RECOVERY_FAILED", self._error_text(exc)
            )
        if not isinstance(observed, Mapping):
            return ActionRecoveryResult.unresolved(
                "RELEVANCE_BLACKLIST_RECOVERY_UNKNOWN", "拉黑动作回读结果无效"
            )
        status = str(observed.get("status") or "").strip().lower()
        if status == "completed":
            return ActionRecoveryResult.completed(
                NodeResult.executed(
                    {
                        "gateway": {"readback": dict(observed)},
                        "recovered": True,
                        "idempotency_key": request_id,
                        **(
                            {"relevance_checkpoint": checkpoint_state}
                            if checkpoint_state is not None
                            else {}
                        ),
                    },
                    action=payload,
                )
            )
        if status in {"not_found", "pending"}:
            return ActionRecoveryResult.safe_to_retry()
        return ActionRecoveryResult.unresolved(
            "RELEVANCE_BLACKLIST_RECOVERY_UNKNOWN",
            "拉黑动作回读无法确认是否完成，未重复执行",
        )

    @staticmethod
    def _normalize_action(spec: QuerySpec, action: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(action, Mapping) or action.get("type") != "product_blacklist":
            raise ValueError("相关性动作必须为 product_blacklist")
        raw_ids = action.get("product_ids") or action.get("product_id") or []
        if isinstance(raw_ids, (str, bytes)):
            raw_ids = [raw_ids]
        product_ids = list(dict.fromkeys(str(value).strip() for value in raw_ids if str(value).strip()))
        if not product_ids:
            raise ValueError("商品拉黑动作缺少 product_ids")
        normalized: dict[str, Any] = {
            "type": "product_blacklist",
            "brand": spec.brand,
            "query": spec.query,
            "execution_mode": str(action.get("execution_mode") or action.get("executionMode") or "auto"),
            "product_ids": product_ids,
            "product_names": {
                str(product_id): str(name or "")
                for product_id, name in dict(action.get("product_names") or {}).items()
                if str(product_id).strip()
            },
            "product_images": {
                str(product_id): str(image or "")
                for product_id, image in dict(action.get("product_images") or {}).items()
                if str(product_id).strip()
            },
            "reasons": dict(action.get("reasons") or {}),
        }
        raw_buckets = action.get("product_buckets")
        if isinstance(raw_buckets, Mapping):
            product_buckets = {
                str(product_id).strip(): str(bucket or "").strip()
                for product_id, bucket in raw_buckets.items()
                if str(product_id).strip() and str(bucket or "").strip()
            }
            if product_buckets:
                normalized["product_buckets"] = product_buckets
        return normalized

    @staticmethod
    def _pinned_product_id(product: Mapping[str, Any]) -> str:
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
                return str(value).strip()
        return ""

    def _unpin_top_products_if_needed(
        self, spec: QuerySpec, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        # Top分桶商品在上游检索中拥有最高优先级，直接拉黑不会生效，必须在拉黑前先从自定义排序中移除置顶。
        product_buckets = payload.get("product_buckets")
        if isinstance(product_buckets, Mapping) and product_buckets:
            has_top_candidate = any(
                str(bucket or "").strip().lower() == "top"
                for bucket in product_buckets.values()
            )
            if not has_top_candidate:
                return {"unpinned": False, "unpinned_product_ids": []}

        find_config = getattr(self.operational, "find_query_sort_config", None)
        get_products = getattr(self.operational, "get_query_sort_products", None)
        if not callable(find_config) or not callable(get_products):
            return {"unpinned": False, "unpinned_product_ids": []}

        existing = find_config(spec.brand, spec.query, payload.get("filters"))
        config_id = (
            str(existing.get("id")).strip()
            if isinstance(existing, Mapping) and existing.get("id") is not None
            else ""
        )
        if not config_id:
            return {"unpinned": False, "unpinned_product_ids": []}

        before_products = [
            dict(item)
            for item in (get_products(spec.brand, config_id, spec.query) or [])
            if isinstance(item, Mapping)
        ]
        if not before_products:
            return {"unpinned": False, "unpinned_product_ids": []}

        target_upper = {
            str(pid).strip().upper()
            for pid in (payload.get("product_ids") or [])
            if str(pid).strip()
        }
        remaining_products: list[dict[str, Any]] = []
        unpinned_ids: list[str] = []
        for item in before_products:
            pinned_id = self._pinned_product_id(item)
            if pinned_id and pinned_id.upper() in target_upper:
                unpinned_ids.append(pinned_id)
            else:
                remaining_products.append(dict(item))

        if not unpinned_ids:
            return {"unpinned": False, "unpinned_product_ids": []}

        if remaining_products:
            reindexed_products: list[dict[str, Any]] = []
            for index, item in enumerate(remaining_products, 1):
                updated = dict(item)
                pinned_id = self._pinned_product_id(updated)
                if pinned_id and not str(updated.get("taggingUid") or "").strip():
                    updated["taggingUid"] = pinned_id
                updated["position"] = index
                reindexed_products.append(updated)
            self.operational.save_query_sort_products(
                spec.brand, config_id, reindexed_products, spec.query
            )
            deleted_config = False
        else:
            self.operational.delete_query_sort_config(
                spec.brand, config_id, spec.query
            )
            deleted_config = True

        return {
            "unpinned": True,
            "config_id": config_id,
            "deleted_config": deleted_config,
            "filters": (
                existing.get("filters")
                if isinstance(existing, Mapping) and isinstance(existing.get("filters"), list)
                else payload.get("filters")
            ),
            "before_products": before_products,
            "unpinned_product_ids": unpinned_ids,
        }

    def _rollback_unpin(
        self, spec: QuerySpec, unpin_info: Mapping[str, Any]
    ) -> dict[str, Any]:
        config_id = str(unpin_info.get("config_id") or "").strip()
        before_products = [
            dict(item)
            for item in (unpin_info.get("before_products") or [])
            if isinstance(item, Mapping)
        ]
        if not config_id or not before_products:
            return {"status": "not_required"}
        target_config_id = config_id
        if unpin_info.get("deleted_config") is True:
            create_config = getattr(self.operational, "create_query_sort_config", None)
            if callable(create_config):
                created_id = str(
                    create_config(spec.brand, spec.query, unpin_info.get("filters")) or ""
                ).strip()
                if created_id:
                    target_config_id = created_id
        self.operational.save_query_sort_products(
            spec.brand, target_config_id, before_products, spec.query
        )
        return {
            "status": "success",
            "config_id": target_config_id,
            "restored_product_ids": [
                self._pinned_product_id(item)
                for item in before_products
                if self._pinned_product_id(item)
            ],
        }

    @staticmethod
    def _idempotency_key(spec: QuerySpec, action: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            {
                "brand": spec.brand,
                "query": spec.query,
                "product_ids": action["product_ids"],
                "batch_id": spec.batch_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "relevance-blacklist:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _error_text(exc: Exception) -> str:
        return str(exc).strip() or exc.__class__.__name__


__all__ = ["RelevanceActionPlanRepository", "RelevanceBlacklistActionGateway"]
