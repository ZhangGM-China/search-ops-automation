"""Daily relevance-remediation retag and delayed verification boundary.

The service is deliberately scheduler-agnostic: callers may invoke both public
methods repeatedly.  One ActionIntent per brand/day makes the external retag
write idempotent, while the query-governance action plan keeps the aggregate
baseline and verification summary available to the read API.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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
    QueryGovernanceAsyncTask,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
)


KG_ACTION_TYPES = frozenset({"kg_plan", "kg_tag", "kg_relation", "kg_remediation"})
KG_BLOCKING_STATUSES = frozenset(
    {
        GovernancePlanStatus.PENDING.value,
        GovernancePlanStatus.EXECUTING.value,
        GovernancePlanStatus.EXECUTION_UNKNOWN.value,
        GovernancePlanStatus.ROLLBACK_PENDING.value,
    }
)
VERIFICATION_DELAY = timedelta(minutes=30)


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


def _iso(value: datetime) -> str:
    return value.replace(tzinfo=None).isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return None


def _ordered_ids(values: Any) -> list[str]:
    if isinstance(values, (str, bytes)):
        return []
    if not isinstance(values, Iterable):
        return []
    result: list[str] = []
    for value in values:
        product_id = str(value or "").strip()
        if product_id and product_id not in result:
            result.append(product_id)
    return result


def _product_id(product: Mapping[str, Any]) -> str:
    for key in (
        "product_id",
        "productId",
        "taggingUid",
        "spuId",
        "spu",
        "skuId",
        "id",
    ):
        value = str(product.get(key) or "").strip()
        if value:
            return value
    return ""


class RelevanceRetagVerificationService:
    """Coordinate one retag write and one delayed live-search verification."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        operational_service: Any,
        fact_gateway: ActionGateway,
        clock: Callable[[], datetime] = datetime.now,
        operator: str = "scheduler:query_governance",
        release: str | None = None,
        permission: str = "execute_governance_actions",
    ) -> None:
        self._session_factory = session_factory
        self.operational = operational_service
        self.fact_gateway = fact_gateway
        self.clock = clock
        self.operator = operator
        self.release = release or str(os.getenv("AIOPS_RELEASE") or "dev")
        self.permission = permission

    @staticmethod
    def _idempotency_key(brand: str, data_date: date) -> str:
        return f"query-governance:retag:{brand}:{data_date.isoformat()}"

    def trigger_if_ready(self, brand: str, data_date: date) -> dict[str, Any]:
        normalized_brand = str(brand or "").strip().lower()
        if not normalized_brand or not isinstance(data_date, date):
            raise ValueError("brand and data_date are required")
        facts = self._daily_facts(normalized_brand, data_date)
        if not facts["tasks"]:
            return {"status": "skipped", "reason": "no_query_tasks"}
        if facts["unfinishedTaskIds"]:
            return {
                "status": "waiting",
                "reason": "query_tasks_not_terminal",
                "pendingTaskIds": facts["unfinishedTaskIds"],
            }
        if facts["unfinishedRootCauseTaskIds"]:
            return {
                "status": "waiting",
                "reason": "root_cause_tasks_not_terminal",
                "pendingRootCauseTaskIds": facts["unfinishedRootCauseTaskIds"],
            }
        if facts["pendingActionPlanIds"]:
            return {
                "status": "waiting",
                "reason": "kg_actions_not_terminal",
                "pendingActionPlanIds": facts["pendingActionPlanIds"],
            }
        if not facts["verifiedActionPlanIds"]:
            return {"status": "skipped", "reason": "no_verified_kg_change"}

        key = self._idempotency_key(normalized_brand, data_date)
        plan_id, request = self._create_or_get_plan(
            key=key,
            brand=normalized_brand,
            data_date=data_date,
            facts=facts,
        )
        payload = {
            "brand": normalized_brand,
            "dataDate": data_date.isoformat(),
            "verifiedKgActionPlanIds": request["verifiedKgActionPlanIds"],
        }
        intent = ActionIntent(
            action_type="product_retag",
            idempotency_key=key,
            payload=payload,
            idempotency_payload=payload,
            operator=self.operator,
            brand=normalized_brand,
            release=self.release,
            permission=self.permission,
            target=f"{normalized_brand}:{data_date.isoformat()}",
        )

        def execute(context: ExecutionContext) -> dict[str, Any]:
            if self.operational.retag_products(
                normalized_brand, external_request_id=context.external_request_id
            ) is not True:
                raise RuntimeError("重新打标签接口执行失败")
            triggered_at = self.clock().replace(tzinfo=None)
            return {
                "status": "verified",
                "triggeredAt": _iso(triggered_at),
                "earliestVerificationAt": _iso(triggered_at + VERIFICATION_DELAY),
                "externalRequestId": context.external_request_id,
            }

        try:
            result = self.fact_gateway.execute(intent, execute)
        except ActionExecutionFailedError as exc:
            outcome = {
                "status": "failed",
                "errorCode": "RETAG_EXECUTION_FAILED",
                "errorMessage": str(
                    (exc.result or {}).get("error")
                    if isinstance(exc.result, Mapping)
                    else exc
                ),
            }
            self._record_plan(
                plan_id,
                status=GovernancePlanStatus.FAILED.value,
                retag=outcome,
                error_code=outcome["errorCode"],
                error_message=outcome["errorMessage"],
                failure_stage=GovernancePlanFailureStage.EXECUTION.value,
            )
            return outcome
        except ActionInProgressError as exc:
            outcome = {
                "status": "execution_unknown",
                "errorCode": "RETAG_EXECUTION_UNKNOWN",
                "errorMessage": str(exc),
            }
            self._record_plan(
                plan_id,
                status=GovernancePlanStatus.EXECUTION_UNKNOWN.value,
                retag=outcome,
                error_code=outcome["errorCode"],
                error_message=outcome["errorMessage"],
                failure_stage=GovernancePlanFailureStage.EXECUTION.value,
            )
            return outcome
        except (ActionAuthorizationError, ActionConflictError) as exc:
            outcome = {
                "status": "failed",
                "errorCode": "RETAG_EXECUTION_FAILED",
                "errorMessage": str(exc),
            }
            self._record_plan(
                plan_id,
                status=GovernancePlanStatus.FAILED.value,
                retag=outcome,
                error_code=outcome["errorCode"],
                error_message=outcome["errorMessage"],
                failure_stage=GovernancePlanFailureStage.EXECUTION.value,
            )
            return outcome

        if not isinstance(result, Mapping) or str(result.get("status")) != "verified":
            outcome = {
                "status": "execution_unknown",
                "errorCode": "RETAG_EXECUTION_UNKNOWN",
                "errorMessage": str(
                    result.get("reason") if isinstance(result, Mapping) else "invalid result"
                ),
            }
            self._record_plan(
                plan_id,
                status=GovernancePlanStatus.EXECUTION_UNKNOWN.value,
                retag=outcome,
                error_code=outcome["errorCode"],
                error_message=outcome["errorMessage"],
                failure_stage=GovernancePlanFailureStage.EXECUTION.value,
            )
            return outcome
        outcome = dict(result)
        self._record_plan(
            plan_id, status=GovernancePlanStatus.VERIFIED.value, retag=outcome
        )
        return outcome

    def verify_if_due(self, brand: str, data_date: date) -> dict[str, Any]:
        plan = self._retag_plan(str(brand or "").strip().lower(), data_date)
        if plan is None:
            return {"status": "waiting", "reason": "retag_not_triggered"}
        response = _json_object(plan.response_json)
        existing = response.get("remediationSummary")
        if isinstance(existing, Mapping) and str(existing.get("status")) in {
            "verified",
            "failed",
            "execution_unknown",
        }:
            return dict(existing)
        retag = response.get("retag") if isinstance(response.get("retag"), Mapping) else {}
        if plan.status != GovernancePlanStatus.VERIFIED.value:
            return dict(retag) if retag else {"status": plan.status}
        earliest = _parse_datetime(retag.get("earliestVerificationAt"))
        if earliest is None:
            summary = {
                "status": "execution_unknown",
                "conclusion": "unknown",
                "errorCode": "RETAG_VERIFICATION_GATE_MISSING",
            }
            self._record_summary(int(plan.id), summary)
            return summary
        now = self.clock().replace(tzinfo=None)
        if now < earliest:
            return {
                "status": "waiting",
                "reason": "verification_not_due",
                "earliestVerificationAt": _iso(earliest),
            }

        request = _json_object(plan.request_json)
        baselines = request.get("queryBaselines")
        if not isinstance(baselines, list):
            baselines = []
        query_summaries: list[dict[str, Any]] = []
        try:
            for baseline in baselines:
                if not isinstance(baseline, Mapping):
                    continue
                query_summaries.append(self._verify_query(plan.brand, baseline))
        except Exception as exc:
            summary = {
                "status": "execution_unknown",
                "conclusion": "unknown",
                "errorCode": "RETAG_VERIFICATION_SEARCH_FAILED",
                "errorMessage": str(exc).strip() or exc.__class__.__name__,
                "verificationTime": _iso(now),
            }
            self._record_summary(int(plan.id), summary)
            return summary

        remaining = sorted(
            {
                product_id
                for item in query_summaries
                for product_id in item["remainingErrorProductIds"]
            }
        )
        harmed = sorted(
            {
                product_id
                for item in query_summaries
                for product_id in item["harmedCorrectProductIds"]
            }
        )
        passed = not remaining and not harmed
        summary = {
            "status": "verified" if passed else "failed",
            "conclusion": "passed" if passed else "failed",
            "verificationTime": _iso(now),
            "earliestVerificationAt": _iso(earliest),
            "beforeRecallCount": sum(item["beforeRecallCount"] for item in query_summaries),
            "afterRecallCount": sum(item["afterRecallCount"] for item in query_summaries),
            "errorProductsDisappeared": not remaining,
            "correctProductsHarmed": bool(harmed),
            "remainingErrorProductIds": remaining,
            "harmedCorrectProductIds": harmed,
            "queries": query_summaries,
        }
        self._record_summary(int(plan.id), summary)
        return summary

    def summary_for_task(self, task_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            task = session.get(QueryGovernanceTask, int(task_id))
            if task is None:
                return None
            brand, data_date = task.brand, task.data_date
        plan = self._retag_plan(brand, data_date)
        if plan is None:
            return None
        response = _json_object(plan.response_json)
        summary = response.get("remediationSummary")
        if isinstance(summary, Mapping):
            return dict(summary)
        retag = response.get("retag") if isinstance(response.get("retag"), Mapping) else {}
        if plan.status == GovernancePlanStatus.VERIFIED.value and retag:
            return {
                "status": "waiting",
                "reason": "verification_not_due",
                "earliestVerificationAt": retag.get("earliestVerificationAt"),
            }
        return dict(retag) if retag else {"status": plan.status}

    def _daily_facts(self, brand: str, data_date: date) -> dict[str, Any]:
        with self._session_factory() as session:
            tasks = session.scalars(
                select(QueryGovernanceTask)
                .where(
                    QueryGovernanceTask.brand == brand,
                    QueryGovernanceTask.data_date == data_date,
                )
                .order_by(QueryGovernanceTask.id.asc())
            ).all()
            task_ids = [int(task.id) for task in tasks]
            root_cause_tasks = (
                session.scalars(
                    select(QueryGovernanceAsyncTask)
                    .where(
                        QueryGovernanceAsyncTask.task_id.in_(task_ids),
                        QueryGovernanceAsyncTask.trigger_source == "relevance_root_cause",
                    )
                    .order_by(QueryGovernanceAsyncTask.id.asc())
                ).all()
                if task_ids
                else []
            )
            actions = (
                session.scalars(
                    select(QueryGovernanceActionPlan)
                    .where(
                        QueryGovernanceActionPlan.task_id.in_(task_ids),
                        QueryGovernanceActionPlan.action_type.in_(KG_ACTION_TYPES),
                    )
                    .order_by(QueryGovernanceActionPlan.id.asc())
                ).all()
                if task_ids
                else []
            )
            steps = (
                session.scalars(
                    select(QueryGovernanceStepStatus).where(
                        QueryGovernanceStepStatus.task_id.in_(task_ids),
                        QueryGovernanceStepStatus.node_key == "relevance",
                    )
                ).all()
                if task_ids
                else []
            )
        step_by_task = {int(step.task_id): step for step in steps}
        return {
            "tasks": tasks,
            "unfinishedTaskIds": [
                int(task.id)
                for task in tasks
                if str(task.status) not in {"completed", "error", "failed", "skipped"}
            ],
            "unfinishedRootCauseTaskIds": [
                int(async_task.id)
                for async_task in root_cause_tasks
                if str(async_task.status) in {"pending", "processing"}
            ],
            "pendingActionPlanIds": [
                int(action.id)
                for action in actions
                if str(action.status) in KG_BLOCKING_STATUSES
            ],
            "verifiedActionPlanIds": [
                int(action.id)
                for action in actions
                if str(action.status) == GovernancePlanStatus.VERIFIED.value
            ],
            "queryBaselines": [
                self._baseline(task, step_by_task.get(int(task.id))) for task in tasks
            ],
        }

    @staticmethod
    def _baseline(task: QueryGovernanceTask, step: Any | None) -> dict[str, Any]:
        payload = _json_object(getattr(step, "result_json", None))
        candidates = payload.get("blacklist_candidates") or payload.get("blacklistCandidates") or []
        error_ids = []
        if isinstance(candidates, list):
            error_ids = _ordered_ids(
                _product_id(item) for item in candidates if isinstance(item, Mapping)
            )
        detected = _ordered_ids(
            payload.get("detected_product_ids") or payload.get("detectedProductIds") or []
        )
        correct = _ordered_ids(
            payload.get("relevant_product_ids") or payload.get("relevantProductIds") or []
        )
        if not correct:
            correct = [product_id for product_id in detected if product_id not in error_ids]
        return {
            "taskId": int(task.id),
            "query": str(task.query_text),
            "beforeProductIds": detected,
            "errorProductIds": error_ids,
            "correctProductIds": correct,
        }

    def _create_or_get_plan(
        self,
        *,
        key: str,
        brand: str,
        data_date: date,
        facts: Mapping[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        request = {
            "brand": brand,
            "dataDate": data_date.isoformat(),
            "execution_mode": "auto",
            "verifiedKgActionPlanIds": list(facts["verifiedActionPlanIds"]),
            "queryBaselines": list(facts["queryBaselines"]),
        }
        with self._session_factory() as session:
            existing = session.scalar(
                select(QueryGovernanceActionPlan).where(
                    QueryGovernanceActionPlan.idempotency_key == key
                )
            )
            if existing is not None:
                return int(existing.id), _json_object(existing.request_json)
            owner = facts["tasks"][0]
            row = QueryGovernanceActionPlan(
                task_id=owner.id,
                batch_id=owner.batch_id,
                brand=brand,
                query_text="",
                node_key="query_governance.retag",
                action_type="product_retag",
                idempotency_key=key,
                status=GovernancePlanStatus.EXECUTING.value,
                executor_type=GovernancePlanExecutorType.AI_AUTO.value,
                request_json=json.dumps(request, ensure_ascii=False, sort_keys=True),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(QueryGovernanceActionPlan).where(
                        QueryGovernanceActionPlan.idempotency_key == key
                    )
                )
                if existing is None:
                    raise
                return int(existing.id), _json_object(existing.request_json)
            session.refresh(row)
            return int(row.id), request

    def _record_plan(
        self,
        plan_id: int,
        *,
        status: str,
        retag: Mapping[str, Any],
        error_code: str | None = None,
        error_message: str | None = None,
        failure_stage: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            row = session.get(QueryGovernanceActionPlan, plan_id)
            if row is None:
                raise KeyError(f"retag plan not found: {plan_id}")
            response = _json_object(row.response_json)
            response["retag"] = dict(retag)
            row.response_json = json.dumps(response, ensure_ascii=False, sort_keys=True)
            row.status = GovernancePlanStatus(status).value
            row.executor_type = GovernancePlanExecutorType.AI_AUTO.value
            row.executed_at = self.clock().replace(tzinfo=None)
            row.error_code = error_code
            row.error_message = error_message
            row.failure_stage = failure_stage
            session.commit()

    def _record_summary(self, plan_id: int, summary: Mapping[str, Any]) -> None:
        with self._session_factory() as session:
            row = session.get(QueryGovernanceActionPlan, plan_id)
            if row is None:
                raise KeyError(f"retag plan not found: {plan_id}")
            response = _json_object(row.response_json)
            response["remediationSummary"] = dict(summary)
            row.response_json = json.dumps(response, ensure_ascii=False, sort_keys=True)
            session.commit()

    def _retag_plan(self, brand: str, data_date: date) -> QueryGovernanceActionPlan | None:
        key = self._idempotency_key(brand, data_date)
        with self._session_factory() as session:
            row = session.scalar(
                select(QueryGovernanceActionPlan).where(
                    QueryGovernanceActionPlan.idempotency_key == key
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    def _verify_query(
        self, brand: str, baseline: Mapping[str, Any]
    ) -> dict[str, Any]:
        query = str(baseline.get("query") or "").strip()
        before_ids = _ordered_ids(baseline.get("beforeProductIds") or [])
        error_ids = _ordered_ids(baseline.get("errorProductIds") or [])
        correct_ids = _ordered_ids(baseline.get("correctProductIds") or [])
        page_size = max(100, len(before_ids), len(error_ids) + len(correct_ids))
        live = self.operational.search_products_with_total(
            brand, query, page_size=page_size, page_num=1
        )
        if not isinstance(live, Mapping) or live.get("success") is not True:
            raise RuntimeError(str((live or {}).get("error_reason") or "search failed"))
        products = live.get("products")
        if not isinstance(products, list):
            raise RuntimeError("search products are invalid")
        current_ids = {
            _product_id(item) for item in products if isinstance(item, Mapping) and _product_id(item)
        }
        remaining = [product_id for product_id in error_ids if product_id in current_ids]
        harmed = [product_id for product_id in correct_ids if product_id not in current_ids]
        total = live.get("total_count")
        after_count = int(total) if isinstance(total, (int, float)) and not isinstance(total, bool) else len(current_ids)
        return {
            "query": query,
            "beforeRecallCount": len(before_ids),
            "afterRecallCount": after_count,
            "errorProductIds": error_ids,
            "remainingErrorProductIds": remaining,
            "correctProductIds": correct_ids,
            "harmedCorrectProductIds": harmed,
            "errorProductsDisappeared": not remaining,
            "correctProductsHarmed": bool(harmed),
        }


__all__ = [
    "KG_ACTION_TYPES",
    "RelevanceRetagVerificationService",
    "VERIFICATION_DELAY",
]
