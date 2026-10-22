import datetime
from contextlib import ExitStack, contextmanager
from functools import wraps
import hashlib
import json
import uuid
from time import perf_counter
from typing import Any, Dict, Optional

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from src.persistence import (
    BrandDailyMetric,
    SessionLocal,
    TopQueryActionPlan,
    TopQueryAgentEvent,
    TopQueryStepStatus,
    TopQueryTask,
    create_brand_daily_metric,
    mark_inventory_task_recovered_by_details,
    save_action_log,
    save_inventory_task,
    upsert_top_query_task,
)
from src.config import get_brand_business_config, get_env
from src.integrations.knowledge_graph import KnowledgeGraphClient
from src.integrations.operational_service import OperationalService
from src.query_governance.retirement import LegacyAgentLoopRetiredError

from .extractor import TopQueryDataExtractor
from .executors import build_world_knowledge_verification_summary
from .experiment_service import (
    assert_queries_governance_allowed,
    assert_queries_governance_allowed_in_session,
    assert_query_governance_allowed,
)
from src.governance.query_identity import (
    normalize_governance_query_key,
    normalize_query_key,
    transform_governance_payload_query,
)
from .query_demotion_contract import (
    AUTHORITATIVE_QUERY_DEMOTION_V1,
    LEGACY_PENDING_STATUSES,
    LEGACY_QUERY_DEMOTION_BLOCK_REASON,
    should_block_legacy_query_demotion,
    uses_authoritative_query_demotion,
)
from .task_identity import select_task_winner
from .sanitization import sanitize_action_error, sanitize_action_error_details
from .workflow import (
    QUERY_DEMOTION_VERIFICATION_LEASE_SECONDS,
    _reconcile_query_demotion_verifying_actions,
    _verification_lease_token,
    _verification_now,
    initialize_query_demotion_verification_snapshot,
    query_demotion_verification_group_key,
    query_demotion_verification_targets,
)
from .world_knowledge import WorldKnowledgeBatchSnapshot, WorldKnowledgeService
from .task_lock import (
    TopQueryTaskLockBusy,
    top_query_brand_day_lock,
    top_query_brand_governance_lock,
    top_query_task_lock,
)


class ActionExecutionError(ValueError):
    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        *,
        retryable: bool = True,
    ):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.details = dict(details or {})
        self.details.setdefault("retryable", self.retryable)


class ActionStateConflictError(ValueError):
    pass


class LegacyQueryDemotionContractError(ActionStateConflictError):
    def __init__(self, action_id: int, status: str):
        self.blocked_reason = LEGACY_QUERY_DEMOTION_BLOCK_REASON
        self.requires_rebuild = True
        self.details = {
            "action_id": int(action_id),
            "status": str(status or ""),
            "reason": LEGACY_QUERY_DEMOTION_BLOCK_REASON,
            "required_execution_contract": AUTHORITATIVE_QUERY_DEMOTION_V1,
        }
        super().__init__(LEGACY_QUERY_DEMOTION_BLOCK_REASON)


class ActionValidationError(ValueError):
    pass


_TOP_QUERY_ACTION_RESPONSE_MAX_BYTES = 60000
_TOP_QUERY_ACTION_RESPONSE_TEXT_MAX_BYTES = 2048
_TOP_QUERY_ACTION_RESPONSE_COLLECTION_LIMIT = 16
_TOP_QUERY_ACTION_RESPONSE_PRIORITY_KEYS = frozenset({
    "action_id", "code", "error", "execution_unknown", "executionUnknown",
    "external_reconciliation", "external_request_id", "lifecycle", "ok", "real_action",
    "status", "success", "tool_key", "tool_response", "verification_failed", "verifiedAt",
    "write_succeeded", "config_verified", "result_effect", "world_knowledge_verification",
})


def _action_response_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _truncate_action_response_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    digest = hashlib.sha256(encoded).hexdigest()
    suffix = f"... [truncated sha256={digest}]"
    available = max(0, max_bytes - len(suffix.encode("utf-8")))
    return encoded[:available].decode("utf-8", errors="ignore") + suffix


def _compact_action_response_value(
    value: Any,
    *,
    text_max_bytes: int = _TOP_QUERY_ACTION_RESPONSE_TEXT_MAX_BYTES,
    collection_limit: int = _TOP_QUERY_ACTION_RESPONSE_COLLECTION_LIMIT,
) -> Any:
    if isinstance(value, str):
        return _truncate_action_response_text(value, text_max_bytes)
    if isinstance(value, dict):
        items = sorted(
            value.items(),
            key=lambda item: (
                str(item[0]) not in _TOP_QUERY_ACTION_RESPONSE_PRIORITY_KEYS,
                str(item[0]),
            ),
        )
        compacted = {
            str(key): _compact_action_response_value(
                item,
                text_max_bytes=text_max_bytes,
                collection_limit=collection_limit,
            )
            for key, item in items[:collection_limit]
        }
        if len(items) > collection_limit:
            compacted["__truncated_key_count__"] = len(items) - collection_limit
            compacted["__truncated_sha256__"] = hashlib.sha256(
                _action_response_json(value).encode("utf-8")
            ).hexdigest()
        return compacted
    if isinstance(value, (list, tuple, set)):
        items = sorted(value, key=repr) if isinstance(value, set) else list(value)
        compacted = [
            _compact_action_response_value(
                item,
                text_max_bytes=text_max_bytes,
                collection_limit=collection_limit,
            )
            for item in items[:collection_limit]
        ]
        if len(items) <= collection_limit:
            return compacted
        return {
            "items": compacted,
            "__truncated_item_count__": len(items) - collection_limit,
            "__truncated_sha256__": hashlib.sha256(
                _action_response_json(items).encode("utf-8")
            ).hexdigest(),
        }
    return value


def _bounded_action_response_payload(value: Any) -> str:
    serialized = _action_response_json(value)
    serialized_bytes = serialized.encode("utf-8")
    if len(serialized_bytes) <= _TOP_QUERY_ACTION_RESPONSE_MAX_BYTES:
        return serialized

    audit = {
        "truncated": True,
        "original_bytes": len(serialized_bytes),
        "sha256": hashlib.sha256(serialized_bytes).hexdigest(),
    }
    for text_max_bytes, collection_limit in ((2048, 16), (1024, 8), (512, 4), (256, 2)):
        compacted = _compact_action_response_value(
            value,
            text_max_bytes=text_max_bytes,
            collection_limit=collection_limit,
        )
        if isinstance(compacted, dict):
            compacted["response_payload_audit"] = audit
        else:
            compacted = {"response": compacted, "response_payload_audit": audit}
        compacted_serialized = _action_response_json(compacted)
        if len(compacted_serialized.encode("utf-8")) <= _TOP_QUERY_ACTION_RESPONSE_MAX_BYTES:
            return compacted_serialized

    response = value if isinstance(value, dict) else {}
    fallback_keys = (
        "action_id", "tool_key", "dry_run", "real_action", "status", "success", "ok", "code",
        "write_succeeded", "execution_unknown", "executionUnknown", "verification_failed",
        "external_request_id", "error", "lifecycle", "tool_response", "external_reconciliation",
    )
    fallback = {
        key: _compact_action_response_value(
            response[key], text_max_bytes=128, collection_limit=1,
        )
        for key in fallback_keys
        if key in response
    }
    fallback["response_payload_audit"] = audit
    return _action_response_json(fallback)


def _bounded_tag_bucket_payload(value: Any) -> str:
    serialized = _action_response_json(value)
    serialized_bytes = serialized.encode("utf-8")
    if len(serialized_bytes) <= _TOP_QUERY_ACTION_RESPONSE_MAX_BYTES:
        return serialized

    audit = {
        "truncated": True,
        "original_bytes": len(serialized_bytes),
        "sha256": hashlib.sha256(serialized_bytes).hexdigest(),
    }
    for text_max_bytes, collection_limit in ((2048, 16), (1024, 8), (512, 4), (256, 2)):
        compacted = _compact_action_response_value(
            value,
            text_max_bytes=text_max_bytes,
            collection_limit=collection_limit,
        )
        if isinstance(compacted, dict):
            compacted["tag_bucket_payload_audit"] = audit
        else:
            compacted = {"payload": compacted, "tag_bucket_payload_audit": audit}
        compacted_serialized = _action_response_json(compacted)
        if len(compacted_serialized.encode("utf-8")) <= _TOP_QUERY_ACTION_RESPONSE_MAX_BYTES:
            return compacted_serialized

    source = value if isinstance(value, dict) else {}
    verification = source.get("world_knowledge_verification")
    verification = verification if isinstance(verification, dict) else source
    fallback = {
        "config": _compact_action_response_value(
            source.get("config") or {}, text_max_bytes=128, collection_limit=1,
        ),
        "world_knowledge_verification": {
            key: verification[key]
            for key in ("action_id", "config_verified", "result_effect", "verifiedAt")
            if key in verification
        },
        "tag_bucket_payload_audit": audit,
    }
    return _action_response_json(fallback)


def _task_execution_locked(*, skip_when_busy: bool = False):
    def decorator(method):
        @wraps(method)
        def wrapped(self, task_id: int, *args, **kwargs):
            db = SessionLocal()
            try:
                task = db.get(TopQueryTask, task_id)
                if not task:
                    raise ValueError(f"top query task not found: {task_id}")
                brand = str(task.brand or "")
            finally:
                db.close()
            try:
                with top_query_brand_governance_lock(brand):
                    with top_query_task_lock(task_id):
                        return method(self, task_id, *args, **kwargs)
            except TopQueryTaskLockBusy as exc:
                if skip_when_busy:
                    return {
                        "task_id": int(task_id),
                        "final_status": "skipped",
                        "skip_reason": "task_execution_lock_busy",
                    }
                raise ActionStateConflictError(
                    f"top query task is busy: {int(task_id)}"
                ) from exc
        return wrapped
    return decorator


def _json_payload_action_id(value: Any, nested_key: Optional[str] = None) -> Optional[int]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if nested_key:
        payload = payload.get(nested_key) if isinstance(payload, dict) else None
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("action_id"))
    except (TypeError, ValueError):
        return None


def _action_approval_metadata(response_payload: Any) -> Dict[str, Any]:
    try:
        payload = json.loads(response_payload or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    lifecycle = payload.get("lifecycle") if isinstance(payload.get("lifecycle"), dict) else {}
    approved_at = payload.get("approved_at") or payload.get("approvedAt") or lifecycle.get("approvedAt")
    metadata = {
        key: payload[key]
        for key in ("operator", "reason", "approved_at", "approvedAt")
        if payload.get(key) not in (None, "")
    }
    if approved_at and "approved_at" not in metadata and "approvedAt" not in metadata:
        metadata["approved_at"] = approved_at
    return metadata


class TopQueryGovernanceService:
    ACTION_EXECUTION_LEASE_SECONDS = 60
    QUERY_DEMOTION_VERIFICATION_WINDOW_SECONDS = 15 * 60

    def __init__(
        self,
        extractor: Optional[TopQueryDataExtractor] = None,
        workflow: Optional[Any] = None,
        operational_service: Optional[OperationalService] = None,
        kg_client: Optional[Any] = None,
        world_knowledge_service: Optional[WorldKnowledgeService] = None,
    ):
        self.extractor = extractor
        self._extractors: Dict[str, TopQueryDataExtractor] = {}
        self.workflow = workflow
        self.operational_service = operational_service or OperationalService()
        self.kg_client = kg_client or KnowledgeGraphClient()
        self.world_knowledge_service = world_knowledge_service or WorldKnowledgeService()

    def prepare_world_knowledge_batch_snapshot(
        self,
        brand: str,
        data_date: datetime.date,
        knowledge_date: datetime.date,
        scope_id: str,
    ) -> WorldKnowledgeBatchSnapshot:
        config = get_brand_business_config(brand).get("top_query_world_knowledge_config") or {}
        return self.world_knowledge_service.prepare_batch_snapshot(
            brand=brand,
            data_date=data_date,
            knowledge_date=knowledge_date,
            config=config,
            scope_id=scope_id,
        )

    def _get_extractor_for_brand(self, brand: str):
        if self.extractor is not None:
            return self.extractor

        brand_type = "rl" if brand == "riviera" else "rds"
        if brand_type not in self._extractors:
            self._extractors[brand_type] = TopQueryDataExtractor(brand_type=brand_type)
        return self._extractors[brand_type]

    def create_daily_tasks(
        self,
        brand: str = "atlas",
        target_date: datetime.date = None,
        limit: int = 150,
        allowed_queries: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        target_date = target_date or (datetime.date.today() - datetime.timedelta(days=1))
        extractor = self._get_extractor_for_brand(brand)
        metric = extractor.get_brand_metric(brand, target_date)
        data_quality = None
        if allowed_queries is None:
            top_queries = extractor.get_top_queries(brand, target_date, limit)
        else:
            locked_queries = sorted(allowed_queries, key=normalize_query_key)
            strict_result = extractor.get_query_daily_metrics_strict(brand, target_date, locked_queries)
            data_quality = {
                "status": strict_result.get("status"),
                "missingKeys": strict_result.get("missingKeys") or [],
                "error": strict_result.get("error"),
            }
            if strict_result.get("status") != "complete":
                return {
                    "brand": brand,
                    "data_date": target_date.isoformat(),
                    "task_ids": [],
                    "top_query_count": 0,
                    "data_quality": data_quality,
                }
            top_queries = strict_result.get("items") or []
        top_queries = self._dedupe_daily_rows(top_queries)
        assert_queries_governance_allowed(
            brand,
            [str(row.get("query") or "") for row in top_queries if str(row.get("query") or "").strip()],
        )
        top_query_pv = sum(float(row.get("search_pv") or 0) for row in top_queries)
        brand_pv = float(metric.get("brand_pv") or 0)
        with top_query_brand_governance_lock(brand):
            assert_queries_governance_allowed(
                brand,
                [str(row.get("query") or "") for row in top_queries if str(row.get("query") or "").strip()],
            )
            create_brand_daily_metric({
                **metric,
                "brand": brand,
                "data_date": target_date,
                "top_query_count": len(top_queries),
                "top_query_pv": top_query_pv,
                "top_query_pv_share": top_query_pv / brand_pv if brand_pv else 0,
            })

            with self._existing_task_locks(brand, target_date):
                return self._persist_daily_tasks(
                    brand=brand,
                    target_date=target_date,
                    top_queries=top_queries,
                    data_quality=data_quality,
                )

    def create_experiment_tasks(
        self,
        *,
        experiment_id: int,
        brand: str,
        data_date: datetime.date,
        treatment_rows: list[dict],
    ) -> Dict[str, Any]:
        treatment_keys = [normalize_governance_query_key(brand, row.get("query")) for row in treatment_rows]
        if len(treatment_rows) != 20 or any(not key for key in treatment_keys) or len(set(treatment_keys)) != 20:
            raise ValueError("experiment treatment snapshot must contain exactly 20 canonical queries")
        assert_queries_governance_allowed(
            brand,
            [str(row.get("query") or "") for row in treatment_rows if str(row.get("query") or "").strip()],
        )

        with top_query_brand_governance_lock(brand):
            assert_queries_governance_allowed(
                brand,
                [str(row.get("query") or "") for row in treatment_rows if str(row.get("query") or "").strip()],
            )
            with self._existing_task_locks(brand, data_date):
                return self._persist_experiment_tasks(
                    experiment_id=experiment_id,
                    brand=brand,
                    data_date=data_date,
                    treatment_rows=treatment_rows,
                )

    @contextmanager
    def _existing_task_locks(self, brand: str, data_date: datetime.date):
        try:
            with ExitStack() as locks:
                locks.enter_context(top_query_brand_day_lock(brand, data_date))
                db = SessionLocal()
                try:
                    task_ids = [
                        int(task_id)
                        for (task_id,) in db.query(TopQueryTask.id).filter(
                            TopQueryTask.brand == brand,
                            TopQueryTask.data_date == data_date,
                        ).all()
                    ]
                finally:
                    db.close()
                for task_id in sorted(task_ids):
                    locks.enter_context(top_query_task_lock(task_id))
                yield
        except TopQueryTaskLockBusy as exc:
            raise ActionStateConflictError(
                f"top query task is busy: {exc.task_id}"
            ) from exc

    def _load_existing_tasks_for_update(
        self,
        db,
        brand: str,
        data_date: datetime.date,
    ) -> list[TopQueryTask]:
        return db.query(TopQueryTask).filter(
            TopQueryTask.brand == brand,
            TopQueryTask.data_date == data_date,
        ).with_for_update().all()

    def _persist_daily_tasks(
        self,
        *,
        brand: str,
        target_date: datetime.date,
        top_queries: list[dict],
        data_quality: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            existing_tasks = self._load_existing_tasks_for_update(db, brand, target_date)
            existing_by_query = self._converge_existing_tasks(
                db,
                brand,
                existing_tasks,
                preserve_daily_query_identity=True,
            )
            db.commit()
            for task in existing_by_query.values():
                _ = (task.id, task.query_text, task.status)
                db.expunge(task)
        finally:
            db.close()

        task_ids = []
        active_queries = set()
        for row in top_queries:
            query_text = str(row.get("query") or "").strip()
            if not query_text:
                continue
            query_key = normalize_query_key(query_text)
            active_queries.add(query_key)
            existing_task = existing_by_query.get(query_key)
            task_payload = {
                "brand": brand,
                "data_date": target_date,
                "query_text": str(existing_task.query_text) if existing_task else query_text,
                "query_rank": row.get("query_rank"),
                "search_uv": row.get("search_uv"),
                "search_pv": row.get("search_pv"),
                "ctr": row.get("ctr"),
                "cvr": row.get("cvr"),
                "avg_ctr": row.get("avg_ctr"),
                "avg_cvr": row.get("avg_cvr"),
                "query_pv_share": row.get("query_pv_share"),
                "raw_payload": row,
            }
            if existing_task is None or str(existing_task.status or "").lower() == "superseded":
                task_payload["status"] = "pending"
                task_payload["display_status"] = "normal"
            task_ids.append(upsert_top_query_task(
                task_payload,
                task_id=int(existing_task.id) if existing_task is not None else None,
            ))

        stale_rows = [
            task for query_key, task in existing_by_query.items()
            if query_key not in active_queries
        ]
        if stale_rows:
            db = SessionLocal()
            try:
                stale_ids = [int(task.id) for task in stale_rows if task.id]
                stale_tasks = db.query(TopQueryTask).filter(
                    TopQueryTask.id.in_(stale_ids),
                ).with_for_update().all()
                for row in stale_tasks:
                    row.status = "superseded" # type: ignore
                    row.display_status = "superseded" # type: ignore
                db.commit()
            finally:
                db.close()
        result = {
            "brand": brand,
            "data_date": target_date.isoformat(),
            "task_ids": task_ids,
            "top_query_count": len(task_ids),
        }
        if data_quality is not None:
            result["data_quality"] = data_quality
        return result

    def _persist_experiment_tasks(
        self,
        *,
        experiment_id: int,
        brand: str,
        data_date: datetime.date,
        treatment_rows: list[dict],
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            result = self.create_experiment_tasks_in_session(
                db,
                experiment_id=experiment_id,
                brand=brand,
                data_date=data_date,
                treatment_rows=treatment_rows,
            )
            db.commit()
            return result
        finally:
            db.close()

    def create_experiment_tasks_in_session(
        self,
        db,
        *,
        experiment_id: int,
        brand: str,
        data_date: datetime.date,
        treatment_rows: list[dict],
    ) -> Dict[str, Any]:
        treatment_keys = [normalize_governance_query_key(brand, row.get("query")) for row in treatment_rows]
        if len(treatment_rows) != 20 or any(not key for key in treatment_keys) or len(set(treatment_keys)) != 20:
            raise ValueError("experiment treatment snapshot must contain exactly 20 canonical queries")
        assert_queries_governance_allowed_in_session(
            db,
            brand,
            [str(row.get("query") or "") for row in treatment_rows],
        )

        existing_tasks = db.query(TopQueryTask).filter(
            TopQueryTask.brand == brand,
            TopQueryTask.data_date == data_date,
        ).all()
        existing_by_query = self._converge_existing_tasks(db, brand, existing_tasks)
        active_existing_keys = {
            key
            for key, task in existing_by_query.items()
            if str(task.status or "").lower() != "superseded"
        }
        if active_existing_keys - set(treatment_keys):
            raise ValueError("experiment task scope conflicts with existing daily tasks")

        task_by_key: Dict[str, TopQueryTask] = {}
        for row in treatment_rows:
            query_text = str(row.get("query") or "").strip()
            query_key = normalize_governance_query_key(brand, query_text)
            existing_task = existing_by_query.get(query_key)
            raw_payload = dict(row)
            raw_payload["experiment_id"] = experiment_id
            if existing_task is None or str(existing_task.status or "").lower() == "superseded":
                existing_task = TopQueryTask(
                    brand=brand,
                    data_date=data_date,
                    query_text=query_text,
                    status="pending",
                    display_status="normal",
                )
                db.add(existing_task)
            existing_task.query_rank = row.get("query_rank") # type: ignore
            existing_task.search_uv = row.get("search_uv") # type: ignore
            existing_task.search_pv = row.get("search_pv") # type: ignore
            existing_task.ctr = row.get("ctr") # type: ignore
            existing_task.cvr = row.get("cvr") # type: ignore
            existing_task.avg_ctr = row.get("avg_ctr") # type: ignore
            existing_task.avg_cvr = row.get("avg_cvr") # type: ignore
            existing_task.query_pv_share = row.get("query_pv_share") # type: ignore
            existing_task.raw_payload = json.dumps(raw_payload, ensure_ascii=False) # type: ignore
            task_by_key[query_key] = existing_task

        db.flush()
        active_tasks = [
            task
            for task in db.query(TopQueryTask).filter(
                TopQueryTask.brand == brand,
                TopQueryTask.data_date == data_date,
            ).all()
            if str(task.status or "").lower() != "superseded"
        ]
        active_keys = {
            normalize_governance_query_key(brand, task.query_text)
            for task in active_tasks
        }
        if len(active_tasks) != 20 or active_keys != set(treatment_keys):
            raise ValueError("experiment task write did not produce exactly 20 treatment tasks")
        task_ids = [int(task_by_key[key].id) for key in treatment_keys]

        return {
            "experiment_id": experiment_id,
            "brand": brand,
            "data_date": data_date.isoformat(),
            "task_ids": task_ids,
            "top_query_count": len(task_ids),
        }

    @staticmethod
    def _dedupe_daily_rows(rows: list[dict]) -> list[dict]:
        result = []
        seen = set()
        for row in rows:
            key = normalize_query_key(row.get("query"))
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    @staticmethod
    def _converge_existing_tasks(
        db,
        brand: str,
        tasks: list[TopQueryTask],
        *,
        preserve_daily_query_identity: bool = False,
    ) -> Dict[str, TopQueryTask]:
        grouped: Dict[str, list[TopQueryTask]] = {}
        for task in tasks:
            key = (
                normalize_query_key(task.query_text)
                if preserve_daily_query_identity
                else normalize_governance_query_key(brand, task.query_text)
            )
            if key:
                grouped.setdefault(key, []).append(task)
        task_ids = [int(task.id) for task in tasks if task.id]
        action_task_ids = {
            int(task_id) for (task_id,) in db.query(TopQueryActionPlan.task_id).filter(
                TopQueryActionPlan.task_id.in_(task_ids)
            ).distinct().all()
        } if task_ids else set()
        winners: Dict[str, TopQueryTask] = {}
        for key, variants in grouped.items():
            winner = select_task_winner(variants, action_task_ids)
            winners[key] = winner
            for task in variants:
                if task.id != winner.id and str(task.status or "") != "superseded":
                    task.status = "superseded" # type: ignore
                    task.display_status = "superseded" # type: ignore
        return winners

    @staticmethod
    def _preferred_metric(metrics: list[BrandDailyMetric]) -> Optional[BrandDailyMetric]:
        if not metrics:
            return None

        def metric_priority(metric: BrandDailyMetric) -> tuple[int, int, float, int]:
            try:
                raw_payload = json.loads(metric.raw_payload or "{}")
            except json.JSONDecodeError:
                raw_payload = {}
            return (
                1 if raw_payload.get("metric_source") == "user_behavior" else 0,
                int(metric.top_query_count or 0),
                float(metric.top_query_pv or 0),
                int(metric.id or 0),
            )

        return max(metrics, key=metric_priority)

    def compare_daily_snapshot(self, brand: str, target_date: datetime.date, limit: Optional[int] = None) -> Dict[str, Any]:
        extractor = self._get_extractor_for_brand(brand)
        source_metric = extractor.get_brand_metric(brand, target_date)
        source_queries = extractor.get_top_queries(brand, target_date, limit or 150)

        db = SessionLocal()
        try:
            local_metric = self._preferred_metric(db.query(BrandDailyMetric).filter(
                BrandDailyMetric.brand == brand,
                BrandDailyMetric.data_date == target_date,
            ).all())
            local_tasks = db.query(TopQueryTask).filter(
                TopQueryTask.brand == brand,
                TopQueryTask.data_date == target_date,
                TopQueryTask.status != "superseded",
            ).order_by(TopQueryTask.search_pv.desc(), TopQueryTask.id.asc()).all()
        finally:
            db.close()

        source_by_query = {
            str(item.get("query") or "").strip(): item
            for item in source_queries
            if str(item.get("query") or "").strip()
        }
        local_by_query = {
            str(task.query_text or "").strip(): task
            for task in local_tasks
            if str(task.query_text or "").strip()
        }
        query_diffs = []
        all_queries = sorted(set(source_by_query.keys()) | set(local_by_query.keys()))
        for query_text in all_queries:
            source = source_by_query.get(query_text)
            local = local_by_query.get(query_text)
            mismatch_fields: list[str] = []
            if source is None:
                mismatch_fields.append("missing_source")
            if local is None:
                mismatch_fields.append("missing_local")
            if source and local:
                comparisons = {
                    "queryRank": (int(source.get("query_rank") or 0), int(local.query_rank or 0)),
                    "searchPv": (int(float(source.get("search_pv") or 0)), int(local.search_pv or 0)),
                    "ctr": (round(float(source.get("ctr") or 0), 6), round(float(local.ctr or 0), 6)),
                    "cvr": (round(float(source.get("cvr") or 0), 6), round(float(local.cvr or 0), 6)),
                    "avgCtr": (round(float(source.get("avg_ctr") or 0), 6), round(float(local.avg_ctr or 0), 6)),
                    "avgCvr": (round(float(source.get("avg_cvr") or 0), 6), round(float(local.avg_cvr or 0), 6)),
                }
                mismatch_fields.extend([field for field, values in comparisons.items() if values[0] != values[1]])
            if mismatch_fields:
                query_diffs.append({"queryText": query_text, "mismatchFields": mismatch_fields})

        return {
            "brand": brand,
            "dataDate": target_date.isoformat(),
            "source": {
                "topQueryCount": len(source_queries),
                "brandPv": int(float(source_metric.get("brand_pv") or 0)),
            },
            "local": {
                "topQueryCount": len(local_tasks),
                "brandPv": int(local_metric.brand_pv or 0) if local_metric else 0,
            },
            "queryDiffs": query_diffs,
        }

    def compare_snapshot_for_monitoring_date(self, brand: str, monitoring_date: datetime.date, limit: Optional[int] = None) -> Dict[str, Any]:
        target_date = monitoring_date - datetime.timedelta(days=1)
        diff = self.compare_daily_snapshot(brand, target_date, limit)
        return {
            "brand": brand,
            "monitoringDate": monitoring_date.isoformat(),
            **diff,
        }

    def rebuild_snapshot_for_monitoring_date(self, brand: str, monitoring_date: datetime.date, limit: Optional[int] = None) -> Dict[str, Any]:
        target_date = monitoring_date - datetime.timedelta(days=1)
        result = self.create_daily_tasks(brand, target_date, limit=limit or int(get_brand_business_config(brand).get("query_governance_extract_limit") or 150))
        return {
            "brand": brand,
            "monitoringDate": monitoring_date.isoformat(),
            "dataDate": target_date.isoformat(),
            "topQueryCount": result["top_query_count"],
            "taskIds": result["task_ids"],
        }

    @_task_execution_locked(skip_when_busy=True)
    def run_task(
        self,
        task_id: int,
        dry_run: bool = False,
        *,
        retry_failed: bool = False,
        world_knowledge_snapshot: Optional[WorldKnowledgeBatchSnapshot] = None,
    ) -> Dict[str, Any]:
        raise LegacyAgentLoopRetiredError()
    def approve_action(self, action_id: int, operator: str, reason: str = "") -> Dict[str, Any]:
        db = SessionLocal()
        try:
            action = db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            task_id = int(action.task_id)
            task = db.get(TopQueryTask, task_id)
            brand = str(action.brand or (task.brand if task else "") or "")
        finally:
            db.close()
        try:
            with top_query_brand_governance_lock(brand):
                with top_query_task_lock(task_id):
                    return self._approve_action_locked(
                        action_id,
                        operator=operator,
                        reason=reason,
                    )
        except TopQueryTaskLockBusy as exc:
            raise ActionStateConflictError(
                f"top query task is busy: {task_id}"
            ) from exc

    def _approve_action_locked(self, action_id: int, operator: str, reason: str = "") -> Dict[str, Any]:
        db = SessionLocal()
        try:
            action = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == action_id,
            ).with_for_update().first()
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            task = db.get(TopQueryTask, action.task_id)
            brand = str(action.brand or (task.brand if task else "") or "")
            query_text = str(action.query_text or (task.query_text if task else "") or "")
            assert_query_governance_allowed(brand, query_text)
            status = str(action.status or "").lower()
            env = get_env()
            if (
                str(action.action_type or "") == "query_demote"
                and env == "prod"
                and status in LEGACY_PENDING_STATUSES
            ):
                payload = self._load_action_payload(action)
                if should_block_legacy_query_demotion(
                    env=env,
                    status=status,
                    payload=payload,
                ):
                    raise LegacyQueryDemotionContractError(action_id, status)
            if status == "approved":
                return {"action_id": action_id, "status": "approved"}
            if status != "waiting_human_approval":
                raise ActionStateConflictError(f"action cannot be approved from status: {action.status}")
            try:
                response = json.loads(action.response_payload or "{}")
            except (TypeError, json.JSONDecodeError):
                response = {}
            if not isinstance(response, dict):
                response = {}
            approved_at = datetime.datetime.now().isoformat()
            lifecycle = response.get("lifecycle") if isinstance(response.get("lifecycle"), dict) else {}
            response = {
                **response,
                "operator": operator,
                "reason": reason,
                "approved_at": approved_at,
                "lifecycle": {
                    **lifecycle,
                    "approvedAt": approved_at,
                    "operator": operator,
                },
            }
            encoded_response = _bounded_action_response_payload(response)
            db.rollback()
            claimed = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == action_id,
                TopQueryActionPlan.status == "waiting_human_approval",
            ).update({
                TopQueryActionPlan.status: "approved",
                TopQueryActionPlan.response_payload: encoded_response,
            }, synchronize_session=False)
            if claimed != 1:
                db.rollback()
                current = db.get(TopQueryActionPlan, action_id)
                if not current:
                    raise ActionValidationError(f"top query action not found: {action_id}")
                if str(current.status or "").lower() == "approved":
                    return {"action_id": action_id, "status": "approved"}
                raise ActionStateConflictError(
                    f"action cannot be approved from status: {current.status}"
                )
            db.commit()
            return {"action_id": action_id, "status": "approved"}
        finally:
            db.close()

    def reject_action(self, action_id: int, operator: str, reason: str = "") -> Dict[str, Any]:
        db = SessionLocal()
        try:
            action = db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            task_id = int(action.task_id)
            task = db.get(TopQueryTask, task_id)
            brand = str(action.brand or (task.brand if task else "") or "")
        finally:
            db.close()
        try:
            with top_query_brand_governance_lock(brand):
                with top_query_task_lock(task_id):
                    return self._reject_action_locked(
                        action_id,
                        operator=operator,
                        reason=reason,
                    )
        except TopQueryTaskLockBusy as exc:
            raise ActionStateConflictError(
                f"top query task is busy: {task_id}"
            ) from exc

    def _reject_action_locked(self, action_id: int, operator: str, reason: str = "") -> Dict[str, Any]:
        db = SessionLocal()
        try:
            action = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == action_id,
            ).with_for_update().first()
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            task = db.get(TopQueryTask, action.task_id)
            brand = str(action.brand or (task.brand if task else "") or "")
            query_text = str(action.query_text or (task.query_text if task else "") or "")
            assert_query_governance_allowed(brand, query_text)
            status = str(action.status or "").lower()
            if status == "rejected":
                return {"action_id": action_id, "status": "rejected"}
            if status not in {"waiting_human_approval", "approved"}:
                raise ActionStateConflictError(
                    f"action cannot be rejected from status: {status or 'unknown'}"
                )
            try:
                response = json.loads(action.response_payload or "{}")
            except (TypeError, json.JSONDecodeError):
                response = {}
            if not isinstance(response, dict):
                response = {}
            response["rejection"] = {
                "operator": operator,
                "reason": reason,
                "rejectedAt": datetime.datetime.now().isoformat(),
            }
            updated = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == action_id,
                TopQueryActionPlan.status == action.status,
                TopQueryActionPlan.status.in_(["waiting_human_approval", "approved"]),
            ).update({
                TopQueryActionPlan.status: "rejected",
                TopQueryActionPlan.response_payload: _bounded_action_response_payload(response),
            }, synchronize_session=False)
            if updated != 1:
                raise ActionStateConflictError("action rejection state changed")
            db.commit()
            return {"action_id": action_id, "status": "rejected"}
        finally:
            db.close()

    def execute_approved_action(
        self,
        action_id: int,
        dry_run: bool = False,
        external_request_id: Optional[str] = None,
        expected_config_version: str | None = None,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            action = db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            task_id = int(action.task_id)
            task = db.get(TopQueryTask, task_id)
            brand = str(action.brand or (task.brand if task else "") or "")
            query_text = str(action.query_text or (task.query_text if task else "") or "")
            assert_query_governance_allowed(brand, query_text)
        finally:
            db.close()
        try:
            with top_query_brand_governance_lock(brand):
                with top_query_task_lock(task_id):
                    return self._execute_approved_action_locked(
                        action_id,
                        dry_run=dry_run,
                        external_request_id=external_request_id,
                        expected_config_version=expected_config_version,
                    )
        except TopQueryTaskLockBusy as exc:
            raise ActionStateConflictError(
                f"top query task is busy: {task_id}"
            ) from exc

    def _anchor_inventory_demotion_task(
        self,
        db,
        *,
        brand: str,
        query_key: str,
        query_text: str,
    ) -> Optional[TopQueryTask]:
        """为仅存在于当前治理管线的 Query 补建降桶锚点任务。

        权威降桶执行链路（动作分组、回读与对账）要求动作挂在 top_query_task 上；
        旧 Top Query 管线停写后，这里用最新的 query_governance_task 落数据补一个
        完成态锚点，替代原先“找不到旧任务即拒绝降桶”的行为。
        """
        from src.governance.query_identity import _ATLAS_GOVERNANCE_SUFFIXES, normalize_query_key
        from src.query_governance.models import QueryGovernanceTask

        candidate_queries = [query_text, query_key]
        if normalize_query_key(brand) == "atlas":
            candidate_queries.extend(f"{query_key} {s}" for s in _ATLAS_GOVERNANCE_SUFFIXES)
            candidate_queries.extend(f"{query_text} {s}" for s in _ATLAS_GOVERNANCE_SUFFIXES)

        seen: set[str] = set()
        unique_candidates = [q for q in candidate_queries if q and not (q in seen or seen.add(q))]

        matched_date_row = (
            db.query(QueryGovernanceTask.data_date)
            .filter(
                QueryGovernanceTask.brand == brand,
                QueryGovernanceTask.query_text.in_(unique_candidates),
            )
            .order_by(
                QueryGovernanceTask.data_date.desc(),
                QueryGovernanceTask.id.desc(),
            )
            .first()
        )
        matched_data_date = matched_date_row[0] if matched_date_row is not None else None

        if matched_data_date is None:
            governance_tasks = (
                db.query(QueryGovernanceTask.query_text, QueryGovernanceTask.data_date)
                .filter(QueryGovernanceTask.brand == brand)
                .order_by(
                    QueryGovernanceTask.data_date.desc(),
                    QueryGovernanceTask.id.desc(),
                )
                .limit(1000)
                .all()
            )
            for cand_query_text, cand_data_date in governance_tasks:
                if normalize_governance_query_key(brand, cand_query_text) == query_key:
                    matched_data_date = cand_data_date
                    break

        if matched_data_date is None:
            return None
        anchor = TopQueryTask(
            brand=brand,
            query_text=query_text,
            data_date=matched_data_date,
            status="completed",
            display_status="normal",
        )
        db.add(anchor)
        db.flush()
        return anchor

    def execute_inventory_monitor_demotion(
        self,
        brand: str,
        query: str,
        candidates: list[Dict[str, Any]],
        recovery_candidates: Optional[list[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        normalized_brand = str(brand or "").strip()
        normalized_query = transform_governance_payload_query(normalized_brand, query)
        normalized_candidates: Dict[str, Dict[str, Any]] = {}
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            product_id = str(candidate.get("product_id") or "").strip()
            if not product_id:
                continue
            try:
                broken_rate_percent = int(round(float(candidate.get("broken_rate_percent"))))
            except (TypeError, ValueError):
                continue
            if broken_rate_percent < 0 or broken_rate_percent > 100:
                continue
            normalized_candidates[product_id] = {
                "product_name": str(candidate.get("product_name") or ""),
                "broken_rate_percent": broken_rate_percent,
            }
        normalized_recovery_candidates: Dict[str, Dict[str, Any]] = {}
        for candidate in recovery_candidates or []:
            if not isinstance(candidate, dict):
                continue
            product_id = str(candidate.get("product_id") or "").strip()
            if not product_id:
                continue
            normalized_recovery_candidates[product_id] = {
                "product_name": str(candidate.get("product_name") or ""),
                "broken_rate_percent": candidate.get("broken_rate_percent"),
            }
        if (
            not normalized_brand
            or not normalized_query
            or (not normalized_candidates and not normalized_recovery_candidates)
        ):
            raise ActionValidationError("inventory monitor query_demote requires brand, query and candidates")

        requested_product_ids = sorted(normalized_candidates)
        requested_recovery_ids = sorted(
            set(normalized_recovery_candidates) - set(requested_product_ids)
        )
        with top_query_brand_governance_lock(normalized_brand):
            assert_query_governance_allowed(normalized_brand, normalized_query)
            db = SessionLocal()
            try:
                query_key = normalize_governance_query_key(
                    normalized_brand,
                    normalized_query,
                )
                visible_tasks = db.query(TopQueryTask).filter(
                    TopQueryTask.brand == normalized_brand,
                    func.coalesce(TopQueryTask.query_source, "top_query") == "top_query",
                ).all()
                matching_tasks = [
                    candidate
                    for candidate in visible_tasks
                    if normalize_governance_query_key(
                        normalized_brand,
                        candidate.query_text,
                    )
                    == query_key
                ]
                latest_data_date = max(
                    (candidate.data_date or datetime.date.min for candidate in matching_tasks),
                    default=None,
                )
                latest_tasks = [
                    candidate
                    for candidate in matching_tasks
                    if (candidate.data_date or datetime.date.min) == latest_data_date
                    and str(candidate.status or "").lower()
                    not in {"cancelled", "superseded"}
                ]
                latest_task_ids = [int(candidate.id) for candidate in latest_tasks]
                action_task_ids = {
                    int(task_id)
                    for (task_id,) in db.query(TopQueryActionPlan.task_id).filter(
                        TopQueryActionPlan.task_id.in_(latest_task_ids)
                    ).distinct().all()
                } if latest_task_ids else set()
                task = select_task_winner(latest_tasks, action_task_ids)
                if task is None:
                    # 治理主链迁移到 query_governance 后 top_query_task 不再产生新行，
                    # 断码降桶改以当前治理任务为锚点补建任务行，动作事实链路保持可回读。
                    task = self._anchor_inventory_demotion_task(
                        db,
                        brand=normalized_brand,
                        query_key=query_key,
                        query_text=normalized_query,
                    )
                if task is None:
                    raise ActionValidationError(
                        "inventory monitor query_demote requires visible top query task"
                    )
                matching_actions = db.query(TopQueryActionPlan).filter(
                    TopQueryActionPlan.brand == normalized_brand,
                    TopQueryActionPlan.query_text == normalized_query,
                    TopQueryActionPlan.check_key == "inventory",
                    TopQueryActionPlan.action_type == "query_demote",
                ).order_by(TopQueryActionPlan.id.desc()).all()
                covered_add_ids = set()
                covered_remove_ids = set()
                covered_action = None
                reusable_action = None
                blocked_action = None
                for action in matching_actions:
                    payload = self._load_action_payload(action)
                    if payload.get("source") != "inventory_monitor":
                        continue
                    action_add_ids, action_remove_ids = self._query_demote_action_deltas(payload)
                    status = str(action.status or "").lower()
                    if status == "approved":
                        if reusable_action is None:
                            reusable_action = action
                        continue
                    if status in {"executing", "execution_unknown", "reconciling"}:
                        blocked_action = action
                        break
                    if status == "verification_failed":
                        continue
                    if status == "verifying":
                        covered_add_ids.update(action_add_ids)
                        covered_remove_ids.update(action_remove_ids)
                        if covered_action is None:
                            covered_action = action

                if blocked_action is not None:
                    return {
                        "action_id": int(blocked_action.id),
                        "status": str(blocked_action.status or ""),
                        "applied_product_count": 0,
                        "recovered_product_count": 0,
                        "reused": True,
                        "blocked": True,
                    }

                if reusable_action is not None:
                    action_id = int(reusable_action.id)
                    action_payload = self._load_action_payload(reusable_action)
                    action_add_ids, action_remove_ids = self._query_demote_action_deltas(action_payload)
                    execute_existing = True
                    applied_product_count = len(action_add_ids)
                    recovered_product_count = len(action_remove_ids)
                else:
                    product_ids = [
                        product_id
                        for product_id in requested_product_ids
                        if product_id not in covered_add_ids
                    ]
                    remove_product_ids = [
                        product_id
                        for product_id in requested_recovery_ids
                        if product_id not in covered_remove_ids
                    ]
                    if not product_ids and not remove_product_ids:
                        return {
                            "action_id": int(covered_action.id) if covered_action else 0,
                            "status": "verifying",
                            "applied_product_count": 0,
                            "recovered_product_count": 0,
                            "reused": True,
                            "blocked": False,
                        }
                    execute_existing = False
                    applied_product_count = len(product_ids)
                    recovered_product_count = len(remove_product_ids)
                    identity = json.dumps({
                        "brand": normalized_brand.casefold(),
                        "query": normalized_query.casefold(),
                        "add": product_ids,
                        "remove": remove_product_ids,
                    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    event_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                    event_prefix = f"inventory-monitor-query-demote:{event_digest}:event:"
                    prior_events = [
                        action for action in matching_actions
                        if str(action.idempotency_key or "").startswith(event_prefix)
                    ]
                    action = TopQueryActionPlan(
                        task_id=int(task.id),
                        idempotency_key=f"{event_prefix}{len(prior_events) + 1}",
                        brand=normalized_brand,
                        query_text=normalized_query,
                        check_key="inventory",
                        tool_key="query_demote",
                        action_type="query_demote",
                        execution_mode="auto",
                        status="approved",
                        request_payload=json.dumps({
                            "brand": normalized_brand,
                            "query": normalized_query,
                            "product_ids": product_ids,
                            "remove_product_ids": remove_product_ids,
                            "product_names": {
                                **{
                                    product_id: normalized_candidates[product_id]["product_name"]
                                    for product_id in product_ids
                                },
                                **{
                                    product_id: normalized_recovery_candidates[product_id]["product_name"]
                                    for product_id in remove_product_ids
                                },
                            },
                            "broken_rate_percent": (
                                max(
                                    normalized_candidates[product_id]["broken_rate_percent"]
                                    for product_id in product_ids
                                )
                                if product_ids
                                else None
                            ),
                            "broken_rate_percent_by_product": {
                                product_id: normalized_candidates[product_id]["broken_rate_percent"]
                                for product_id in product_ids
                            },
                            "check_key": "inventory",
                            "action_type": "query_demote",
                            "source": "inventory_monitor",
                            "execution_contract": AUTHORITATIVE_QUERY_DEMOTION_V1,
                        }, ensure_ascii=False),
                        reason="断码率过高，按 Query 降桶",
                    )
                    db.add(action)
                    db.commit()
                    action_id = int(action.id)
            finally:
                db.close()

        self.execute_approved_action(action_id)
        result_db = SessionLocal()
        try:
            action = result_db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            final_status = str(action.status or "")
            return {
                "action_id": action_id,
                "status": final_status,
                "applied_product_count": (
                    applied_product_count if final_status == "verifying" else 0
                ),
                "recovered_product_count": (
                    recovered_product_count if final_status == "verifying" else 0
                ),
                "reused": execute_existing,
                "blocked": False,
            }
        finally:
            result_db.close()

    def _execute_approved_action_locked(
        self,
        action_id: int,
        dry_run: bool = False,
        external_request_id: Optional[str] = None,
        expected_config_version: str | None = None,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            action = db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            task = db.get(TopQueryTask, action.task_id)
            brand = str(action.brand or (task.brand if task else "") or "")
            query_text = str(action.query_text or (task.query_text if task else "") or "")
            assert_query_governance_allowed(brand, query_text)
            status = str(action.status or "").lower()
            env = get_env()
            if str(action.action_type or "") == "query_demote":
                payload = self._load_action_payload(action)
                if should_block_legacy_query_demotion(
                    env=env,
                    status=status,
                    payload=payload,
                ):
                    raise LegacyQueryDemotionContractError(action_id, status)
                if (
                    status in {"approved", "waiting_human_approval", "pending_confirmation"}
                    and uses_authoritative_query_demotion(payload)
                ):
                    if str(action.execution_mode or "") == "human_approval":
                        raise ActionValidationError(
                            "human authoritative query_demote requires task-level preview execution"
                        )
                    if dry_run:
                        return self._preview_query_demote_execution(
                            [action.id],
                            single_action_id=action.id,
                        )
                    group_results = self._execute_query_demote_group(
                        [action.id],
                        dry_run=False,
                        expected_config_version=expected_config_version,
                    )
                    if (
                        not isinstance(group_results, list)
                        or not group_results
                        or not isinstance(group_results[0], dict)
                    ):
                        raise ActionExecutionError(
                            "query_demote invalid group execution result"
                        )
                    return group_results[0]
            started_at = datetime.datetime.now()
            external_request_id = str(external_request_id or uuid.uuid4())
            claimed = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == action_id,
                TopQueryActionPlan.status.in_(["approved", "waiting_human_approval", "pending_confirmation"]),
            ).update({
                TopQueryActionPlan.status: "executing",
                TopQueryActionPlan.external_request_id: func.coalesce(
                    TopQueryActionPlan.external_request_id,
                    external_request_id,
                ),
                TopQueryActionPlan.execution_started_at: started_at,
                TopQueryActionPlan.execution_finished_at: None,
                TopQueryActionPlan.execution_lease_expires_at: started_at + datetime.timedelta(
                    seconds=self.ACTION_EXECUTION_LEASE_SECONDS,
                ),
            }, synchronize_session=False)
            if claimed != 1:
                action = db.get(TopQueryActionPlan, action_id)
                if not action:
                    raise ActionValidationError(f"top query action not found: {action_id}")
                if (
                    action.status == "executing"
                    and action.execution_lease_expires_at
                    and action.execution_lease_expires_at <= started_at
                ):
                    expired = db.query(TopQueryActionPlan).filter(
                        TopQueryActionPlan.id == action_id,
                        TopQueryActionPlan.status == "executing",
                        TopQueryActionPlan.execution_lease_expires_at <= started_at,
                    ).update({
                        TopQueryActionPlan.status: "execution_unknown",
                        TopQueryActionPlan.execution_lease_expires_at: None,
                    }, synchronize_session=False)
                    if expired == 1:
                        db.commit()
                        raise ActionStateConflictError(
                            "action execution lease expired; reconcile external status before retry"
                        )
                raise ActionStateConflictError(f"action is not executable from status: {action.status}")
            db.commit()
            action = db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            terminal_execution_fence = action.execution_lease_expires_at
            terminal_action_type = str(action.action_type or "")
            approval_metadata = _action_approval_metadata(action.response_payload)
            lifecycle = {
                "proposedAt": action.created_at.isoformat() if action.created_at else None,
                "approvedAt": approval_metadata.get("approved_at") or approval_metadata.get("approvedAt"),
                "operator": approval_metadata.get("operator"),
                "executionStartedAt": started_at.isoformat(),
                "executionFinishedAt": None,
                "durationSeconds": None,
            }
            result = {
                **approval_metadata,
                "action_id": action_id,
                "tool_key": action.tool_key,
                "dry_run": dry_run,
                "lifecycle": lifecycle,
            }
            try:
                payload = self._load_action_payload(action)
                assert_query_governance_allowed(brand, query_text)
                if not dry_run:
                    result.update(self._execute_real_action(action, payload))
            except Exception as exc:
                if action.status == "superseded":
                    raise
                finished_at = datetime.datetime.now()
                lifecycle["executionFinishedAt"] = finished_at.isoformat()
                lifecycle["durationSeconds"] = round((finished_at - started_at).total_seconds(), 6)
                result["executed_at"] = finished_at.isoformat()
                action.status = "execution_unknown" if self._is_ambiguous_external_failure(exc) else "failed" # type: ignore
                action.execution_finished_at = finished_at # type: ignore
                action.execution_lease_expires_at = None # type: ignore
                sanitized_error = sanitize_action_error(exc)
                action.error_message = sanitized_error # type: ignore
                error_details = getattr(exc, "details", {}) if isinstance(getattr(exc, "details", {}), dict) else {}
                error_details = sanitize_action_error_details(error_details)
                if isinstance(exc, ActionExecutionError):
                    exc.details = error_details
                failure_result = {**result, **error_details, "error": sanitized_error, "lifecycle": lifecycle}
                if action.action_type == "query_demote":
                    failure_result.update(
                        self._query_demote_persisted_concurrency_evidence(
                            self._preview_json_object(action.before_snapshot)
                        )
                        or self._query_demote_persisted_concurrency_evidence(
                            self._preview_json_object(action.response_payload)
                        )
                        or self._query_demote_unknown_concurrency_evidence()
                    )
                self._set_verification_timestamp(failure_result, finished_at)
                action.response_payload = _bounded_action_response_payload(failure_result) # type: ignore
                try:
                    if action.action_type == "tag_bucket_config" and failure_result.get("world_knowledge_verification"):
                        self._persist_tag_bucket_verification(action, failure_result, db=db)
                    db.commit()
                except Exception as audit_exc:
                    db.rollback()
                    failure_result["audit_error"] = sanitize_action_error(audit_exc)
                    self._commit_action_failure_after_rollback(db, action_id, failure_result, exc)
                raise

            finished_at = datetime.datetime.now()
            lifecycle["executionFinishedAt"] = finished_at.isoformat()
            lifecycle["durationSeconds"] = round((finished_at - started_at).total_seconds(), 6)
            result["executed_at"] = finished_at.isoformat()
            self._set_verification_timestamp(result, finished_at)
            if action.action_type == "tag_bucket_config":
                action.before_snapshot = _bounded_tag_bucket_payload(
                    result.get("before_snapshot") or {},
                ) # type: ignore
                action.after_snapshot = _bounded_tag_bucket_payload({
                    **(result.get("after_snapshot") or {}),
                    "world_knowledge_verification": result.get("world_knowledge_verification") or {},
                }) # type: ignore
            success_status = "executed"
            action.status = "dry_run_executed" if dry_run else success_status # type: ignore
            action.execution_finished_at = finished_at # type: ignore
            action.execution_lease_expires_at = None # type: ignore
            action.response_payload = _bounded_action_response_payload(result) # type: ignore
            try:
                if not dry_run and action.action_type == "tag_bucket_config":
                    self._persist_tag_bucket_verification(action, result, db=db)
                db.commit()
            except Exception as audit_exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                try:
                    recovered = self._retry_terminal_action_persistence(
                        action_id,
                        expected_execution_fence=terminal_execution_fence,
                        status="dry_run_executed" if dry_run else success_status,
                        execution_finished_at=finished_at,
                        response_payload=_bounded_action_response_payload(result),
                        action_type=terminal_action_type,
                        result=result,
                    )
                except Exception:
                    recovered = False
                if recovered:
                    return result
                self._commit_action_failure_after_rollback(
                    db,
                    action_id,
                    result,
                    audit_exc,
                    status="execution_unknown" if not dry_run else "failed",
                )
                raise
            return result
        finally:
            db.close()

    def reconcile_unknown_action(self, action_id: int) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            action = db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            task_id = int(action.task_id)
            task = db.get(TopQueryTask, task_id)
            brand = str(action.brand or (task.brand if task else "") or "")
        finally:
            db.close()
        try:
            with top_query_brand_governance_lock(brand):
                with top_query_task_lock(task_id):
                    return self._reconcile_unknown_action_locked(action_id)
        except TopQueryTaskLockBusy as exc:
            raise ActionStateConflictError(
                f"top query task is busy: {task_id}"
            ) from exc

    def reconcile_expired_query_demote_execution(self, action_id: int) -> Dict[str, Any]:
        lookup_db = SessionLocal()
        try:
            action = lookup_db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            task_id = int(action.task_id)
            brand = str(action.brand or "")
        finally:
            lookup_db.close()
        try:
            with top_query_brand_governance_lock(brand):
                with top_query_task_lock(task_id):
                    db = SessionLocal()
                    try:
                        seed = db.get(TopQueryActionPlan, action_id)
                        now = datetime.datetime.now()
                        if (
                            not seed
                            or str(seed.status or "") != "executing"
                            or (
                                seed.execution_lease_expires_at is not None
                                and seed.execution_lease_expires_at > now
                            )
                            or not self._is_authoritative_query_demote_action(seed)
                        ):
                            raise ActionStateConflictError(
                                "query demote execution lease is not expired"
                            )
                        metadata_state, metadata = self._query_demote_group_metadata_result(seed)
                        if metadata_state != "valid" or metadata is None:
                            raise ActionValidationError(
                                f"query demote group metadata is {metadata_state}"
                            )
                        action_ids = list(metadata["actionIds"])
                        group = db.query(TopQueryActionPlan).filter(
                            TopQueryActionPlan.id.in_(action_ids),
                        ).order_by(TopQueryActionPlan.id.asc()).all()
                        if (
                            len(group) != len(action_ids)
                            or any(
                                str(peer.status or "") != "executing"
                                or (
                                    peer.execution_lease_expires_at is not None
                                    and peer.execution_lease_expires_at > now
                                )
                                or self._query_demote_group_metadata(peer) != metadata
                                for peer in group
                            )
                        ):
                            raise ActionStateConflictError(
                                "query demote expired execution group is incomplete"
                            )
                        transitioned = db.query(TopQueryActionPlan).filter(
                            TopQueryActionPlan.id.in_(action_ids),
                            TopQueryActionPlan.status == "executing",
                            or_(
                                TopQueryActionPlan.execution_lease_expires_at.is_(None),
                                TopQueryActionPlan.execution_lease_expires_at <= now,
                            ),
                        ).update({
                            TopQueryActionPlan.status: "execution_unknown",
                            TopQueryActionPlan.execution_lease_expires_at: None,
                        }, synchronize_session=False)
                        if transitioned != len(action_ids):
                            db.rollback()
                            raise ActionStateConflictError(
                                "query demote expired execution group changed"
                            )
                        db.commit()
                    finally:
                        db.close()
                    return self._reconcile_query_demote_group(action_id, metadata)
        except TopQueryTaskLockBusy as exc:
            raise ActionStateConflictError(
                f"top query task is busy: {task_id}"
            ) from exc

    def reconcile_expired_action_execution(self, action_id: int) -> Dict[str, Any]:
        lookup_db = SessionLocal()
        try:
            action = lookup_db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            if str(action.action_type or "") == "query_demote":
                raise ActionValidationError(
                    "generic expired execution reconciliation excludes query_demote"
                )
            task_id = int(action.task_id)
            brand = str(action.brand or "")
        finally:
            lookup_db.close()
        try:
            with top_query_brand_governance_lock(brand):
                with top_query_task_lock(task_id):
                    db = SessionLocal()
                    try:
                        seed = db.get(TopQueryActionPlan, action_id)
                        now = datetime.datetime.now()
                        if (
                            not seed
                            or str(seed.status or "") != "executing"
                            or str(seed.action_type or "") == "query_demote"
                            or not str(seed.external_request_id or "").strip()
                            or (
                                seed.execution_lease_expires_at is not None
                                and seed.execution_lease_expires_at > now
                            )
                        ):
                            raise ActionStateConflictError(
                                "generic action execution lease is not expired"
                            )
                        transitioned = db.query(TopQueryActionPlan).filter(
                            TopQueryActionPlan.id == action_id,
                            TopQueryActionPlan.status == "executing",
                            TopQueryActionPlan.action_type != "query_demote",
                            TopQueryActionPlan.external_request_id.is_not(None),
                            or_(
                                TopQueryActionPlan.execution_lease_expires_at.is_(None),
                                TopQueryActionPlan.execution_lease_expires_at <= now,
                            ),
                        ).update({
                            TopQueryActionPlan.status: "execution_unknown",
                            TopQueryActionPlan.execution_lease_expires_at: None,
                        }, synchronize_session=False)
                        if transitioned != 1:
                            db.rollback()
                            raise ActionStateConflictError(
                                "generic action expired execution changed"
                            )
                        db.commit()
                    finally:
                        db.close()
                    return self._reconcile_unknown_action_locked(action_id)
        except TopQueryTaskLockBusy as exc:
            raise ActionStateConflictError(
                f"top query task is busy: {task_id}"
            ) from exc

    def _reconcile_unknown_action_locked(self, action_id: int) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            seed_action = db.get(TopQueryActionPlan, action_id)
            if not seed_action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            seed_payload = self._load_action_payload(seed_action)
            if (
                str(seed_action.action_type or "") == "query_demote"
                and uses_authoritative_query_demotion(seed_payload)
            ):
                metadata_state, group_metadata = self._query_demote_group_metadata_result(
                    seed_action
                )
                if metadata_state != "valid" or group_metadata is None:
                    raise ActionValidationError(
                        f"query demote group metadata is {metadata_state}"
                    )
                db.close()
                return self._reconcile_query_demote_group(action_id, group_metadata)
            reconciliation_started_at = datetime.datetime.now()
            claimed_until = _verification_lease_token(
                reconciliation_started_at + datetime.timedelta(
                    seconds=self.ACTION_EXECUTION_LEASE_SECONDS,
                )
            )
            claimed = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == action_id,
                or_(
                    TopQueryActionPlan.status == "execution_unknown",
                    (
                        (TopQueryActionPlan.status == "reconciling")
                        & (
                            TopQueryActionPlan.execution_lease_expires_at.is_(None)
                            | (
                                TopQueryActionPlan.execution_lease_expires_at
                                <= reconciliation_started_at
                            )
                        )
                    ),
                ),
            ).update({
                TopQueryActionPlan.status: "reconciling",
                TopQueryActionPlan.execution_lease_expires_at: claimed_until,
            }, synchronize_session=False)
            if claimed != 1:
                action = db.get(TopQueryActionPlan, action_id)
                if not action:
                    raise ActionValidationError(f"top query action not found: {action_id}")
                raise ActionStateConflictError(f"action cannot be reconciled from status: {action.status}")
            db.commit()
            action = db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            payload = self._load_action_payload(action)
            external_request_id = str(action.external_request_id or "").strip()
            if not external_request_id:
                action.status = "execution_unknown" # type: ignore
                action.execution_lease_expires_at = None # type: ignore
                db.commit()
                raise ActionValidationError("execution_unknown action requires external_request_id")

            probe = getattr(self.operational_service, "get_top_query_action_status", None)
            if action.action_type in {"kg_tag", "kg_relation"}:
                probe = getattr(self.kg_client, "get_top_query_action_status", None) or probe
            if not callable(probe):
                action.status = "execution_unknown" # type: ignore
                action.execution_lease_expires_at = None # type: ignore
                db.commit()
                raise ActionExecutionError("external action status lookup is not supported")
            try:
                probe_payload = dict(payload)
                probe_payload.setdefault("brand", str(action.brand or ""))
                probe_payload.setdefault("query", str(action.query_text or ""))
                external_result = probe(
                    action_type=str(action.action_type or ""),
                    external_request_id=external_request_id,
                    payload=probe_payload,
                )
            except Exception:
                action.status = "execution_unknown" # type: ignore
                action.execution_lease_expires_at = None # type: ignore
                db.commit()
                raise

            if not isinstance(external_result, dict):
                external_result = {"status": "unknown", "response": external_result}
            external_status = str(external_result.get("status") or "unknown").strip().lower()
            status_map = {
                "completed": "executed",
                "succeeded": "executed",
                "success": "executed",
                "failed": "failed",
                "not_found": "approved",
            }
            next_status = status_map.get(external_status, "execution_unknown")
            finished_at = datetime.datetime.now()
            action.status = next_status # type: ignore
            action.execution_lease_expires_at = None # type: ignore
            action.execution_finished_at = finished_at if next_status in {"executed", "failed"} else None # type: ignore
            try:
                response = json.loads(action.response_payload or "{}")
            except (TypeError, json.JSONDecodeError):
                response = {}
            if not isinstance(response, dict):
                response = {}
            response["external_reconciliation"] = sanitize_action_error_details({
                "checked_at": finished_at.isoformat(),
                "external_request_id": external_request_id,
                "external_status": external_status,
                "result": external_result.get("response"),
            })
            action.response_payload = _bounded_action_response_payload(response) # type: ignore
            if next_status == "failed":
                action.error_message = sanitize_action_error(
                    external_result.get("error") or "external action reported failed"
                ) # type: ignore
            elif next_status in {"executed", "approved"}:
                action.error_message = None # type: ignore
            db.commit()
            return {
                "action_id": action_id,
                "status": next_status,
                "external_status": external_status,
                "external_request_id": external_request_id,
            }
        finally:
            db.close()

    def _validated_authoritative_query_demote_group(
        self,
        db: Any,
        seed_action: TopQueryActionPlan,
        *,
        required_status: str,
    ) -> tuple[Dict[str, Any], list[TopQueryActionPlan]]:
        metadata_state, metadata = self._query_demote_group_metadata_result(seed_action)
        if metadata_state != "valid" or metadata is None:
            raise ActionValidationError(
                f"query demote group metadata is {metadata_state}"
            )
        action_ids = list(metadata["actionIds"])
        required_metadata_keys = set(metadata)
        group = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.id.in_(action_ids),
        ).order_by(TopQueryActionPlan.id.asc()).all()
        if (
            len(group) != len(action_ids)
            or [int(action.id) for action in group] != action_ids
            or int(seed_action.id) not in action_ids
            or int(metadata["leaderActionId"]) not in action_ids
            or any(
                str(action.status or "") != required_status
                or not self._is_authoritative_query_demote_action(action)
                or self._query_demote_group_metadata(action) != metadata
                or self._preview_json_object(action.before_snapshot).get("complete") is not True
                or not required_metadata_keys.issubset(
                    self._preview_json_object(action.before_snapshot)
                )
                or not required_metadata_keys.issubset(
                    self._preview_json_object(action.after_snapshot)
                )
                or not required_metadata_keys.issubset(
                    self._preview_json_object(action.response_payload)
                )
                for action in group
            )
        ):
            raise ActionValidationError(
                "query demote authoritative group identity is incomplete"
            )
        task_id, brand, normalized_query = self._authoritative_query_demote_group_identity(
            db,
            group,
        )
        if (
            task_id != int(seed_action.task_id)
            or normalize_query_key(metadata["brand"]) != normalize_query_key(brand)
            or metadata["normalizedQuery"] != normalized_query
        ):
            raise ActionValidationError(
                "query demote authoritative group identity mismatch"
            )
        return metadata, group

    def refresh_query_demotion_verification(self, action_id: int) -> Dict[str, Any]:
        lookup_db = SessionLocal()
        try:
            seed_action = lookup_db.get(TopQueryActionPlan, action_id)
            if not seed_action:
                raise ActionValidationError(f"top query action not found: {action_id}")
            if str(seed_action.action_type or "") != "query_demote":
                raise ActionValidationError("query demotion verification requires query_demote action")
            current_status = str(seed_action.status or "")
            if not self._is_authoritative_query_demote_action(seed_action):
                raise LegacyQueryDemotionContractError(action_id, current_status)
            if current_status == "rollback_pending":
                return {
                    "action_id": action_id,
                    "status": "rollback_pending",
                    "skipped": "rollback_verification_not_available",
                }
            if current_status in {
                "verified",
                "verification_failed",
                "verification_inconclusive",
            }:
                return {"action_id": action_id, "status": current_status}
            if current_status != "verifying":
                raise ActionStateConflictError(
                    f"action cannot refresh verification from status: {current_status}"
                )

            metadata, group = self._validated_authoritative_query_demote_group(
                lookup_db,
                seed_action,
                required_status="verifying",
            )
            action_ids = list(metadata["actionIds"])
            seed_group_key = f"request:{metadata['groupExternalRequestId']}"
            task_id = int(seed_action.task_id)
            brand = str(metadata["brand"])
            query_text = str(metadata["normalizedQuery"])
        finally:
            lookup_db.close()

        claim_db = SessionLocal()
        claimed_until = None
        try:
            claimed_at = _verification_lease_token(_verification_now())
            claimed_until = _verification_lease_token(
                claimed_at + datetime.timedelta(
                    seconds=QUERY_DEMOTION_VERIFICATION_LEASE_SECONDS,
                )
            )
            claimed = claim_db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(action_ids),
                TopQueryActionPlan.status == "verifying",
                (
                    TopQueryActionPlan.execution_lease_expires_at.is_(None)
                    | (TopQueryActionPlan.execution_lease_expires_at <= claimed_at)
                ),
            ).update({
                TopQueryActionPlan.execution_lease_expires_at: claimed_until,
            }, synchronize_session=False)
            if claimed != len(action_ids):
                claim_db.rollback()
                return {"action_id": action_id, "status": "busy"}
            claim_db.commit()
        finally:
            claim_db.close()

        target_db = SessionLocal()
        try:
            claimed_actions = target_db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(action_ids),
                TopQueryActionPlan.status == "verifying",
                TopQueryActionPlan.execution_lease_expires_at == claimed_until,
            ).order_by(TopQueryActionPlan.id.asc()).all()
            snapshots = [
                self._preview_json_object(action.after_snapshot)
                for action in claimed_actions
            ]
            local_audit_recorded = bool(snapshots) and all(
                snapshot.get("localAuditRecorded") is True
                or (
                    isinstance(snapshot.get("configVerification"), dict)
                    and snapshot["configVerification"].get("status") == "passed"
                )
                for snapshot in snapshots
            )
            leader_response = self._preview_json_object(
                claimed_actions[0].response_payload if claimed_actions else None
            )
            write_version = str(leader_response.get("writeVersion") or "").strip()
            refreshed_config = self._query_demote_config_verification(
                brand,
                query_text,
                list(metadata["desiredIds"]),
                expected_version=write_version,
            )
            if refreshed_config.get("status") == "passed" and not local_audit_recorded:
                payloads = [
                    self._load_action_payload(action)
                    for action in claimed_actions
                ]
                action_deltas = [
                    self._query_demote_action_deltas(payload)
                    for payload in payloads
                ]
                try:
                    self._record_query_demote_group_local_audit(
                        claimed_actions,
                        payloads,
                        action_deltas,
                        brand=brand,
                        query=query_text,
                        before_ids=list(metadata["beforeIds"]),
                        desired_ids=list(metadata["desiredIds"]),
                    )
                except Exception:
                    target_db.rollback()
                    target_db.query(TopQueryActionPlan).filter(
                        TopQueryActionPlan.id.in_(action_ids),
                        TopQueryActionPlan.status == "verifying",
                        TopQueryActionPlan.execution_lease_expires_at == claimed_until,
                    ).update({
                        TopQueryActionPlan.execution_lease_expires_at: None,
                    }, synchronize_session=False)
                    target_db.commit()
                    raise
                local_audit_recorded = True
            for action, snapshot in zip(claimed_actions, snapshots):
                snapshot["configVerification"] = refreshed_config
                snapshot["localAuditRecorded"] = local_audit_recorded
                response = self._preview_json_object(action.response_payload)
                response["configVerification"] = refreshed_config
                action.after_snapshot = json.dumps(snapshot, ensure_ascii=False)
                action.response_payload = _bounded_action_response_payload(response)
            target_db.commit()
            target_db.expire_all()
            claimed_actions = target_db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(action_ids),
                TopQueryActionPlan.status == "verifying",
                TopQueryActionPlan.execution_lease_expires_at == claimed_until,
            ).order_by(TopQueryActionPlan.id.asc()).all()
            verification_targets, target_error = query_demotion_verification_targets(claimed_actions)
        finally:
            target_db.close()

        search_result: Dict[str, Any]
        observe_buckets = getattr(self.operational_service, "observe_query_product_buckets", None)
        try:
            if target_error:
                search_result = {
                    "success": False,
                    "total_count": None,
                    "checked_product_count": 0,
                    "result_complete": False,
                    "observations": {},
                    "missing_product_ids": [],
                    "error_reason": target_error,
                }
            elif not verification_targets:
                search_result = {
                    "success": True,
                    "total_count": None,
                    "checked_product_count": 0,
                    "result_complete": False,
                    "observations": {},
                    "missing_product_ids": [],
                    "error_reason": None,
                }
            elif not callable(observe_buckets):
                raise ActionExecutionError("observe_query_product_buckets is not supported")
            else:
                raw_search_result = observe_buckets(
                    brand,
                    query_text,
                    verification_targets,
                    page_size=500,
                )
                if not isinstance(raw_search_result, dict):
                    raise ActionExecutionError("invalid search verification response")
                search_result = raw_search_result
        except Exception as exc:
            search_result = {
                "success": False,
                "total_count": None,
                "checked_product_count": 0,
                "result_complete": False,
                "observations": {},
                "missing_product_ids": [],
                "error_reason": sanitize_action_error(exc),
            }

        verification = {
            "after": {
                "query_succeeded": search_result.get("success") is True,
                "result_complete": search_result.get("result_complete"),
                "result_count": search_result.get("total_count"),
                "failure_reason": search_result.get("error_reason"),
                "bucket_observations": search_result.get("observations"),
                "missing_product_ids": search_result.get("missing_product_ids"),
                "checked_product_count": search_result.get("checked_product_count"),
                "products": [],
            },
        }
        try:
            _reconcile_query_demotion_verifying_actions(
                task_id,
                verification,
                action_ids=action_ids,
                expected_lease_expires_at=claimed_until,
                expected_group_key=seed_group_key,
            )
        finally:
            release_db = SessionLocal()
            try:
                releasable_actions = release_db.query(TopQueryActionPlan).filter(
                    TopQueryActionPlan.id.in_(action_ids),
                    TopQueryActionPlan.status.in_(["verifying", "rollback_pending"]),
                    TopQueryActionPlan.execution_lease_expires_at == claimed_until,
                ).all()
                if len(releasable_actions) == len(action_ids) and all(
                    query_demotion_verification_group_key(
                        action,
                        self._preview_json_object(action.after_snapshot),
                    ) == seed_group_key
                    for action in releasable_actions
                ):
                    release_db.query(TopQueryActionPlan).filter(
                        TopQueryActionPlan.id.in_(action_ids),
                        TopQueryActionPlan.status.in_(["verifying", "rollback_pending"]),
                        TopQueryActionPlan.execution_lease_expires_at == claimed_until,
                    ).update({
                        TopQueryActionPlan.execution_lease_expires_at: None,
                    }, synchronize_session=False)
                release_db.commit()
            finally:
                release_db.close()

        result_db = SessionLocal()
        try:
            refreshed = result_db.get(TopQueryActionPlan, action_id)
            if not refreshed:
                raise ActionValidationError(f"top query action not found: {action_id}")
            return {
                "action_id": action_id,
                "action_ids": action_ids,
                "status": str(refreshed.status or "verifying"),
            }
        finally:
            result_db.close()

    def _reconcile_query_demote_group(
        self,
        action_id: int,
        group_metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        action_ids = list(group_metadata["actionIds"])
        group_external_request_id = str(group_metadata["groupExternalRequestId"])
        leader_action_id = int(group_metadata["leaderActionId"])
        db = SessionLocal()
        try:
            actions = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(action_ids),
            ).order_by(TopQueryActionPlan.id.asc()).all()
            seed_action = next((action for action in actions if int(action.id) == action_id), None)
            if not seed_action or len(actions) != len(action_ids):
                raise ActionValidationError("query demote reconciliation group is incomplete")
            seed_query_key = normalize_governance_query_key(seed_action.brand, seed_action.query_text)
            if any(
                int(action.task_id) != int(seed_action.task_id)
                or str(action.action_type or "") != "query_demote"
                or not self._is_authoritative_query_demote_action(action)
                or normalize_governance_query_key(action.brand, action.query_text) != seed_query_key
                or self._query_demote_group_metadata(action) != group_metadata
                for action in actions
            ):
                raise ActionValidationError("query demote reconciliation group identity mismatch")
            leader_action = next(
                (action for action in actions if int(action.id) == leader_action_id),
                None,
            )
            if not leader_action:
                raise ActionValidationError("query demote reconciliation leader is missing")

            reconciliation_started_at = datetime.datetime.now()
            claimed_until = _verification_lease_token(
                reconciliation_started_at + datetime.timedelta(
                    seconds=self.ACTION_EXECUTION_LEASE_SECONDS,
                )
            )
            claimed = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(action_ids),
                or_(
                    TopQueryActionPlan.status == "execution_unknown",
                    (
                        (TopQueryActionPlan.status == "reconciling")
                        & (
                            TopQueryActionPlan.execution_lease_expires_at.is_(None)
                            | (
                                TopQueryActionPlan.execution_lease_expires_at
                                <= reconciliation_started_at
                            )
                        )
                    ),
                ),
            ).update({
                TopQueryActionPlan.status: "reconciling",
                TopQueryActionPlan.execution_lease_expires_at: claimed_until,
            }, synchronize_session=False)
            if claimed != len(action_ids):
                db.rollback()
                current = db.get(TopQueryActionPlan, action_id)
                raise ActionStateConflictError(
                    f"action cannot be reconciled from status: {current.status if current else 'missing'}"
                )
            db.commit()

            concurrency_evidence = self._query_demote_reconciliation_concurrency_evidence(
                leader_action,
            )
            brand = str(group_metadata.get("brand") or "").strip()
            normalized_query = str(group_metadata.get("normalizedQuery") or "").strip()
            desired_ids = group_metadata.get("desiredIds")
            pre_write_version = str(group_metadata.get("configVersion") or "").strip()
            snapshot_group_request_id = str(group_metadata.get("groupExternalRequestId") or "").strip()
            checked_at = datetime.datetime.now()
            snapshot_error = None
            if (
                not brand
                or not normalized_query
                or not isinstance(desired_ids, list)
                or not pre_write_version
                or snapshot_group_request_id != group_external_request_id
                or self._query_demote_group_metadata(leader_action) != group_metadata
            ):
                snapshot_error = "query_demote_reconciliation_snapshot_incomplete"
            normalized_desired_ids = sorted({
                str(item).strip()
                for item in desired_ids or []
                if str(item).strip()
            })
            readback = None
            readback_error = snapshot_error
            external_status = "unavailable"
            external_probe_error = None
            probe = getattr(self.operational_service, "get_top_query_action_status", None)
            try:
                if readback_error is None and callable(probe):
                    probe_payload = self._load_action_payload(leader_action)
                    probe_payload.setdefault("brand", brand)
                    probe_payload.setdefault("query", normalized_query)
                    external_result = probe(
                        action_type="query_demote",
                        external_request_id=group_external_request_id,
                        payload=probe_payload,
                    )
                    external_status = str(
                        external_result.get("status")
                        if isinstance(external_result, dict)
                        else "unknown"
                    ).strip().lower() or "unknown"
            except Exception as exc:
                external_status = "unknown"
                external_probe_error = sanitize_action_error(exc)
            try:
                if readback_error is None:
                    readback = self.operational_service.get_ops_low_weight_config(
                        brand,
                        normalized_query,
                    )
            except Exception as exc:
                readback_error = sanitize_action_error(exc)

            actual_ids: list[str] = []
            actual_version = None
            if readback_error is None:
                config_error = self._query_demote_authoritative_config_error(readback)
                if config_error:
                    readback_error = config_error
                else:
                    actual_ids = sorted({
                        str(item).strip()
                        for item in readback["product_ids"]
                        if str(item).strip()
                    })
                    actual_version = str(readback["version"]).strip()

            expected_set = set(normalized_desired_ids)
            actual_set = set(actual_ids)
            matched = readback_error is None and actual_set == expected_set
            mismatch = readback_error is None and not matched
            error_reason = (
                "config_ids_mismatch"
                if mismatch
                else sanitize_action_error(readback_error) if readback_error else None
            )
            evidence_status = "matched" if matched else "mismatch" if mismatch else "inconclusive"
            verification = {
                "status": "passed" if matched else "failed" if mismatch else "inconclusive",
                "expectedIds": normalized_desired_ids,
                "actualIds": actual_ids,
                "missingIds": sorted(expected_set - actual_set) if mismatch else [],
                "unexpectedIds": sorted(actual_set - expected_set) if mismatch else [],
                "expectedVersion": actual_version,
                "actualVersion": actual_version,
                "checkedAt": checked_at.isoformat(),
                "errorReason": error_reason,
            }
            if matched:
                payloads = [self._load_action_payload(action) for action in actions]
                action_deltas = [
                    self._query_demote_action_deltas(payload)
                    for payload in payloads
                ]
                try:
                    self._record_query_demote_group_local_audit(
                        actions,
                        payloads,
                        action_deltas,
                        brand=brand,
                        query=normalized_query,
                        before_ids=list(group_metadata["beforeIds"]),
                        desired_ids=normalized_desired_ids,
                    )
                except Exception as exc:
                    audit_error = sanitize_action_error(exc)
                    retry_values = {
                        int(action.id): {
                            TopQueryActionPlan.status: "execution_unknown",
                            TopQueryActionPlan.execution_lease_expires_at: None,
                            TopQueryActionPlan.error_message: audit_error,
                        }
                        for action in actions
                    }
                    if not self._query_demote_group_fenced_update(
                        db,
                        action_ids,
                        expected_status="reconciling",
                        expected_fence=claimed_until,
                        values_by_id=retry_values,
                    ):
                        raise ActionStateConflictError(
                            "query demote reconciliation group lease fence changed"
                        ) from exc
                    raise ActionExecutionError(
                        f"query_demote local audit failed after reconciliation: {audit_error}",
                        {"write_succeeded": True, "execution_unknown": True},
                    ) from exc
            reconciliation_snapshot = {
                "status": evidence_status,
                "checkedAt": checked_at.isoformat(),
                "groupExternalRequestId": group_external_request_id,
                "errorReason": error_reason,
                "externalStatus": external_status,
                "externalProbeError": external_probe_error,
            }
            after_snapshot = {
                "configVerification": verification,
                "preWriteConfigVersion": pre_write_version or None,
                **group_metadata,
                "configReconciliation": reconciliation_snapshot,
                **concurrency_evidence,
            }
            if matched:
                after_snapshot["searchEffect"] = {"status": "pending"}
            verification_now = _verification_now()
            final_values: Dict[int, Dict[Any, Any]] = {}
            next_status = (
                "verifying"
                if matched
                else "verification_failed"
                if mismatch
                else "execution_unknown"
            )
            for action in actions:
                action_snapshot = dict(after_snapshot)
                if matched:
                    existing_snapshot = self._preview_json_object(action.after_snapshot)
                    for key in (
                        "verificationStartedAt",
                        "verificationDeadlineAt",
                        "verificationAttempts",
                    ):
                        if key in existing_snapshot:
                            action_snapshot[key] = existing_snapshot[key]
                    action_snapshot = initialize_query_demotion_verification_snapshot(
                        action_snapshot,
                        now=verification_now,
                    )
                response = self._preview_json_object(action.response_payload)
                response.update(group_metadata)
                response["configVerification"] = verification
                response["config_reconciliation"] = reconciliation_snapshot
                response.update(concurrency_evidence)
                if matched:
                    response["searchEffect"] = {"status": "pending"}
                final_values[int(action.id)] = {
                    TopQueryActionPlan.status: next_status,
                    TopQueryActionPlan.after_snapshot: json.dumps(action_snapshot, ensure_ascii=False),
                    TopQueryActionPlan.response_payload: _bounded_action_response_payload(response),
                    TopQueryActionPlan.execution_lease_expires_at: None,
                    TopQueryActionPlan.execution_finished_at: (
                        checked_at if matched or mismatch else None
                    ),
                    TopQueryActionPlan.execution_started_at: (
                        leader_action.execution_started_at if matched or mismatch else None
                    ),
                    TopQueryActionPlan.error_message: error_reason if not matched else None,
                }
            if not self._query_demote_group_fenced_update(
                db,
                action_ids,
                expected_status="reconciling",
                expected_fence=claimed_until,
                values_by_id=final_values,
            ):
                raise ActionStateConflictError(
                    "query demote reconciliation group lease fence changed"
                )
            return {
                "action_id": action_id,
                "action_ids": action_ids,
                "status": next_status,
                "external_request_id": group_external_request_id,
                "configVerification": verification,
            }
        finally:
            db.close()

    @staticmethod
    def _set_query_demote_group_reconciliation_status(
        db: Any,
        action_ids: list[int],
        *,
        status: str,
    ) -> None:
        db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.id.in_(action_ids),
        ).update({
            TopQueryActionPlan.status: status,
            TopQueryActionPlan.execution_lease_expires_at: None,
        }, synchronize_session=False)
        db.commit()

    @staticmethod
    def _is_ambiguous_external_failure(exc: Exception) -> bool:
        if isinstance(exc, ActionExecutionError) and exc.details.get("execution_unknown") is True:
            return True
        current: Optional[BaseException] = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, (TimeoutError, ConnectionError)):
                return True
            class_name = current.__class__.__name__.lower()
            if "timeout" in class_name or "connectionerror" in class_name:
                return True
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _set_verification_timestamp(result: Dict[str, Any], verified_at: datetime.datetime) -> None:
        verification = result.get("world_knowledge_verification")
        if isinstance(verification, dict) and not verification.get("verifiedAt"):
            verification["verifiedAt"] = verified_at.isoformat()

    @staticmethod
    def _commit_action_failure_after_rollback(
        db: Any,
        action_id: int,
        result: Dict[str, Any],
        exc: Exception,
        status: str = "failed",
    ) -> None:
        action = db.get(TopQueryActionPlan, action_id)
        sanitized_error = sanitize_action_error(exc)
        failure_result = {**result, "error": sanitized_error}
        action.status = status # type: ignore
        action.error_message = sanitized_error # type: ignore
        action.execution_finished_at = datetime.datetime.now() # type: ignore
        action.execution_lease_expires_at = None # type: ignore
        action.response_payload = _bounded_action_response_payload(failure_result) # type: ignore
        db.commit()

    def _retry_terminal_action_persistence(
        self,
        action_id: int,
        *,
        expected_execution_fence: Optional[datetime.datetime],
        status: str,
        execution_finished_at: datetime.datetime,
        response_payload: str,
        action_type: str = "",
        result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if expected_execution_fence is None:
            return False
        result = result or {}
        values = {
            TopQueryActionPlan.status: status,
            TopQueryActionPlan.execution_finished_at: execution_finished_at,
            TopQueryActionPlan.execution_lease_expires_at: None,
            TopQueryActionPlan.response_payload: response_payload,
            TopQueryActionPlan.error_message: None,
            TopQueryActionPlan.updated_at: datetime.datetime.now(),
        }
        if action_type == "tag_bucket_config":
            verification = result.get("world_knowledge_verification")
            values[TopQueryActionPlan.before_snapshot] = _bounded_tag_bucket_payload(
                result.get("before_snapshot") or {},
            )
            values[TopQueryActionPlan.after_snapshot] = _bounded_tag_bucket_payload({
                **(result.get("after_snapshot") or {}),
                "world_knowledge_verification": verification,
            })
        db = SessionLocal()
        try:
            updated = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == action_id,
                TopQueryActionPlan.status == "executing",
                TopQueryActionPlan.execution_lease_expires_at == expected_execution_fence,
            ).update(values, synchronize_session=False)
            if updated == 1:
                if action_type == "tag_bucket_config":
                    action = db.get(TopQueryActionPlan, action_id)
                    if not action:
                        db.rollback()
                        return False
                    self._persist_tag_bucket_verification(action, result, db=db)
                db.commit()
                return True
            db.rollback()
            action = db.get(TopQueryActionPlan, action_id)
            return bool(
                action
                and str(action.status or "") == status
                and action.execution_lease_expires_at is None
                and action.execution_finished_at == execution_finished_at
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _begin_manual_query_demote_scope_guard(
        db: Any,
        *,
        task_id: int,
    ) -> list[TopQueryActionPlan]:
        dialect_name = str(db.get_bind().dialect.name or "").lower()
        connection = db.connection()
        if dialect_name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        query = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == int(task_id),
        ).order_by(TopQueryActionPlan.id.asc())
        if dialect_name != "sqlite":
            query = query.with_for_update()
        return query.all()

    def _manual_query_demote_scope_ids(
        self,
        actions: list[TopQueryActionPlan],
        expected_action_ids: list[int],
        *,
        post_claim: bool,
    ) -> list[int]:
        expected_ids = set(expected_action_ids)
        scope_ids = []
        for action in actions:
            action_id = int(action.id)
            status = str(action.status or "").lower()
            if post_claim and action_id in expected_ids:
                if status == "executing":
                    scope_ids.append(action_id)
                continue
            if (
                status in {"waiting_human_approval", "pending_confirmation", "approved"}
                and str(action.execution_mode or "") == "human_approval"
                and str(action.action_type or "") == "query_demote"
                and self._is_authoritative_query_demote_action(action)
                and self._is_displayable_governance_action(action)
            ):
                scope_ids.append(action_id)
        return sorted(scope_ids)

    def _is_authoritative_query_demote_action(self, action: TopQueryActionPlan) -> bool:
        return (
            str(action.action_type or "") == "query_demote"
            and uses_authoritative_query_demotion(self._load_action_payload(action))
        )

    def _authoritative_query_demote_group_identity(
        self,
        db: Any,
        actions: list[TopQueryActionPlan],
    ) -> tuple[int, str, str]:
        task_ids = {int(action.task_id) for action in actions}
        if len(task_ids) != 1:
            raise ActionValidationError("query_demote group identity requires one task")
        task_id = next(iter(task_ids))
        task = db.get(TopQueryTask, task_id)
        if not task:
            raise ActionValidationError("query_demote group identity task is missing")
        task_brand = str(task.brand or "").strip()
        task_query = str(task.query_text or "").strip()
        brand_key = normalize_query_key(task_brand)
        query_key = normalize_governance_query_key(task_brand, task_query)
        normalized_query = transform_governance_payload_query(task_brand, task_query)
        if not brand_key or not query_key or not normalized_query:
            raise ActionValidationError("query_demote group identity is incomplete")

        for action in actions:
            payload = self._load_action_payload(action)
            action_brand = str(action.brand or "").strip()
            payload_brand = (
                str(payload.get("brand") or "").strip()
                if "brand" in payload
                else action_brand
            )
            action_query = str(action.query_text or "").strip()
            payload_query = (
                str(payload.get("query") or "").strip()
                if "query" in payload
                else action_query
            )
            if (
                normalize_query_key(action_brand) != brand_key
                or normalize_query_key(payload_brand) != brand_key
                or normalize_governance_query_key(task_brand, action_query) != query_key
                or normalize_governance_query_key(task_brand, payload_query) != query_key
            ):
                raise ActionValidationError(
                    "query_demote action/task identity mismatch"
                )
        return task_id, task_brand, normalized_query

    def _execute_query_demote_group(
        self,
        action_ids: list[int],
        *,
        dry_run: bool,
        expected_config_version: Optional[str],
        expected_task_id: Optional[int] = None,
        bind_to_manual_scope: bool = False,
    ) -> list[Dict[str, Any]]:
        normalized_action_ids = sorted({int(action_id) for action_id in action_ids})
        if not normalized_action_ids:
            return []
        if dry_run:
            preview = self._preview_query_demote_execution(normalized_action_ids)
            return [
                {**preview, "action_id": action_id}
                for action_id in normalized_action_ids
            ]

        db = SessionLocal()
        original_state: Dict[int, Dict[str, Any]] = {}
        try:
            guarded_task_actions = None
            if bind_to_manual_scope:
                if expected_task_id is None:
                    raise ActionValidationError("expected_task_id is required for manual scope binding")
                try:
                    guarded_task_actions = self._begin_manual_query_demote_scope_guard(
                        db,
                        task_id=int(expected_task_id),
                    )
                except Exception as exc:
                    db.rollback()
                    raise ActionExecutionError(
                        "manual query_demote scope guard failed: "
                        f"{self._safe_database_action_error(exc)}"
                    ) from exc

            actions = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(normalized_action_ids),
            ).order_by(TopQueryActionPlan.id.asc()).all()
            if len(actions) != len(normalized_action_ids):
                if bind_to_manual_scope:
                    raise ActionStateConflictError("query demote action scope changed; refresh preview")
                raise ActionValidationError("top query action group contains missing actions")
            if any(not self._is_authoritative_query_demote_action(action) for action in actions):
                raise ActionValidationError(
                    "query demote authoritative group requires marked query_demote actions"
                )
            if bind_to_manual_scope:
                for action in actions:
                    payload = self._load_action_payload(action)
                    check_key = str(payload.get("check_key") or action.check_key or "")
                    if (
                        int(action.task_id) != int(expected_task_id)
                        or str(action.execution_mode or "") != "human_approval"
                        or check_key != "inventory"
                        or not self._is_displayable_governance_action(action)
                    ):
                        raise ActionValidationError(
                            "expected_action_ids must contain only displayable human authoritative "
                            "query_demote actions for the requested task"
                        )
                current_action_ids = self._manual_query_demote_scope_ids(
                    guarded_task_actions or [],
                    normalized_action_ids,
                    post_claim=False,
                )
                if current_action_ids != normalized_action_ids:
                    raise ActionStateConflictError("query demote action scope changed; refresh preview")

            payloads = [self._load_action_payload(action) for action in actions]
            task_id, brand, normalized_query = (
                self._authoritative_query_demote_group_identity(db, actions)
            )
            action_deltas = [self._query_demote_action_deltas(payload) for payload in payloads]
            if any(not add_ids and not remove_ids for add_ids, remove_ids in action_deltas):
                raise ActionValidationError("query_demote requires product_id/product_ids/skus")
            normalized_expected_version = str(expected_config_version or "").strip()
            if (
                any(str(action.execution_mode or "") == "human_approval" for action in actions)
                and not normalized_expected_version
            ):
                raise ActionValidationError(
                    "expected_config_version is required for human query_demote execution"
                )
            assert_queries_governance_allowed_in_session(
                db,
                brand,
                [
                    str(action.query_text or "")
                    for action in actions
                ] + [
                    str(payload.get("query") or "")
                    for payload in payloads
                ] + [normalized_query],
            )
            if (
                get_env() == "prod"
                and not self._query_demote_safe_write_available()
            ):
                raise ActionExecutionError(
                    "query_demote server atomic compare unavailable"
                )

            add_ids = {
                product_id
                for action_add_ids, _ in action_deltas
                for product_id in action_add_ids
            }
            remove_ids = {
                product_id
                for _, action_remove_ids in action_deltas
                for product_id in action_remove_ids
            }
            original_state = {
                int(action.id): {
                    "status": str(action.status or ""),
                    "before_snapshot": action.before_snapshot,
                    "after_snapshot": action.after_snapshot,
                    "response_payload": action.response_payload,
                    "error_message": action.error_message,
                    "external_request_id": action.external_request_id,
                    "execution_started_at": action.execution_started_at,
                    "execution_finished_at": action.execution_finished_at,
                    "execution_lease_expires_at": action.execution_lease_expires_at,
                }
                for action in actions
            }
            started_at = datetime.datetime.now()
            claimed_until = _verification_lease_token(
                started_at + datetime.timedelta(
                    seconds=self.ACTION_EXECUTION_LEASE_SECONDS,
                )
            )
            claimed = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(normalized_action_ids),
                TopQueryActionPlan.status.in_([
                    "approved",
                    "waiting_human_approval",
                    "pending_confirmation",
                ]),
            ).update({
                TopQueryActionPlan.status: "executing",
                TopQueryActionPlan.execution_started_at: started_at,
                TopQueryActionPlan.execution_finished_at: None,
                TopQueryActionPlan.execution_lease_expires_at: claimed_until,
            }, synchronize_session=False)
            if claimed != len(normalized_action_ids):
                db.rollback()
                expired_action_ids = [
                    int(action.id)
                    for action in actions
                    if str(action.status or "") == "executing"
                    and action.execution_lease_expires_at
                    and action.execution_lease_expires_at <= started_at
                ]
                if expired_action_ids:
                    expired = db.query(TopQueryActionPlan).filter(
                        TopQueryActionPlan.id.in_(expired_action_ids),
                        TopQueryActionPlan.status == "executing",
                        TopQueryActionPlan.execution_lease_expires_at <= started_at,
                    ).update({
                        TopQueryActionPlan.status: "execution_unknown",
                        TopQueryActionPlan.execution_lease_expires_at: None,
                    }, synchronize_session=False)
                    if expired:
                        db.commit()
                        raise ActionStateConflictError(
                            "action execution lease expired; reconcile external status before retry"
                        )
                raise ActionStateConflictError("query demote action group claim conflict")

            db.expire_all()
            actions = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(normalized_action_ids),
            ).order_by(TopQueryActionPlan.id.asc()).all()
            assert_queries_governance_allowed_in_session(
                db,
                brand,
                [
                    str(action.query_text or normalized_query)
                    for action in actions
                ] + [normalized_query],
            )

            leader_action_id = normalized_action_ids[0]
            group_external_request_id = self._stable_query_demote_group_request_id(
                task_id=task_id,
                brand=brand,
                normalized_query=normalized_query,
                action_ids=normalized_action_ids,
            )
            if len(actions) == 1:
                existing_request_id = str(
                    original_state[leader_action_id]["external_request_id"] or ""
                ).strip()
                if existing_request_id:
                    group_external_request_id = existing_request_id
            for action in actions:
                action.external_request_id = ( # type: ignore
                    group_external_request_id
                    if int(action.id) == leader_action_id
                    else None
                )

            if bind_to_manual_scope:
                db.flush()
                db.expire_all()
                postclaim_query = db.query(TopQueryActionPlan).filter(
                    TopQueryActionPlan.task_id == int(expected_task_id),
                ).order_by(TopQueryActionPlan.id.asc())
                if str(db.get_bind().dialect.name or "").lower() != "sqlite":
                    postclaim_query = postclaim_query.with_for_update()
                postclaim_actions = postclaim_query.all()
                postclaim_scope_ids = self._manual_query_demote_scope_ids(
                    postclaim_actions,
                    normalized_action_ids,
                    post_claim=True,
                )
                if postclaim_scope_ids != normalized_action_ids:
                    db.rollback()
                    raise ActionStateConflictError("query demote action scope changed; refresh preview")
            db.commit()

            try:
                config = self.operational_service.get_ops_low_weight_config(
                    brand,
                    normalized_query,
                )
            except Exception as exc:
                self._restore_query_demote_group(
                    db,
                    original_state,
                    expected_status="executing",
                    expected_fence=claimed_until,
                )
                raise ActionExecutionError(
                    f"query_demote authoritative config read failed: {sanitize_action_error(exc)}"
                ) from exc
            config_error = self._query_demote_authoritative_config_error(config)
            if config_error:
                self._restore_query_demote_group(
                    db,
                    original_state,
                    expected_status="executing",
                    expected_fence=claimed_until,
                )
                raise ActionExecutionError(
                    f"query_demote authoritative config unavailable: {config_error}"
                )

            config_version = str(config["version"]).strip()
            if normalized_expected_version and config_version != normalized_expected_version:
                self._restore_query_demote_group(
                    db,
                    original_state,
                    expected_status="executing",
                    expected_fence=claimed_until,
                )
                raise ActionStateConflictError("config version changed; refresh preview")

            before_ids = sorted({
                str(item).strip()
                for item in config["product_ids"]
                if str(item).strip()
            })
            before_set = set(before_ids)
            desired_ids = sorted((before_set | add_ids) - remove_ids)
            desired_set = set(desired_ids)
            target_ids = sorted(add_ids | remove_ids)
            concurrency_evidence = self._query_demote_concurrency_control_evidence()
            before_snapshot = {
                "complete": True,
                "brand": brand,
                "normalizedQuery": normalized_query,
                "beforeIds": before_ids,
                "desiredIds": desired_ids,
                "configVersion": config_version,
                "rollbackAvailable": True,
                "actionIds": normalized_action_ids,
                "targetIds": target_ids,
                "addedIds": sorted(desired_set - before_set),
                "retainedIds": sorted(desired_set & before_set),
                "removedIds": sorted(before_set - desired_set),
                "groupExternalRequestId": group_external_request_id,
                "leaderActionId": leader_action_id,
                **concurrency_evidence,
            }
            serialized_before_snapshot = json.dumps(before_snapshot, ensure_ascii=False)
            try:
                snapshot_values = {
                    action_id: {
                        TopQueryActionPlan.before_snapshot: serialized_before_snapshot,
                    }
                    for action_id in normalized_action_ids
                }
                if not self._query_demote_group_fenced_update(
                    db,
                    normalized_action_ids,
                    expected_status="executing",
                    expected_fence=claimed_until,
                    values_by_id=snapshot_values,
                ):
                    raise ActionStateConflictError(
                        "query demote action group lease fence changed before write"
                    )
            except Exception as exc:
                db.rollback()
                if isinstance(exc, ActionStateConflictError):
                    raise
                restore_error = None
                try:
                    self._restore_query_demote_group_independently(
                        original_state,
                        expected_status="executing",
                        expected_fence=claimed_until,
                    )
                except Exception as restore_exc:
                    restore_error = self._safe_database_action_error(restore_exc)
                details = {
                    "snapshot_error": self._safe_database_action_error(exc),
                    "restore_attempted": True,
                    "execution_state_may_be_stale": restore_error is not None,
                }
                if restore_error is not None:
                    details["restore_error"] = restore_error
                raise ActionExecutionError(
                    "query_demote snapshot persistence failed",
                    details,
                ) from exc

            try:
                write_result = self.operational_service.set_ops_low_weight_config(
                    brand,
                    normalized_query,
                    desired_ids,
                    expected_version=config_version,
                    external_request_id=group_external_request_id,
                )
            except Exception as exc:
                failure = ActionExecutionError(
                    f"query_demote conditional write failed: {sanitize_action_error(exc)}",
                    {"execution_unknown": self._is_ambiguous_external_failure(exc)},
                )
                self._fail_query_demote_group(
                    db,
                    normalized_action_ids,
                    failure,
                    expected_status="executing",
                    expected_fence=claimed_until,
                )
                raise failure from exc
            if not isinstance(write_result, dict):
                failure = ActionExecutionError(
                    "query_demote conditional write failed: invalid response"
                )
                self._fail_query_demote_group(
                    db,
                    normalized_action_ids,
                    failure,
                    expected_status="executing",
                    expected_fence=claimed_until,
                )
                raise failure
            if write_result.get("conflict") is True:
                self._restore_query_demote_group(
                    db,
                    original_state,
                    expected_status="executing",
                    expected_fence=claimed_until,
                )
                raise ActionStateConflictError("config version changed; refresh preview")
            write_version = write_result.get("version")
            strict_write_success = (
                write_result.get("success") is True
                and write_result.get("conflict") is False
                and isinstance(write_version, str)
                and bool(write_version.strip())
            )
            if not strict_write_success:
                error_reason = sanitize_action_error(
                    write_result.get("error_reason") or "invalid_conditional_write_response"
                )
                failure = ActionExecutionError(
                    f"query_demote conditional write failed: {error_reason}"
                )
                failure_status = (
                    "execution_unknown"
                    if str(write_result.get("error_reason") or "")
                    == "conditional_write_request_failed"
                    else "failed"
                )
                self._fail_query_demote_group(
                    db,
                    normalized_action_ids,
                    failure,
                    status=failure_status,
                    expected_status="executing",
                    expected_fence=claimed_until,
                )
                raise failure

            try:
                db.rollback()
                owned = db.query(TopQueryActionPlan).filter(
                    TopQueryActionPlan.id.in_(normalized_action_ids),
                    TopQueryActionPlan.status == "executing",
                    TopQueryActionPlan.execution_lease_expires_at == claimed_until,
                ).count()
                if owned != len(normalized_action_ids):
                    raise ActionStateConflictError(
                        "query demote action group lease fence changed after write"
                    )
            except Exception as exc:
                failure = ActionExecutionError(
                    f"query_demote ownership check failed after external write: {sanitize_action_error(exc)}",
                    {"write_succeeded": True, "execution_unknown": True},
                )
                self._fail_query_demote_group(
                    db,
                    normalized_action_ids,
                    failure,
                    status="execution_unknown",
                    expected_status="executing",
                    expected_fence=claimed_until,
                )
                raise failure from exc

            expected_written_version = str(write_version).strip()
            verification = self._query_demote_config_verification(
                brand,
                normalized_query,
                desired_ids,
                expected_version=expected_written_version,
            )
            if verification.get("status") == "passed":
                try:
                    audit_task = db.get(TopQueryTask, task_id)
                    audit_query = str(
                        audit_task.query_text if audit_task else normalized_query
                    ).strip() or normalized_query
                    self._record_query_demote_group_local_audit(
                        actions,
                        payloads,
                        action_deltas,
                        brand=brand,
                        query=audit_query,
                        before_ids=before_ids,
                        desired_ids=desired_ids,
                    )
                except Exception as exc:
                    failure = ActionExecutionError(
                        f"query_demote local audit failed after config verification: {sanitize_action_error(exc)}",
                        {"write_succeeded": True, "execution_unknown": True},
                    )
                    self._fail_query_demote_group(
                        db,
                        normalized_action_ids,
                        failure,
                        status="execution_unknown",
                        expected_status="executing",
                        expected_fence=claimed_until,
                    )
                    raise failure from exc
            finished_at = datetime.datetime.now()
            after_snapshot = self._new_query_demote_verification_snapshot(
                verification,
                group_external_request_id=group_external_request_id,
                leader_action_id=leader_action_id,
                action_ids=normalized_action_ids,
            )
            after_snapshot.update({
                key: before_snapshot[key]
                for key in (
                    "complete",
                    "brand",
                    "normalizedQuery",
                    "beforeIds",
                    "desiredIds",
                    "configVersion",
                    "rollbackAvailable",
                    "concurrencyControl",
                    "serverAtomicCompare",
                )
            })
            after_snapshot["localAuditRecorded"] = verification.get("status") == "passed"
            serialized_after_snapshot = json.dumps(after_snapshot, ensure_ascii=False)
            immediate_status = "verifying"
            results = []
            db.rollback()
            actions = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(normalized_action_ids),
            ).order_by(TopQueryActionPlan.id.asc()).all()
            final_values: Dict[int, Dict[Any, Any]] = {}
            for action, (action_add_ids, action_remove_ids) in zip(actions, action_deltas):
                approval_metadata = _action_approval_metadata(
                    original_state[int(action.id)]["response_payload"]
                )
                lifecycle = {
                    "proposedAt": action.created_at.isoformat() if action.created_at else None,
                    "approvedAt": approval_metadata.get("approved_at")
                    or approval_metadata.get("approvedAt"),
                    "operator": approval_metadata.get("operator"),
                    "executionStartedAt": started_at.isoformat(),
                    "executionFinishedAt": finished_at.isoformat(),
                    "durationSeconds": round((finished_at - started_at).total_seconds(), 6),
                }
                result = {
                    **approval_metadata,
                    "action_id": int(action.id),
                    "tool_key": action.tool_key,
                    "dry_run": False,
                    "real_action": "query_demote",
                    "query": normalized_query,
                    "product_ids": sorted(set(action_add_ids) | set(action_remove_ids)),
                    "desired_ids": desired_ids,
                    "external_request_id": str(action.external_request_id or ""),
                    "groupExternalRequestId": group_external_request_id,
                    "leaderActionId": leader_action_id,
                    "actionIds": normalized_action_ids,
                    "complete": True,
                    "brand": brand,
                    "normalizedQuery": normalized_query,
                    "beforeIds": before_ids,
                    "desiredIds": desired_ids,
                    "configVersion": config_version,
                    "rollbackAvailable": True,
                    "writeVersion": expected_written_version,
                    "configVerification": verification,
                    "searchEffect": {"status": "pending"},
                    "executed_at": finished_at.isoformat(),
                    "lifecycle": lifecycle,
                    **concurrency_evidence,
                }
                final_values[int(action.id)] = {
                    TopQueryActionPlan.status: immediate_status,
                    TopQueryActionPlan.execution_finished_at: finished_at,
                    TopQueryActionPlan.execution_lease_expires_at: None,
                    TopQueryActionPlan.error_message: None,
                    TopQueryActionPlan.after_snapshot: serialized_after_snapshot,
                    TopQueryActionPlan.response_payload: _bounded_action_response_payload(result),
                }
                results.append(result)
            try:
                if not self._query_demote_group_fenced_update(
                    db,
                    normalized_action_ids,
                    expected_status="executing",
                    expected_fence=claimed_until,
                    values_by_id=final_values,
                ):
                    raise ActionStateConflictError(
                        "query demote action group lease fence changed before finalization"
                    )
            except Exception as exc:
                db.rollback()
                if isinstance(exc, ActionStateConflictError):
                    raise
                failure = ActionExecutionError(
                    f"query_demote local audit failed after external write: {sanitize_action_error(exc)}",
                    {"write_succeeded": True, "execution_unknown": True},
                )
                self._fail_query_demote_group(
                    db,
                    normalized_action_ids,
                    failure,
                    status="execution_unknown",
                    expected_status="executing",
                    expected_fence=claimed_until,
                )
                raise failure from exc
            return results
        finally:
            db.close()

    @staticmethod
    def _query_demote_authoritative_config_error(config: Any) -> Optional[str]:
        if not isinstance(config, dict):
            return "invalid_authoritative_config_response"
        if config.get("success") is not True:
            return sanitize_action_error(
                config.get("error_reason") or "authoritative_config_unavailable"
            )
        if not isinstance(config.get("product_ids"), list):
            return "invalid_authoritative_config_response"
        version = config.get("version")
        if not isinstance(version, str) or not version.strip():
            return "invalid_authoritative_config_response"
        return None

    def _query_demote_server_atomic_compare_available(self) -> bool:
        capability = getattr(
            self.operational_service,
            "supports_ops_low_weight_atomic_compare",
            None,
        )
        if not callable(capability):
            return False
        try:
            return capability() is True
        except Exception:
            return False

    def _query_demote_client_preflight_available(self) -> bool:
        capability = getattr(
            self.operational_service,
            "supports_ops_low_weight_client_preflight",
            None,
        )
        if not callable(capability):
            return False
        try:
            return capability() is True
        except Exception:
            return False

    def _query_demote_safe_write_available(self) -> bool:
        return (
            self._query_demote_server_atomic_compare_available()
            or self._query_demote_client_preflight_available()
        )

    def _query_demote_concurrency_control_evidence(self) -> Dict[str, Any]:
        server_atomic_compare = self._query_demote_server_atomic_compare_available()
        return {
            "concurrencyControl": (
                "server_atomic_compare"
                if server_atomic_compare
                else "client_preflight_full_replace"
                if self._query_demote_client_preflight_available()
                else "unavailable"
            ),
            "serverAtomicCompare": server_atomic_compare,
        }

    @staticmethod
    def _query_demote_unknown_concurrency_evidence() -> Dict[str, Any]:
        return {
            "concurrencyControl": "unavailable",
            "serverAtomicCompare": False,
        }

    @staticmethod
    def _query_demote_persisted_concurrency_evidence(
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        mode = payload.get("concurrencyControl")
        server_atomic_compare = payload.get("serverAtomicCompare")
        if mode == "server_atomic_compare" and server_atomic_compare is True:
            return {
                "concurrencyControl": mode,
                "serverAtomicCompare": True,
            }
        if mode == "client_preflight_full_replace" and server_atomic_compare is False:
            return {
                "concurrencyControl": mode,
                "serverAtomicCompare": False,
            }
        return None

    def _query_demote_reconciliation_concurrency_evidence(
        self,
        action: TopQueryActionPlan,
    ) -> Dict[str, Any]:
        for persisted_payload in (
            action.before_snapshot,
            action.response_payload,
        ):
            evidence = self._query_demote_persisted_concurrency_evidence(
                self._preview_json_object(persisted_payload),
            )
            if evidence is not None:
                return evidence
        return self._query_demote_unknown_concurrency_evidence()

    def _query_demote_action_deltas(
        self,
        payload: Dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        operation = str(payload.get("operation") or "add").strip().lower()
        generic_ids = self._payload_product_ids(payload)

        def ids_for(*keys: str) -> list[str]:
            values = []
            for key in keys:
                if key in payload:
                    values.extend(self._payload_product_ids({"product_ids": payload.get(key)}))
            return values

        add_ids = ids_for("add_ids", "addIds", "add_product_ids", "addProductIds")
        remove_ids = ids_for(
            "remove_ids",
            "removeIds",
            "remove_product_ids",
            "removeProductIds",
        )
        if operation == "remove":
            remove_ids.extend(generic_ids)
        else:
            add_ids.extend(generic_ids)
        return sorted(set(add_ids)), sorted(set(remove_ids))

    @staticmethod
    def _stable_query_demote_group_request_id(
        *,
        task_id: int,
        brand: str,
        normalized_query: str,
        action_ids: list[int],
    ) -> str:
        identity = ":".join([
            "top-query-query-demote-group",
            str(task_id),
            str(brand).strip().casefold(),
            str(normalized_query).strip().casefold(),
            ",".join(str(action_id) for action_id in sorted(action_ids)),
        ])
        return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))

    def _preview_query_demote_execution(
        self,
        action_ids: list[int],
        *,
        single_action_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        normalized_action_ids = sorted({int(action_id) for action_id in action_ids})
        if not normalized_action_ids:
            raise ActionValidationError("query demote preview requires action ids")
        db = SessionLocal()
        try:
            actions = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(normalized_action_ids),
            ).order_by(TopQueryActionPlan.id.asc()).all()
            if len(actions) != len(normalized_action_ids):
                raise ActionValidationError("top query action group contains missing actions")
            if any(not self._is_authoritative_query_demote_action(action) for action in actions):
                raise ActionValidationError(
                    "query demote authoritative preview requires marked query_demote actions"
                )
            payloads = [self._load_action_payload(action) for action in actions]
            _, brand, normalized_query = (
                self._authoritative_query_demote_group_identity(db, actions)
            )
            assert_queries_governance_allowed_in_session(
                db,
                brand,
                [
                    str(action.query_text or "")
                    for action in actions
                ] + [
                    str(payload.get("query") or "")
                    for payload in payloads
                ] + [normalized_query],
            )
            deltas = [self._query_demote_action_deltas(payload) for payload in payloads]
            if any(not add_ids and not remove_ids for add_ids, remove_ids in deltas):
                raise ActionValidationError("query_demote requires product_id/product_ids/skus")
            add_ids = {product_id for adds, _ in deltas for product_id in adds}
            remove_ids = {product_id for _, removes in deltas for product_id in removes}
        finally:
            db.close()

        try:
            config = self.operational_service.get_ops_low_weight_config(
                brand,
                normalized_query,
            )
        except Exception as exc:
            raise ActionExecutionError(
                f"query_demote authoritative config read failed: {sanitize_action_error(exc)}"
            ) from exc
        config_error = self._query_demote_authoritative_config_error(config)
        if config_error:
            raise ActionExecutionError(
                f"query_demote authoritative config unavailable: {config_error}"
            )
        before_ids = sorted({
            str(item).strip()
            for item in config["product_ids"]
            if str(item).strip()
        })
        before_set = set(before_ids)
        desired_ids = sorted((before_set | add_ids) - remove_ids)
        desired_set = set(desired_ids)
        result = {
            "dry_run": True,
            "actionIds": normalized_action_ids,
            "brand": brand,
            "query": normalized_query,
            "normalizedQuery": normalized_query,
            "configVersion": str(config["version"]).strip(),
            "beforeIds": before_ids,
            "desiredIds": desired_ids,
            "targetIds": sorted(add_ids | remove_ids),
            "addedIds": sorted(desired_set - before_set),
            "retainedIds": sorted(desired_set & before_set),
            "removedIds": sorted(before_set - desired_set),
            "rollbackAvailable": True,
        }
        if single_action_id is not None:
            result["action_id"] = int(single_action_id)
        return result

    @classmethod
    def _query_demote_group_metadata_result(
        cls,
        action: TopQueryActionPlan,
    ) -> tuple[str, Optional[Dict[str, Any]]]:
        metadata_keys = {
            "groupExternalRequestId",
            "leaderActionId",
            "actionIds",
            "brand",
            "normalizedQuery",
            "beforeIds",
            "desiredIds",
            "configVersion",
        }
        normalized_copies = []
        for value in (action.before_snapshot, action.after_snapshot, action.response_payload):
            if value is None or value == "":
                continue
            try:
                payload = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return "invalid", None
            if not isinstance(payload, dict):
                return "invalid", None
            present_keys = metadata_keys.intersection(payload)
            if not present_keys:
                continue
            if present_keys != metadata_keys:
                return "invalid", None
            group_request_id = payload.get("groupExternalRequestId")
            leader_action_id = payload.get("leaderActionId")
            action_ids = payload.get("actionIds")
            brand = payload.get("brand")
            normalized_query = payload.get("normalizedQuery")
            before_ids = payload.get("beforeIds")
            desired_ids = payload.get("desiredIds")
            config_version = payload.get("configVersion")
            if not isinstance(group_request_id, str) or not group_request_id.strip():
                return "invalid", None
            if (
                not isinstance(leader_action_id, int)
                or isinstance(leader_action_id, bool)
                or leader_action_id <= 0
            ):
                return "invalid", None
            if (
                not isinstance(action_ids, list)
                or not action_ids
                or any(
                    not isinstance(action_id, int)
                    or isinstance(action_id, bool)
                    or action_id <= 0
                    for action_id in action_ids
                )
            ):
                return "invalid", None
            normalized_action_ids = sorted(set(action_ids))
            if leader_action_id not in normalized_action_ids:
                return "invalid", None
            if not isinstance(brand, str) or not brand.strip():
                return "invalid", None
            if not isinstance(normalized_query, str) or not normalized_query.strip():
                return "invalid", None
            if not isinstance(config_version, str) or not config_version.strip():
                return "invalid", None
            try:
                normalized_before_ids = cls._rollback_id_list(before_ids, "beforeIds")
                normalized_desired_ids = cls._rollback_id_list(desired_ids, "desiredIds")
            except ActionValidationError:
                return "invalid", None
            normalized_copies.append({
                "groupExternalRequestId": group_request_id.strip(),
                "leaderActionId": leader_action_id,
                "actionIds": normalized_action_ids,
                "brand": brand.strip(),
                "normalizedQuery": normalized_query.strip(),
                "beforeIds": normalized_before_ids,
                "desiredIds": normalized_desired_ids,
                "configVersion": config_version.strip(),
            })
        if not normalized_copies:
            return "missing", None
        metadata = normalized_copies[0]
        if any(candidate != metadata for candidate in normalized_copies[1:]):
            return "invalid", None
        return "valid", metadata

    @classmethod
    def _query_demote_group_metadata(cls, action: TopQueryActionPlan) -> Dict[str, Any]:
        state, metadata = cls._query_demote_group_metadata_result(action)
        return metadata if state == "valid" and metadata is not None else {}

    @staticmethod
    def _query_demote_group_fenced_update(
        db: Any,
        action_ids: list[int],
        *,
        expected_status: str,
        expected_fence: datetime.datetime,
        values_by_id: Dict[int, Dict[Any, Any]],
    ) -> bool:
        db.rollback()
        for action_id in action_ids:
            values = values_by_id.get(int(action_id))
            if values is None:
                db.rollback()
                return False
            updated = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == int(action_id),
                TopQueryActionPlan.status == expected_status,
                TopQueryActionPlan.execution_lease_expires_at == expected_fence,
            ).update(values, synchronize_session=False)
            if updated != 1:
                db.rollback()
                return False
        db.commit()
        return True

    @classmethod
    def _restore_query_demote_group(
        cls,
        db: Any,
        original_state: Dict[int, Dict[str, Any]],
        *,
        expected_status: str,
        expected_fence: datetime.datetime,
    ) -> None:
        action_ids = sorted(original_state)
        values_by_id = {
            action_id: {
                TopQueryActionPlan.status: state["status"],
                TopQueryActionPlan.execution_started_at: state["execution_started_at"],
                TopQueryActionPlan.execution_finished_at: state["execution_finished_at"],
                TopQueryActionPlan.execution_lease_expires_at: state["execution_lease_expires_at"],
                TopQueryActionPlan.external_request_id: state["external_request_id"],
                TopQueryActionPlan.before_snapshot: state["before_snapshot"],
                TopQueryActionPlan.after_snapshot: state["after_snapshot"],
                TopQueryActionPlan.response_payload: state["response_payload"],
                TopQueryActionPlan.error_message: state["error_message"],
            }
            for action_id, state in original_state.items()
        }
        if not cls._query_demote_group_fenced_update(
            db,
            action_ids,
            expected_status=expected_status,
            expected_fence=expected_fence,
            values_by_id=values_by_id,
        ):
            raise ActionStateConflictError("query demote action group lease fence changed")

    def _restore_query_demote_group_independently(
        self,
        original_state: Dict[int, Dict[str, Any]],
        *,
        expected_status: str,
        expected_fence: datetime.datetime,
    ) -> None:
        restore_db = SessionLocal()
        try:
            self._restore_query_demote_group(
                restore_db,
                original_state,
                expected_status=expected_status,
                expected_fence=expected_fence,
            )
        finally:
            restore_db.close()

    @staticmethod
    def _safe_database_action_error(exc: Exception) -> str:
        return f"{exc.__class__.__name__}: database action failed"

    def _fail_query_demote_group(
        self,
        db: Any,
        action_ids: list[int],
        exc: Exception,
        *,
        status: Optional[str] = None,
        expected_status: str,
        expected_fence: datetime.datetime,
    ) -> None:
        db.rollback()
        failure_status = status or (
            "execution_unknown" if self._is_ambiguous_external_failure(exc) else "failed"
        )
        finished_at = datetime.datetime.now()
        sanitized_error = sanitize_action_error(exc)
        values_by_id: Dict[int, Dict[Any, Any]] = {}
        for action_id in action_ids:
            action = db.get(TopQueryActionPlan, action_id)
            if not action:
                raise ActionStateConflictError("query demote action group changed")
            group_metadata = self._query_demote_group_metadata(action)
            concurrency_evidence = (
                self._query_demote_persisted_concurrency_evidence(
                    self._preview_json_object(action.before_snapshot)
                )
                or self._query_demote_persisted_concurrency_evidence(
                    self._preview_json_object(action.response_payload)
                )
                or self._query_demote_unknown_concurrency_evidence()
            )
            values_by_id[int(action_id)] = {
                TopQueryActionPlan.status: failure_status,
                TopQueryActionPlan.execution_finished_at: finished_at,
                TopQueryActionPlan.execution_lease_expires_at: None,
                TopQueryActionPlan.error_message: sanitized_error,
                TopQueryActionPlan.response_payload: _bounded_action_response_payload({
                    "action_id": action_id,
                    "dry_run": False,
                    "error": sanitized_error,
                    "external_request_id": str(action.external_request_id or ""),
                    **group_metadata,
                    **concurrency_evidence,
                }),
            }
        if not self._query_demote_group_fenced_update(
            db,
            action_ids,
            expected_status=expected_status,
            expected_fence=expected_fence,
            values_by_id=values_by_id,
        ):
            raise ActionStateConflictError("query demote action group lease fence changed")

    def _query_demote_config_verification(
        self,
        brand: str,
        normalized_query: str,
        desired_ids: list[str],
        *,
        expected_version: str,
    ) -> Dict[str, Any]:
        checked_at = datetime.datetime.now().isoformat()
        try:
            readback = self.operational_service.get_ops_low_weight_config(
                brand,
                normalized_query,
            )
        except Exception as exc:
            return {
                "status": "inconclusive",
                "expectedIds": desired_ids,
                "actualIds": [],
                "missingIds": [],
                "unexpectedIds": [],
                "expectedVersion": expected_version,
                "actualVersion": None,
                "checkedAt": checked_at,
                "errorReason": sanitize_action_error(exc),
            }
        actual_ids = []
        actual_version = None
        if isinstance(readback, dict):
            raw_actual_ids = readback.get("product_ids")
            if isinstance(raw_actual_ids, list):
                actual_ids = sorted({
                    str(item).strip()
                    for item in raw_actual_ids
                    if str(item).strip()
                })
            version = readback.get("version")
            if isinstance(version, str) and version.strip():
                actual_version = version.strip()
        valid = (
            isinstance(readback, dict)
            and readback.get("success") is True
            and isinstance(readback.get("product_ids"), list)
            and actual_version is not None
        )
        if not valid:
            error_reason = (
                readback.get("error_reason")
                if isinstance(readback, dict)
                else "invalid_authoritative_config_response"
            )
            return {
                "status": "inconclusive",
                "expectedIds": desired_ids,
                "actualIds": actual_ids,
                "missingIds": [],
                "unexpectedIds": [],
                "expectedVersion": expected_version,
                "actualVersion": actual_version,
                "checkedAt": checked_at,
                "errorReason": sanitize_action_error(
                    error_reason or "invalid_authoritative_config_response"
                ),
            }
        expected_set = set(desired_ids)
        actual_set = set(actual_ids)
        passed = actual_set == expected_set
        return {
            "status": "passed" if passed else "failed",
            "expectedIds": desired_ids,
            "actualIds": actual_ids,
            "missingIds": sorted(expected_set - actual_set),
            "unexpectedIds": sorted(actual_set - expected_set),
            "expectedVersion": expected_version,
            "actualVersion": actual_version,
            "checkedAt": checked_at,
            "errorReason": None if passed else "config_ids_mismatch",
        }

    @staticmethod
    def _rollback_id_list(value: Any, field_name: str) -> list[str]:
        if not isinstance(value, list):
            raise ActionValidationError(
                f"query demotion rollback requires complete before snapshot: {field_name} must be a list"
            )
        normalized = []
        for item in value:
            if not isinstance(item, (str, int)) or isinstance(item, bool):
                raise ActionValidationError(
                    f"query demotion rollback requires complete before snapshot: {field_name} contains invalid id"
                )
            product_id = str(item).strip()
            if not product_id:
                raise ActionValidationError(
                    f"query demotion rollback requires complete before snapshot: {field_name} contains empty id"
                )
            normalized.append(product_id)
        return sorted(set(normalized))

    @classmethod
    def _query_demote_rollback_source(cls, action: TopQueryActionPlan) -> Dict[str, Any]:
        snapshot = cls._preview_json_object(action.before_snapshot)
        if snapshot.get("complete") is not True or snapshot.get("rollbackAvailable") is not True:
            raise ActionValidationError("query demotion rollback requires complete before snapshot")
        brand = str(snapshot.get("brand") or "").strip()
        normalized_query = str(snapshot.get("normalizedQuery") or "").strip()
        if not brand or not normalized_query:
            raise ActionValidationError("query demotion rollback requires complete before snapshot")
        action_brand = str(action.brand or "").strip()
        action_query = str(action.query_text or "").strip()
        expected_query = transform_governance_payload_query(action_brand, action_query)
        if brand.casefold() != action_brand.casefold() or normalized_query != expected_query:
            raise ActionValidationError("query demotion rollback requires complete before snapshot")
        before_ids = cls._rollback_id_list(snapshot.get("beforeIds"), "beforeIds")
        desired_ids = cls._rollback_id_list(snapshot.get("desiredIds"), "desiredIds")
        raw_action_ids = snapshot.get("actionIds")
        if not isinstance(raw_action_ids, list) or not raw_action_ids:
            raise ActionValidationError("query demotion rollback requires complete before snapshot")
        try:
            action_ids = sorted({int(action_id) for action_id in raw_action_ids})
        except (TypeError, ValueError):
            raise ActionValidationError("query demotion rollback requires complete before snapshot") from None
        if action.id is None or int(action.id) not in action_ids:
            raise ActionValidationError("query demotion rollback requires complete before snapshot")
        group_request_id = str(snapshot.get("groupExternalRequestId") or "").strip()
        leader_action_id = snapshot.get("leaderActionId")
        config_version = str(snapshot.get("configVersion") or "").strip()
        if (
            not group_request_id
            or not isinstance(leader_action_id, int)
            or isinstance(leader_action_id, bool)
            or leader_action_id not in action_ids
            or not config_version
        ):
            raise ActionValidationError("query demotion rollback requires complete before snapshot")
        return {
            "brand": brand,
            "normalizedQuery": normalized_query,
            "beforeIds": before_ids,
            "desiredIds": desired_ids,
            "actionIds": action_ids,
            "groupExternalRequestId": group_request_id,
            "leaderActionId": leader_action_id,
            "configVersion": config_version,
        }

    @classmethod
    def _confirmed_query_demote_rollback_source(
        cls,
        action: TopQueryActionPlan,
    ) -> Dict[str, Any]:
        source_snapshot = cls._query_demote_rollback_source(action)
        source_status = str(action.status or "")
        if source_status not in {
            "verified",
            "verification_failed",
            "verification_inconclusive",
            "verifying",
        }:
            raise ActionStateConflictError(
                f"query demotion action cannot be rolled back from status: {source_status}"
            )
        after_snapshot = cls._preview_json_object(action.after_snapshot)
        config_verification = after_snapshot.get("configVerification")
        config_verification = config_verification if isinstance(config_verification, dict) else {}
        search_effect = after_snapshot.get("searchEffect")
        search_effect = search_effect if isinstance(search_effect, dict) else {}
        metadata_state, metadata = cls._query_demote_group_metadata_result(action)
        expected_metadata = {
            key: source_snapshot[key]
            for key in (
                "groupExternalRequestId",
                "leaderActionId",
                "actionIds",
                "brand",
                "normalizedQuery",
                "beforeIds",
                "desiredIds",
                "configVersion",
            )
        }
        expected_ids = cls._rollback_id_list(
            config_verification.get("expectedIds"),
            "configVerification.expectedIds",
        )
        actual_ids = cls._rollback_id_list(
            config_verification.get("actualIds"),
            "configVerification.actualIds",
        )
        expected_version = str(config_verification.get("expectedVersion") or "").strip()
        actual_version = str(config_verification.get("actualVersion") or "").strip()
        checked_at = str(config_verification.get("checkedAt") or "").strip()
        response = cls._preview_json_object(action.response_payload)
        write_version = response.get("writeVersion")
        verification_status = str(config_verification.get("status") or "")
        write_version_valid = (
            write_version is None
            or (
                isinstance(write_version, str)
                and bool(write_version.strip())
                and write_version.strip() == expected_version
            )
        )
        passed_verification = (
            verification_status == "passed"
            and actual_ids == source_snapshot["desiredIds"]
            and expected_version == actual_version
        )
        failed_verification = (
            verification_status == "failed"
            and actual_ids != source_snapshot["desiredIds"]
            and config_verification.get("errorReason") == "config_ids_mismatch"
        )
        inconclusive_with_search_failure = (
            verification_status == "inconclusive"
            and str(search_effect.get("status") or "") == "failed"
            and bool(search_effect.get("observedRemainingIds"))
        )
        complete_version_evidence = (
            bool(expected_version)
            and bool(actual_version)
            and write_version_valid
        )
        if (
            not (
                passed_verification
                or failed_verification
                or inconclusive_with_search_failure
            )
            or metadata_state != "valid"
            or metadata != expected_metadata
            or expected_ids != source_snapshot["desiredIds"]
            or not checked_at
            or (
                not inconclusive_with_search_failure
                and not complete_version_evidence
            )
            or (
                inconclusive_with_search_failure
                and not write_version_valid
            )
        ):
            raise ActionValidationError(
                "query demotion rollback requires complete confirmed config verification"
            )
        return {
            **source_snapshot,
            "confirmedConfigVerification": {
                "status": verification_status,
                "expectedIds": expected_ids,
                "actualIds": actual_ids,
                "expectedVersion": expected_version,
                "actualVersion": actual_version,
                "checkedAt": checked_at,
            },
        }

    @staticmethod
    def _rollback_verification(
        *,
        status: str,
        expected_ids: list[str],
        actual_ids: list[str],
        expected_version: Optional[str],
        actual_version: Optional[str],
        error_reason: Optional[str],
    ) -> Dict[str, Any]:
        return {
            "status": status,
            "expectedIds": expected_ids,
            "actualIds": actual_ids,
            "expectedVersion": expected_version,
            "actualVersion": actual_version,
            "checkedAt": datetime.datetime.now().isoformat(),
            "errorReason": sanitize_action_error(error_reason) if error_reason else None,
        }

    @staticmethod
    def _rollback_result(action: TopQueryActionPlan, *, no_write: bool = False) -> Dict[str, Any]:
        return {
            "rollbackActionId": int(action.id),
            "status": str(action.status or ""),
            "externalRequestId": str(action.external_request_id or ""),
            "noWrite": bool(no_write),
        }

    @classmethod
    def _query_demote_rollback_matches(
        cls,
        action: TopQueryActionPlan,
        *,
        source_group_request_id: str,
        source_action_ids: list[int],
    ) -> bool:
        payload = cls._preview_json_object(action.request_payload)
        if payload.get("operation") != "restore":
            return False
        rollback_group_request_id = str(
            payload.get("sourceGroupExternalRequestId") or ""
        ).strip()
        normalized_source_group_request_id = str(source_group_request_id or "").strip()
        if (
            not rollback_group_request_id
            or rollback_group_request_id != normalized_source_group_request_id
        ):
            return False
        raw_action_ids = payload.get("rollbackOfActionIds")
        if not isinstance(raw_action_ids, list) or not raw_action_ids:
            return False
        try:
            if any(isinstance(action_id, bool) for action_id in raw_action_ids):
                return False
            rollback_source_ids = sorted({int(action_id) for action_id in raw_action_ids})
            normalized_source_action_ids = sorted({int(action_id) for action_id in source_action_ids})
        except (TypeError, ValueError):
            return False
        return bool(normalized_source_action_ids) and rollback_source_ids == normalized_source_action_ids

    @classmethod
    def _query_demote_rollback_attempts(
        cls,
        db: Any,
        *,
        task_id: int,
        source_group_request_id: str,
        source_action_ids: list[int],
    ) -> list[TopQueryActionPlan]:
        candidates = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
            TopQueryActionPlan.action_type == "query_demote",
        ).order_by(TopQueryActionPlan.id.desc()).all()
        return [
            action for action in candidates
            if cls._query_demote_rollback_matches(
                action,
                source_group_request_id=source_group_request_id,
                source_action_ids=source_action_ids,
            )
        ]

    @staticmethod
    def _rollback_no_write(action: TopQueryActionPlan) -> bool:
        response = TopQueryGovernanceService._preview_json_object(action.response_payload)
        return response.get("noWrite") is True

    def _cas_persist_query_demote_rollback(
        self,
        rollback_action_id: int,
        *,
        expected_after_snapshot: Optional[str],
        expected_lease_expires_at: Optional[datetime.datetime] = None,
        status: str,
        after_snapshot: str,
        response_payload: str,
        error_message: Optional[str],
        execution_finished_at: Optional[datetime.datetime],
    ) -> Dict[str, Any]:
        persisted_after_snapshot = self._preview_json_object(after_snapshot)
        persisted_response_payload = self._preview_json_object(response_payload)
        concurrency_evidence = (
            self._query_demote_persisted_concurrency_evidence(persisted_after_snapshot)
            or self._query_demote_persisted_concurrency_evidence(persisted_response_payload)
            or self._query_demote_unknown_concurrency_evidence()
        )
        persisted_after_snapshot.update(concurrency_evidence)
        persisted_response_payload.update(concurrency_evidence)
        db = SessionLocal()
        try:
            query = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == rollback_action_id,
                TopQueryActionPlan.status == "rollback_pending",
            )
            if expected_lease_expires_at is not None:
                query = query.filter(
                    TopQueryActionPlan.execution_lease_expires_at
                    == expected_lease_expires_at,
                )
            if expected_after_snapshot is None:
                query = query.filter(TopQueryActionPlan.after_snapshot.is_(None))
            else:
                query = query.filter(
                    TopQueryActionPlan.after_snapshot == expected_after_snapshot,
                )
            updated = query.update({
                TopQueryActionPlan.status: status,
                TopQueryActionPlan.after_snapshot: json.dumps(
                    persisted_after_snapshot,
                    ensure_ascii=False,
                ),
                TopQueryActionPlan.response_payload: _bounded_action_response_payload(
                    persisted_response_payload,
                ),
                TopQueryActionPlan.error_message: error_message,
                TopQueryActionPlan.execution_finished_at: execution_finished_at,
                TopQueryActionPlan.execution_lease_expires_at: None,
                TopQueryActionPlan.updated_at: datetime.datetime.now(),
            }, synchronize_session=False)
            if updated == 1:
                db.commit()
            else:
                db.rollback()
            fresh = db.get(TopQueryActionPlan, rollback_action_id)
            if not fresh:
                raise ActionValidationError(
                    f"top query rollback action not found: {rollback_action_id}"
                )
            return {
                "updated": updated == 1,
                "result": self._rollback_result(
                    fresh,
                    no_write=self._rollback_no_write(fresh),
                ),
            }
        finally:
            db.close()

    def _persist_query_demote_rollback_failure(
        self,
        rollback_action_id: int,
        *,
        status: str,
        expected_ids: list[str],
        expected_version: str,
        error_reason: str,
        expected_after_snapshot: Optional[str],
        expected_lease_expires_at: Optional[datetime.datetime] = None,
    ) -> Dict[str, Any]:
        sanitized_error = sanitize_action_error(error_reason)
        verification = self._rollback_verification(
            status="failed" if status == "rollback_failed" else "pending",
            expected_ids=expected_ids,
            actual_ids=[],
            expected_version=expected_version,
            actual_version=None,
            error_reason=sanitized_error,
        )
        db = SessionLocal()
        try:
            rollback = db.get(TopQueryActionPlan, rollback_action_id)
            if not rollback:
                raise ActionValidationError(
                    f"top query rollback action not found: {rollback_action_id}"
                )
            response = self._preview_json_object(rollback.response_payload)
        finally:
            db.close()
        return self._cas_persist_query_demote_rollback(
            rollback_action_id,
            expected_after_snapshot=expected_after_snapshot,
            expected_lease_expires_at=expected_lease_expires_at,
            status=status,
            after_snapshot=json.dumps({
                "rollbackVerification": verification,
                "searchEffect": {"status": "pending"},
            }, ensure_ascii=False),
            response_payload=json.dumps({
                **response,
                "error": sanitized_error,
                "rollbackVerification": verification,
            }, ensure_ascii=False),
            error_message=sanitized_error,
            execution_finished_at=datetime.datetime.now(),
        )

    def _reconcile_pending_query_demote_rollback(
        self,
        rollback_action_id: int,
        source_snapshot: Dict[str, Any],
        *,
        expected_after_snapshot: Optional[str],
        expected_lease_expires_at: Optional[datetime.datetime] = None,
    ) -> Dict[str, Any]:
        brand = source_snapshot["brand"]
        normalized_query = source_snapshot["normalizedQuery"]
        before_ids = source_snapshot["beforeIds"]
        desired_ids = source_snapshot["desiredIds"]
        actual_ids: list[str] = []
        actual_version = None
        error_reason = None
        authoritative = False
        try:
            current = self.operational_service.get_ops_low_weight_config(
                brand,
                normalized_query,
            )
            config_error = self._query_demote_authoritative_config_error(current)
            if config_error:
                error_reason = config_error
                if isinstance(current, dict):
                    raw_actual_ids = current.get("product_ids")
                    if isinstance(raw_actual_ids, list):
                        actual_ids = sorted({
                            str(item).strip()
                            for item in raw_actual_ids
                            if str(item).strip()
                        })
                    version = current.get("version")
                    if isinstance(version, str) and version.strip():
                        actual_version = version.strip()
            else:
                authoritative = True
                actual_ids = sorted({
                    str(item).strip()
                    for item in current["product_ids"]
                    if str(item).strip()
                })
                actual_version = str(current["version"]).strip()
        except Exception as exc:
            error_reason = sanitize_action_error(exc)

        rolled_back = authoritative and actual_ids == before_ids
        mismatch = authoritative and not rolled_back
        drifted = mismatch and actual_ids != desired_ids
        if authoritative and actual_ids == desired_ids and not rolled_back:
            error_reason = "rollback_write_not_observed"
        elif drifted:
            error_reason = "configuration_changed_while_rollback_pending"
        verification = self._rollback_verification(
            status="passed" if rolled_back else "failed" if mismatch else "pending",
            expected_ids=before_ids,
            actual_ids=actual_ids,
            expected_version=None,
            actual_version=actual_version,
            error_reason=error_reason,
        )
        db = SessionLocal()
        try:
            rollback = db.get(TopQueryActionPlan, rollback_action_id)
            if not rollback:
                raise ActionValidationError(
                    f"top query rollback action not found: {rollback_action_id}"
                )
            response = self._preview_json_object(rollback.response_payload)
        finally:
            db.close()
        persisted = self._cas_persist_query_demote_rollback(
            rollback_action_id,
            expected_after_snapshot=expected_after_snapshot,
            expected_lease_expires_at=expected_lease_expires_at,
            status=(
                "rolled_back"
                if rolled_back
                else "rollback_failed"
                if mismatch
                else "rollback_pending"
            ),
            after_snapshot=json.dumps({
                "rollbackVerification": verification,
                "searchEffect": {"status": "pending"},
            }, ensure_ascii=False),
            response_payload=json.dumps({
                **response,
                "noWrite": True if rolled_back else response.get("noWrite", False),
                "rollbackVerification": verification,
            }, ensure_ascii=False),
            error_message=sanitize_action_error(error_reason) if error_reason else None,
            execution_finished_at=datetime.datetime.now() if authoritative else None,
        )
        return persisted

    def reconcile_pending_query_demote_rollback(
        self,
        rollback_action_id: int,
    ) -> Dict[str, Any]:
        lookup_db = SessionLocal()
        try:
            rollback = lookup_db.get(TopQueryActionPlan, rollback_action_id)
            if not rollback:
                raise ActionValidationError(
                    f"top query rollback action not found: {rollback_action_id}"
                )
            task_id = int(rollback.task_id)
            brand = str(rollback.brand or "")
        finally:
            lookup_db.close()
        try:
            with top_query_brand_governance_lock(brand):
                with top_query_task_lock(task_id):
                    return self._reconcile_pending_query_demote_rollback_locked(
                        rollback_action_id,
                    )
        except TopQueryTaskLockBusy:
            return {
                "rollbackActionId": int(rollback_action_id),
                "status": "busy",
                "processed": False,
            }

    def _reconcile_pending_query_demote_rollback_locked(
        self,
        rollback_action_id: int,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            rollback = db.get(TopQueryActionPlan, rollback_action_id)
            if not rollback:
                raise ActionValidationError(
                    f"top query rollback action not found: {rollback_action_id}"
                )
            status = str(rollback.status or "")
            if status != "rollback_pending":
                return {
                    **self._rollback_result(
                        rollback,
                        no_write=self._rollback_no_write(rollback),
                    ),
                    "processed": False,
                    "skipped": "rollback_not_pending",
                }
            payload = self._preview_json_object(rollback.request_payload)
            if (
                str(rollback.action_type or "") != "query_demote"
                or not uses_authoritative_query_demotion(payload)
                or payload.get("operation") != "restore"
            ):
                raise LegacyQueryDemotionContractError(
                    rollback_action_id,
                    status,
                )
            raw_source_action_ids = payload.get("rollbackOfActionIds")
            if (
                not isinstance(raw_source_action_ids, list)
                or not raw_source_action_ids
                or any(isinstance(action_id, bool) for action_id in raw_source_action_ids)
            ):
                raise ActionValidationError(
                    "query demotion rollback pending source identity is incomplete"
                )
            try:
                source_action_ids = sorted({
                    int(action_id) for action_id in raw_source_action_ids
                })
            except (TypeError, ValueError):
                raise ActionValidationError(
                    "query demotion rollback pending source identity is incomplete"
                ) from None
            source_group_request_id = str(
                payload.get("sourceGroupExternalRequestId") or ""
            ).strip()
            if not source_group_request_id:
                raise ActionValidationError(
                    "query demotion rollback pending source identity is incomplete"
                )
            sources = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(source_action_ids),
            ).order_by(TopQueryActionPlan.id.asc()).all()
            if len(sources) != len(source_action_ids):
                raise ActionValidationError(
                    "query demotion rollback pending source group is incomplete"
                )
            confirmed = [
                self._confirmed_query_demote_rollback_source(source)
                for source in sources
            ]
            source_evidence = confirmed[0]["confirmedConfigVerification"]
            source_snapshot = {
                key: value
                for key, value in confirmed[0].items()
                if key != "confirmedConfigVerification"
            }
            if (
                source_snapshot["actionIds"] != source_action_ids
                or source_snapshot["groupExternalRequestId"] != source_group_request_id
                or any(
                    item["confirmedConfigVerification"] != source_evidence
                    or {
                        key: value
                        for key, value in item.items()
                        if key != "confirmedConfigVerification"
                    } != source_snapshot
                    for item in confirmed[1:]
                )
                or not self._query_demote_rollback_matches(
                    rollback,
                    source_group_request_id=source_group_request_id,
                    source_action_ids=source_action_ids,
                )
            ):
                raise ActionValidationError(
                    "query demotion rollback pending source group identity mismatch"
                )
            rollback_before = self._preview_json_object(rollback.before_snapshot)
            try:
                rollback_before_ids = self._rollback_id_list(
                    rollback_before.get("beforeIds"),
                    "rollback.beforeIds",
                )
                rollback_desired_ids = self._rollback_id_list(
                    rollback_before.get("desiredIds"),
                    "rollback.desiredIds",
                )
            except ActionValidationError:
                raise ActionValidationError(
                    "query demotion rollback pending snapshot is incomplete"
                ) from None
            if (
                int(rollback.task_id) != int(sources[0].task_id)
                or normalize_query_key(rollback.brand)
                != normalize_query_key(source_snapshot["brand"])
                or normalize_governance_query_key(
                    source_snapshot["brand"],
                    rollback.query_text,
                )
                != normalize_governance_query_key(
                    source_snapshot["brand"],
                    source_snapshot["normalizedQuery"],
                )
                or rollback_before_ids != source_snapshot["desiredIds"]
                or rollback_desired_ids != source_snapshot["beforeIds"]
                or sorted(rollback_before.get("sourceActionIds") or [])
                != source_action_ids
                or str(
                    rollback_before.get("sourceGroupExternalRequestId") or ""
                ).strip() != source_group_request_id
            ):
                raise ActionValidationError(
                    "query demotion rollback pending snapshot identity mismatch"
                )
            expected_after_snapshot = rollback.after_snapshot
            claimed_at = datetime.datetime.now()
            claimed_until = _verification_lease_token(
                claimed_at + datetime.timedelta(
                    seconds=self.ACTION_EXECUTION_LEASE_SECONDS,
                )
            )
            claimed = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == rollback_action_id,
                TopQueryActionPlan.status == "rollback_pending",
                or_(
                    TopQueryActionPlan.execution_lease_expires_at.is_(None),
                    TopQueryActionPlan.execution_lease_expires_at <= claimed_at,
                ),
            ).update({
                TopQueryActionPlan.execution_lease_expires_at: claimed_until,
            }, synchronize_session=False)
            if claimed != 1:
                db.rollback()
                return {
                    "rollbackActionId": int(rollback_action_id),
                    "status": "busy",
                    "processed": False,
                }
            db.commit()
        finally:
            db.close()
        persisted = self._reconcile_pending_query_demote_rollback(
            rollback_action_id,
            source_snapshot,
            expected_after_snapshot=expected_after_snapshot,
            expected_lease_expires_at=claimed_until,
        )
        if not persisted["updated"]:
            result = persisted["result"]
            return {
                **result,
                "processed": False,
                "skipped": "rollback_lease_fence_changed",
            }
        return {**persisted["result"], "processed": True}

    def rollback_query_demote_action(self, source_action_id: int, operator: str) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            source = db.get(TopQueryActionPlan, source_action_id)
            if not source:
                raise ActionValidationError(f"top query action not found: {source_action_id}")
            if str(source.action_type or "") != "query_demote":
                raise ActionValidationError("only query_demote actions can be rolled back")
            if not self._is_authoritative_query_demote_action(source):
                raise LegacyQueryDemotionContractError(
                    source_action_id,
                    str(source.status or ""),
                )
            assert_query_governance_allowed(str(source.brand or ""), str(source.query_text or ""))
            confirmed_source = self._confirmed_query_demote_rollback_source(source)
            source_evidence = confirmed_source["confirmedConfigVerification"]
            source_snapshot = {
                key: value
                for key, value in confirmed_source.items()
                if key != "confirmedConfigVerification"
            }
            source_group = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id.in_(source_snapshot["actionIds"]),
            ).order_by(TopQueryActionPlan.id.asc()).all()
            if (
                len(source_group) != len(source_snapshot["actionIds"])
                or any(
                    not self._is_authoritative_query_demote_action(peer)
                    or str(peer.status or "") != str(source.status or "")
                    or (
                        peer_confirmed := self._confirmed_query_demote_rollback_source(peer)
                    ).get("confirmedConfigVerification") != source_evidence
                    or {
                        key: value
                        for key, value in peer_confirmed.items()
                        if key != "confirmedConfigVerification"
                    } != source_snapshot
                    for peer in source_group
                )
            ):
                raise ActionValidationError(
                    "query demotion rollback requires complete group identity"
                )
            task_id = int(source.task_id)
            workflow_run_id = source.workflow_run_id
            round_no = int(source.round_no or 0)
            check_key = str(source.check_key or "")
            source_query_text = str(source.query_text or "")

            rollback_attempts = self._query_demote_rollback_attempts(
                db,
                task_id=task_id,
                source_group_request_id=source_snapshot["groupExternalRequestId"],
                source_action_ids=source_snapshot["actionIds"],
            )
            rolled_back_action = next((
                action for action in rollback_attempts
                if str(action.status or "") == "rolled_back"
            ), None)
            if rolled_back_action is not None:
                return self._rollback_result(
                    rolled_back_action,
                    no_write=self._rollback_no_write(rolled_back_action),
                )
            pending_rollback = next((
                action for action in rollback_attempts
                if str(action.status or "") == "rollback_pending"
            ), None)
            pending_rollback_id = int(pending_rollback.id) if pending_rollback else None
            rollback_attempt = len(rollback_attempts) + 1
        finally:
            db.close()

        if pending_rollback_id is not None:
            return self.reconcile_pending_query_demote_rollback(
                pending_rollback_id,
            )

        brand = source_snapshot["brand"]
        normalized_query = source_snapshot["normalizedQuery"]
        before_ids = source_snapshot["beforeIds"]
        desired_ids = source_snapshot["desiredIds"]
        try:
            current = self.operational_service.get_ops_low_weight_config(brand, normalized_query)
        except Exception as exc:
            raise ActionExecutionError(
                f"query demotion rollback authoritative config read failed: {sanitize_action_error(exc)}"
            ) from exc
        config_error = self._query_demote_authoritative_config_error(current)
        if config_error:
            raise ActionExecutionError(
                f"query demotion rollback authoritative config unavailable: {config_error}"
            )
        current_ids = sorted({str(item).strip() for item in current["product_ids"] if str(item).strip()})
        current_version = str(current["version"]).strip()
        expected_current_ids = (
            sorted(source_evidence["actualIds"])
            if source_evidence.get("status") == "failed"
            else desired_ids
        )
        if current_ids not in (expected_current_ids, before_ids):
            raise ActionStateConflictError(
                "query demotion configuration changed after execution; refresh before rollback"
            )
        if (
            current_ids != before_ids
            and get_env() == "prod"
            and not self._query_demote_safe_write_available()
        ):
            raise ActionExecutionError(
                "query_demote rollback server atomic compare unavailable"
            )

        external_request_id = str(uuid.uuid4())
        rollback_identity = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            ":".join([
                "top-query-query-demote-rollback",
                source_snapshot["groupExternalRequestId"],
                ",".join(str(action_id) for action_id in source_snapshot["actionIds"]),
            ]),
        ))
        idempotency_key = f"query-demote-rollback:{rollback_identity}:attempt:{rollback_attempt}"
        request_payload = {
            "operation": "restore",
            "execution_contract": AUTHORITATIVE_QUERY_DEMOTION_V1,
            "rollbackOfActionId": int(source_action_id),
            "rollbackOfActionIds": source_snapshot["actionIds"],
            "beforeIds": before_ids,
            "expectedIds": before_ids,
            "brand": brand,
            "query": normalized_query,
            "operator": str(operator or ""),
            "sourceGroupExternalRequestId": source_snapshot["groupExternalRequestId"],
        }
        rollback_before_snapshot = {
            "complete": True,
            "brand": brand,
            "normalizedQuery": normalized_query,
            "beforeIds": current_ids,
            "desiredIds": before_ids,
            "configVersion": current_version,
            "sourceActionIds": source_snapshot["actionIds"],
            "sourceGroupExternalRequestId": source_snapshot["groupExternalRequestId"],
            **self._query_demote_concurrency_control_evidence(),
        }
        db = SessionLocal()
        try:
            rollback = TopQueryActionPlan(
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                round_no=round_no,
                idempotency_key=idempotency_key,
                external_request_id=external_request_id,
                brand=brand,
                query_text=source_query_text,
                check_key=check_key,
                tool_key="query_demote",
                action_type="query_demote",
                execution_mode="human_approval",
                status="rollback_pending",
                request_payload=json.dumps(request_payload, ensure_ascii=False),
                before_snapshot=json.dumps(rollback_before_snapshot, ensure_ascii=False),
                response_payload=json.dumps({
                    "operation": "restore",
                    "externalRequestId": external_request_id,
                    "operator": str(operator or ""),
                    **self._query_demote_concurrency_control_evidence(),
                }, ensure_ascii=False),
                reason="restore query demotion configuration",
            )
            db.add(rollback)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.query(TopQueryActionPlan).filter(
                    TopQueryActionPlan.idempotency_key == idempotency_key,
                ).first()
                if existing is None:
                    raise
                return self._rollback_result(
                    existing,
                    no_write=self._rollback_no_write(existing),
                )
            db.refresh(rollback)
            rollback_action_id = int(rollback.id)
            rollback_fence = rollback.after_snapshot

            if current_ids == before_ids:
                verification = self._rollback_verification(
                    status="passed",
                    expected_ids=before_ids,
                    actual_ids=current_ids,
                    expected_version=current_version,
                    actual_version=current_version,
                    error_reason=None,
                )
                persisted = self._cas_persist_query_demote_rollback(
                    rollback_action_id,
                    expected_after_snapshot=rollback_fence,
                    status="rolled_back",
                    after_snapshot=json.dumps({
                    "rollbackVerification": verification,
                    "searchEffect": {"status": "pending"},
                    }, ensure_ascii=False),
                    response_payload=json.dumps({
                    "operation": "restore",
                    "externalRequestId": external_request_id,
                    "operator": str(operator or ""),
                    "noWrite": True,
                    }, ensure_ascii=False),
                    error_message=None,
                    execution_finished_at=datetime.datetime.now(),
                )
                return persisted["result"]
        finally:
            db.close()

        try:
            write_result = self.operational_service.set_ops_low_weight_config(
                brand,
                normalized_query,
                before_ids,
                expected_version=current_version,
                external_request_id=external_request_id,
            )
        except Exception as exc:
            persisted = self._persist_query_demote_rollback_failure(
                rollback_action_id,
                status="rollback_pending",
                expected_ids=before_ids,
                expected_version=current_version,
                error_reason=str(exc),
                expected_after_snapshot=rollback_fence,
            )
            if not persisted["updated"]:
                return persisted["result"]
            raise ActionExecutionError(
                f"query demotion rollback write failed: {sanitize_action_error(exc)}"
            ) from exc
        write_version = write_result.get("version") if isinstance(write_result, dict) else None
        strict_write_success = (
            isinstance(write_result, dict)
            and write_result.get("success") is True
            and write_result.get("conflict") is False
            and isinstance(write_version, str)
            and bool(write_version.strip())
        )
        if not strict_write_success:
            raw_error_reason = (
                write_result.get("error_reason")
                if isinstance(write_result, dict)
                else "invalid_conditional_write_response"
            )
            explicit_failure = (
                isinstance(write_result, dict)
                and (
                    write_result.get("conflict") is True
                    or (
                        write_result.get("success") is False
                        and write_result.get("conflict") is False
                        and str(raw_error_reason or "") != "conditional_write_request_failed"
                    )
                )
            )
            failure_status = "rollback_failed" if explicit_failure else "rollback_pending"
            error_reason = sanitize_action_error(
                raw_error_reason or "invalid_conditional_write_response"
            )
            persisted = self._persist_query_demote_rollback_failure(
                rollback_action_id,
                status=failure_status,
                expected_ids=before_ids,
                expected_version=current_version,
                error_reason=error_reason,
                expected_after_snapshot=rollback_fence,
            )
            if not persisted["updated"]:
                return persisted["result"]
            raise ActionExecutionError(f"query demotion rollback write failed: {error_reason}")

        verification: Dict[str, Any]
        try:
            readback = self.operational_service.get_ops_low_weight_config(brand, normalized_query)
            readback_error = self._query_demote_authoritative_config_error(readback)
            if readback_error:
                verification = self._rollback_verification(
                    status="pending",
                    expected_ids=before_ids,
                    actual_ids=[],
                    expected_version=str(write_version).strip(),
                    actual_version=None,
                    error_reason=readback_error,
                )
                final_status = "rollback_pending"
            else:
                actual_ids = sorted({
                    str(item).strip() for item in readback["product_ids"] if str(item).strip()
                })
                actual_version = str(readback["version"]).strip()
                passed = actual_ids == before_ids
                verification = self._rollback_verification(
                    status="passed" if passed else "failed",
                    expected_ids=before_ids,
                    actual_ids=actual_ids,
                    expected_version=str(write_version).strip(),
                    actual_version=actual_version,
                    error_reason=None if passed else "config_ids_mismatch",
                )
                final_status = "rolled_back" if passed else "rollback_failed"
        except Exception as exc:
            verification = self._rollback_verification(
                status="pending",
                expected_ids=before_ids,
                actual_ids=[],
                expected_version=str(write_version).strip(),
                actual_version=None,
                error_reason=sanitize_action_error(exc),
            )
            final_status = "rollback_pending"

        db = SessionLocal()
        try:
            rollback = db.get(TopQueryActionPlan, rollback_action_id)
            if not rollback:
                raise ActionValidationError(
                    f"top query rollback action not found: {rollback_action_id}"
                )
            response = self._preview_json_object(rollback.response_payload)
        finally:
            db.close()
        persisted = self._cas_persist_query_demote_rollback(
            rollback_action_id,
            expected_after_snapshot=rollback_fence,
            status=final_status,
            after_snapshot=json.dumps({
                "rollbackVerification": verification,
                "searchEffect": {"status": "pending"},
            }, ensure_ascii=False),
            response_payload=json.dumps({
                **response,
                "operation": "restore",
                "externalRequestId": external_request_id,
                "operator": str(operator or ""),
                "writeVersion": str(write_version).strip(),
                "rollbackVerification": verification,
            }, ensure_ascii=False),
            error_message=(
                sanitize_action_error(verification.get("errorReason"))
                if verification.get("errorReason")
                else None
            ),
            execution_finished_at=datetime.datetime.now(),
        )
        return persisted["result"]



    def _new_query_demote_verification_snapshot(
        self,
        verification: Dict[str, Any],
        *,
        group_external_request_id: str,
        leader_action_id: int,
        action_ids: list[int],
    ) -> Dict[str, Any]:
        started_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        deadline_at = started_at + datetime.timedelta(
            seconds=self.QUERY_DEMOTION_VERIFICATION_WINDOW_SECONDS,
        )
        return {
            "configVerification": verification,
            "searchEffect": {"status": "pending"},
            "groupExternalRequestId": group_external_request_id,
            "leaderActionId": leader_action_id,
            "actionIds": action_ids,
            "verificationStartedAt": started_at.isoformat().replace("+00:00", "Z"),
            "verificationDeadlineAt": deadline_at.isoformat().replace("+00:00", "Z"),
            "verificationAttempts": 0,
            "verificationConsecutivePasses": 0,
        }

    @_task_execution_locked()
    def execute_task_manual_actions(
        self,
        task_id: int,
        dry_run: bool = False,
        expected_config_version: Optional[str] = None,
        expected_action_ids: Optional[list[int]] = None,
    ) -> Dict[str, Any]:
        normalized_expected_action_ids = None
        if not dry_run and expected_action_ids is not None:
            normalized_expected_action_ids = self._normalize_expected_action_ids(
                expected_action_ids,
            )
        db = SessionLocal()
        try:
            task = db.get(TopQueryTask, task_id)
            if not task:
                raise ValueError(f"top query task not found: {task_id}")
            assert_query_governance_allowed(str(task.brand or ""), str(task.query_text or ""))
            candidate_actions = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.task_id == task_id,
                TopQueryActionPlan.execution_mode == "human_approval",
                TopQueryActionPlan.status.in_(["waiting_human_approval", "pending_confirmation", "approved"]),
            ).order_by(TopQueryActionPlan.id.asc()).all()
            actions = [
                action for action in candidate_actions
                if self._is_displayable_governance_action(action)
            ]
            authoritative_query_demote_action_ids = [
                int(action.id)
                for action in actions
                if self._is_authoritative_query_demote_action(action)
            ]
            if normalized_expected_action_ids is not None:
                actions_by_id = {int(action.id): action for action in actions}
                if any(
                    action_id not in actions_by_id
                    for action_id in normalized_expected_action_ids
                ):
                    raise ActionValidationError(
                        "expected_action_ids must contain only executable human actions for the requested task"
                    )
                if (
                    authoritative_query_demote_action_ids
                    and sorted(normalized_expected_action_ids)
                    != sorted(authoritative_query_demote_action_ids)
                ):
                    raise ActionStateConflictError(
                        "query demote action scope changed; refresh preview"
                    )
                expected_ids = set(normalized_expected_action_ids)
                actions = [
                    action for action in actions
                    if int(action.id) in expected_ids
                ]
            if str(task.query_source or "top_query") == "hot_query" and len(actions) > 1:
                raise ValueError("当前任务包含主推荐和备选，请先选择一个方案再执行")
            query_demote_action_ids = [
                int(action.id)
                for action in actions
                if self._is_authoritative_query_demote_action(action)
            ]
            other_action_ids = [
                int(action.id)
                for action in actions
                if int(action.id) not in set(query_demote_action_ids)
            ]
            if query_demote_action_ids and not dry_run:
                if normalized_expected_action_ids is None:
                    raise ActionValidationError(
                        "expected_action_ids is required for human query_demote execution"
                    )
                if not str(expected_config_version or "").strip():
                    raise ActionValidationError(
                        "expected_config_version is required for human query_demote execution"
                    )
        finally:
            db.close()

        executed = []
        skipped = []
        query_demote_preview = None
        scoped_query_demote_action_ids = (
            query_demote_action_ids
        )
        if scoped_query_demote_action_ids:
            try:
                if dry_run:
                    query_demote_preview = self._preview_query_demote_execution(
                        scoped_query_demote_action_ids,
                    )
                    demotion_results = [
                        {**query_demote_preview, "action_id": action_id}
                        for action_id in scoped_query_demote_action_ids
                    ]
                else:
                    demotion_results = self._execute_query_demote_group(
                        scoped_query_demote_action_ids,
                        dry_run=False,
                        expected_config_version=expected_config_version,
                        expected_task_id=task_id,
                        bind_to_manual_scope=True,
                    )
                status_db = SessionLocal()
                try:
                    persisted_statuses = {
                        int(action.id): str(action.status or "")
                        for action in status_db.query(TopQueryActionPlan).filter(
                            TopQueryActionPlan.id.in_(scoped_query_demote_action_ids),
                        ).all()
                    }
                finally:
                    status_db.close()
                for result in demotion_results:
                    action_id = int(result["action_id"])
                    status = (
                        "dry_run"
                        if dry_run
                        else persisted_statuses.get(action_id, "verifying")
                    )
                    executed.append({
                        "actionId": action_id,
                        "status": status,
                        "result": result,
                    })
            except (ActionStateConflictError, ActionValidationError):
                raise
            except Exception as exc:
                sanitized_error = sanitize_action_error(exc)
                for action_id in scoped_query_demote_action_ids:
                    status_db = SessionLocal()
                    try:
                        action = status_db.get(TopQueryActionPlan, action_id)
                        status = str(action.status or "failed") if action else "failed"
                    finally:
                        status_db.close()
                    skipped.append({
                        "actionId": action_id,
                        "status": status,
                        "error": sanitized_error,
                    })

        for action_id in other_action_ids:
            try:
                result = self.execute_approved_action(action_id, dry_run=dry_run)
                executed.append({
                    "actionId": action_id,
                    "status": str(
                        result.get("status")
                        or ("dry_run_executed" if dry_run else "executed")
                    ),
                    "result": result,
                })
            except Exception as exc:
                skipped.append({"actionId": action_id, "status": "failed", "error": sanitize_action_error(exc)})
        response = {
            "taskId": task_id,
            "executedActions": executed,
            "skippedActions": skipped,
            "verificationSummary": {
                "verified": sum(item["status"] == "verified" for item in executed),
                "failed": sum(
                    item["status"] == "verification_failed" for item in executed
                ),
                "inconclusive": sum(
                    item["status"] == "verification_inconclusive" for item in executed
                ),
                "pending": sum(item["status"] == "verifying" for item in executed),
            },
        }
        if query_demote_preview is not None:
            response["queryDemotePreview"] = query_demote_preview
        return response

    @staticmethod
    def _normalize_expected_action_ids(expected_action_ids: Any) -> list[int]:
        if not isinstance(expected_action_ids, list) or not expected_action_ids:
            raise ActionValidationError(
                "expected_action_ids must be a non-empty list of positive integers"
            )
        normalized_action_ids = []
        for action_id in expected_action_ids:
            if (
                isinstance(action_id, bool)
                or not isinstance(action_id, int)
                or action_id <= 0
            ):
                raise ActionValidationError(
                    "expected_action_ids must be a non-empty list of positive integers"
                )
            normalized_action_ids.append(action_id)
        if len(set(normalized_action_ids)) != len(normalized_action_ids):
            raise ActionValidationError("expected_action_ids must not contain duplicates")
        return normalized_action_ids

    def preview_task_manual_actions(self, task_id: int) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            task = db.get(TopQueryTask, task_id)
            if not task:
                raise ValueError(f"top query task not found: {task_id}")
            brand = str(task.brand or "")
            query = str(task.query_text or "")
            assert_query_governance_allowed(brand, query)
            candidate_actions = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.task_id == task_id,
                TopQueryActionPlan.execution_mode == "human_approval",
                TopQueryActionPlan.status.in_([
                    "waiting_human_approval",
                    "pending_confirmation",
                    "approved",
                ]),
                TopQueryActionPlan.action_type == "query_demote",
            ).order_by(TopQueryActionPlan.id.asc()).all()

            actions = []
            authoritative_actions = []
            action_sources: Dict[int, tuple[Dict[str, Any], ...]] = {}
            target_ids: set[str] = set()
            add_ids: set[str] = set()
            remove_ids: set[str] = set()
            target_products: Dict[str, Dict[str, Any]] = {}
            for action in candidate_actions:
                payload = self._load_action_payload(action)
                if not uses_authoritative_query_demotion(payload):
                    continue
                check_key = str(payload.get("check_key") or action.check_key or "")
                if (
                    check_key != "inventory"
                    or not self._is_displayable_governance_action(action)
                ):
                    continue
                action_id = int(action.id)
                action_add_ids, action_remove_ids = self._query_demote_action_deltas(payload)
                action_target_ids = sorted(set(action_add_ids) | set(action_remove_ids))
                add_ids.update(action_add_ids)
                remove_ids.update(action_remove_ids)
                actions.append((action_id, check_key, action_target_ids))
                authoritative_actions.append(action)
                action_sources[action_id] = (
                    payload,
                    self._preview_json_object(action.response_payload),
                    self._preview_snapshot_source(action.before_snapshot, "before"),
                    self._preview_snapshot_source(action.after_snapshot, "after"),
                )
                for product_id in action_target_ids:
                    target_ids.add(product_id)
                    product = target_products.setdefault(
                        product_id,
                        self._empty_preview_product(product_id),
                    )
                    product["actionIds"].append(action_id)
                    product["checkKey"].append(check_key)

            authoritative_brand = brand
            authoritative_query = transform_governance_payload_query(brand, query)
            if authoritative_actions:
                _, authoritative_brand, authoritative_query = (
                    self._authoritative_query_demote_group_identity(
                        db,
                        authoritative_actions,
                    )
                )
                assert_queries_governance_allowed_in_session(
                    db,
                    authoritative_brand,
                    [
                        str(action.query_text or "")
                        for action in authoritative_actions
                    ] + [
                        str(self._load_action_payload(action).get("query") or "")
                        for action in authoritative_actions
                    ] + [authoritative_query],
                )

            score_sources = []
        finally:
            db.close()

        action_ids = [action_id for action_id, _, _ in actions]
        sorted_target_ids = sorted(target_ids)
        if not actions:
            return self._build_manual_action_preview(
                task_id=task_id,
                brand=brand,
                query=query,
                action_ids=[],
                target_ids=[],
                target_products=[],
                blocked_reason="no_query_demote_actions",
            )

        for source_index in range(4):
            for action_id, _, action_target_ids in actions:
                source = action_sources[action_id][source_index]
                product_names = source.get("product_names") or source.get("productNames")
                product_names = product_names if isinstance(product_names, dict) else {}
                for product_id in action_target_ids:
                    product_name = product_names.get(product_id)
                    if product_name not in (None, ""):
                        self._merge_preview_product_metadata(
                            target_products[product_id],
                            {"productId": product_id, "productName": product_name},
                        )
                for candidate in self._preview_action_product_sources(source):
                    product_id = self._preview_product_id(candidate)
                    if product_id in target_products:
                        self._merge_preview_product_metadata(
                            target_products[product_id],
                            candidate,
                        )
                if len(action_target_ids) == 1:
                    self._merge_preview_product_metadata(
                        target_products[action_target_ids[0]],
                        source,
                    )

        for candidate in score_sources:
            product_id = self._preview_product_id(candidate)
            if product_id in target_products:
                self._merge_preview_product_metadata(target_products[product_id], candidate)

        normalized_products = []
        for product_id in sorted_target_ids:
            product = target_products[product_id]
            product["actionIds"] = sorted(set(product["actionIds"]))
            product["checkKey"] = sorted(set(product["checkKey"]))
            normalized_products.append(product)

        if (
            get_env() == "prod"
            and not self._query_demote_safe_write_available()
        ):
            return self._build_manual_action_preview(
                task_id=task_id,
                brand=brand,
                query=query,
                action_ids=action_ids,
                target_ids=sorted_target_ids,
                target_products=normalized_products,
                blocked_reason="server_atomic_compare_unavailable",
            )

        try:
            config = self.operational_service.get_ops_low_weight_config(
                authoritative_brand,
                authoritative_query,
            )
        except Exception:
            config = {
                "success": False,
                "product_ids": [],
                "version": None,
                "error_reason": "authoritative_config_request_failed",
            }
        config_error = self._query_demote_authoritative_config_error(config)
        if config_error:
            return self._build_manual_action_preview(
                task_id=task_id,
                brand=brand,
                query=query,
                action_ids=action_ids,
                target_ids=sorted_target_ids,
                target_products=normalized_products,
                blocked_reason=config_error,
            )

        before_ids = sorted({
            str(item).strip()
            for item in config["product_ids"]
            if str(item).strip()
        })
        before_set = set(before_ids)
        desired_ids = sorted((before_set | add_ids) - remove_ids)
        desired_set = set(desired_ids)
        return self._build_manual_action_preview(
            task_id=task_id,
            brand=brand,
            query=query,
            action_ids=action_ids,
            target_ids=sorted_target_ids,
            target_products=normalized_products,
            before_ids=before_ids,
            desired_ids=desired_ids,
            added_ids=sorted(desired_set - before_set),
            retained_ids=sorted(desired_set & before_set),
            removed_ids=sorted(before_set - desired_set),
            config_version=str(config["version"]).strip(),
            executable=True,
            rollback_available=True,
        )

    @staticmethod
    def _build_manual_action_preview(
        *,
        task_id: int,
        brand: str,
        query: str,
        action_ids: list[int],
        target_ids: list[str],
        target_products: list[Dict[str, Any]],
        before_ids: Optional[list[str]] = None,
        desired_ids: Optional[list[str]] = None,
        added_ids: Optional[list[str]] = None,
        retained_ids: Optional[list[str]] = None,
        removed_ids: Optional[list[str]] = None,
        config_version: Optional[str] = None,
        executable: bool = False,
        rollback_available: bool = False,
        blocked_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "taskId": task_id,
            "brand": brand,
            "query": query,
            "actionIds": action_ids,
            "targetIds": target_ids,
            "beforeIds": before_ids or [],
            "desiredIds": desired_ids or [],
            "addedIds": added_ids or [],
            "retainedIds": retained_ids or [],
            "removedIds": removed_ids or [],
            "targetProducts": target_products,
            "configVersion": config_version,
            "rollbackAvailable": rollback_available,
            "executable": executable,
            "blockedReason": blocked_reason,
        }

    @staticmethod
    def _empty_preview_product(product_id: str) -> Dict[str, Any]:
        return {
            "productId": product_id,
            "productName": None,
            "currentPrice": None,
            "referencePrice": None,
            "rank": None,
            "checkKey": [],
            "actionIds": [],
        }

    @staticmethod
    def _preview_product_id(product: Dict[str, Any]) -> str:
        return str(
            product.get("productId")
            or product.get("product_id")
            or product.get("skuId")
            or product.get("sku_id")
            or product.get("spuId")
            or product.get("spu_id")
            or product.get("sku")
            or product.get("spu")
            or ""
        ).strip()

    @classmethod
    def _merge_preview_product_metadata(
        cls,
        target: Dict[str, Any],
        source: Dict[str, Any],
    ) -> None:
        mappings = {
            "productName": ("productName", "product_name", "name", "title"),
            "currentPrice": (
                "currentPrice",
                "current_price",
                "price",
                "discountPrice",
                "discount_price",
                "product_price",
            ),
            "referencePrice": (
                "referencePrice",
                "reference_price",
                "originalPrice",
            ),
            "rank": ("rank", "position", "product_rank"),
        }
        for target_key, source_keys in mappings.items():
            if target.get(target_key) not in (None, ""):
                continue
            for source_key in source_keys:
                value = source.get(source_key)
                if value not in (None, ""):
                    target[target_key] = value
                    break

    @classmethod
    def _preview_action_product_sources(
        cls,
        payload: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        product_keys = (
            "products",
            "relatedProducts",
            "affectedProducts",
            "productList",
            "items",
            "results",
        )
        risk_keys = (
            "risk_decisions",
            "riskDecisions",
            "risk_decision",
            "riskDecision",
            "inventory_risk_decisions",
            "inventoryRiskDecisions",
        )
        container_keys = product_keys + risk_keys
        sources: list[Dict[str, Any]] = []

        def collect(value: Any, include_nested: bool = False) -> None:
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        if include_nested:
                            collect(item, include_nested=True)
                        elif cls._preview_product_id(item):
                            sources.append(item)
                return
            if not isinstance(value, dict):
                return
            if cls._preview_product_id(value):
                sources.append(value)
            for key in product_keys:
                if key in value:
                    collect(value[key])
            for key in risk_keys:
                if key in value:
                    collect(value[key], include_nested=True)
            if include_nested:
                for key, child in value.items():
                    if key not in container_keys:
                        collect(child, include_nested=True)

        for key in product_keys:
            if key in payload:
                collect(payload[key])
        for key in risk_keys:
            if key in payload:
                collect(payload[key], include_nested=True)
        return sources

    @staticmethod
    def _preview_json_object(value: Any) -> Dict[str, Any]:
        try:
            payload = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _preview_snapshot_source(cls, value: Any, branch_name: str) -> Dict[str, Any]:
        payload = cls._preview_json_object(value)
        preferred_keys = (
            ("before", "beforeSnapshot")
            if branch_name == "before"
            else ("after", "afterSnapshot")
        )
        for key in preferred_keys:
            branch = payload.get(key)
            if isinstance(branch, dict):
                return branch
        if any(
            key in payload
            for key in ("before", "after", "beforeSnapshot", "afterSnapshot")
        ):
            return {}
        return payload

    def _is_displayable_governance_action(self, action: TopQueryActionPlan) -> bool:
        if action.execution_mode != "human_approval":
            return False
        payload = self._load_action_payload(action)
        action_type = str(action.action_type or "")
        check_key = str(payload.get("check_key") or action.check_key or "")
        if action_type in {"kg_tag", "kg_relation"}:
            params = payload.get("params")
            if not isinstance(params, dict):
                kg_action = payload.get("kg_action") or payload.get("action")
                if isinstance(kg_action, dict):
                    params = kg_action.get("params")
            return isinstance(params, dict) and bool(params)
        if action_type == "query_demote" and check_key == "inventory":
            query = str(payload.get("query") or action.query_text or "").strip()
            if uses_authoritative_query_demotion(payload):
                add_ids, remove_ids = self._query_demote_action_deltas(payload)
                return bool(query) and bool(add_ids or remove_ids)
            return bool(query) and bool(self._payload_product_ids(payload))
        if action_type == "tag_bucket_config" and check_key == "open_factor":
            brand = str(payload.get("brand") or action.brand or "").strip()
            try:
                tag_bucket_payload = self._tag_bucket_payload(payload, str(action.query_text or ""))
            except ValueError:
                return False
            configs = tag_bucket_payload.get("configs") or []
            return bool(brand) and all(
                isinstance(item, dict)
                and bool(str(item.get("tags") or "").strip())
                and bool(str(item.get("afterBucket") or "").strip())
                for item in configs
            )
        return False

    def _load_action_payload(self, action: TopQueryActionPlan) -> Dict[str, Any]:
        try:
            return json.loads(action.request_payload or "{}")
        except json.JSONDecodeError as exc:
            raise ActionValidationError(f"invalid action payload: {action.id}") from exc

    def _execute_real_action(self, action: TopQueryActionPlan, payload: Dict[str, Any]) -> Dict[str, Any]:
        brand = str(payload.get("brand") or action.brand or "")
        query = str(payload.get("query") or action.query_text or "").strip()
        if not brand:
            raise ActionValidationError("brand is required for real execution")
        assert_query_governance_allowed(brand, query)
        if action.action_type == "product_blacklist":
            product_ids = self._payload_product_ids(payload)
            if not product_ids:
                raise ActionValidationError("product_blacklist requires product_id/product_ids")
            try:
                success = self.operational_service.add_to_blacklist(
                    brand,
                    ";".join(product_ids),
                    [query] if query else [],
                    external_request_id=str(action.external_request_id or "") or None,
                )
            except Exception as exc:
                raise ActionExecutionError(f"product_blacklist execution failed: {exc}") from exc
            if not success:
                raise ActionExecutionError("product_blacklist execution failed")
            try:
                self._record_product_action_logs(action, payload, brand, query, product_ids, action_type=1)
            except Exception as exc:
                raise ActionExecutionError(
                    f"product_blacklist local audit failed after external write: {exc}",
                    {"write_succeeded": True, "execution_unknown": True},
                ) from exc
            return {"real_action": "product_blacklist", "product_ids": product_ids}
        if action.action_type == "query_demote":
            product_ids = self._payload_product_ids(payload)
            if not query:
                raise ActionValidationError("query_demote requires query")
            if not product_ids:
                raise ActionValidationError("query_demote requires product_id/product_ids/skus")
            try:
                success = self.operational_service.sync_broken_code_to_redis(
                    brand,
                    add_items={query: product_ids},
                    external_request_id=str(action.external_request_id or "") or None,
                )
            except Exception as exc:
                raise ActionExecutionError(f"query_demote execution failed: {exc}") from exc
            if not success:
                raise ActionExecutionError("query_demote execution failed")
            try:
                self._record_product_action_logs(action, payload, brand, query, product_ids, action_type=2)
                if (payload.get("check_key") or action.check_key) == "inventory":
                    self._record_inventory_recovery_tasks(brand, query, product_ids, payload)
            except Exception as exc:
                raise ActionExecutionError(
                    f"query_demote local audit failed after external write: {exc}",
                    {"write_succeeded": True, "execution_unknown": True},
                ) from exc
            return {"real_action": "query_demote", "query": query, "product_ids": product_ids}
        if action.action_type == "tag_bucket_config":
            tag_bucket_payload = self._tag_bucket_payload(payload, query)
            try:
                before_snapshot = self._tag_bucket_verification_snapshot(
                    brand,
                    query,
                    tag_bucket_payload,
                    phase="pre-write",
                )
            except ActionExecutionError:
                raise
            except Exception as exc:
                raise ActionExecutionError(
                    f"tag_bucket_config pre-write verification failed: {exc}",
                    {"request_payload": tag_bucket_payload, "write_succeeded": False},
                ) from exc
            try:
                response = self.operational_service.apply_tag_bucket_config(
                    brand,
                    tag_bucket_payload,
                    external_request_id=str(action.external_request_id or "") or None,
                )
            except Exception as exc:
                raise ActionExecutionError(
                    f"tag_bucket_config execution failed: {exc}",
                    {"request_payload": tag_bucket_payload, "write_succeeded": False},
                ) from exc
            if not self._tool_response_success(response):
                raise ActionExecutionError(
                    f"tag_bucket_config execution failed: {response}",
                    {"request_payload": tag_bucket_payload, "tool_response": response},
                )
            decision = payload.get("world_knowledge_decision") if isinstance(payload.get("world_knowledge_decision"), dict) else {}
            if not decision:
                decision = {
                    "decision": "execute",
                    "selected_tags": self._selected_tags_from_tag_bucket_payload(tag_bucket_payload),
                    "config_plan": tag_bucket_payload.get("configs") or [],
                }
            action.before_snapshot = json.dumps(before_snapshot, ensure_ascii=False) # type: ignore
            verification_started_at = perf_counter()
            try:
                after_snapshot = self._tag_bucket_verification_snapshot(
                    brand,
                    query,
                    tag_bucket_payload,
                    phase="post-write",
                )
                verification_payload = build_world_knowledge_verification_summary({
                    "world_knowledge_decision": decision,
                    "products": (before_snapshot.get("search") or {}).get("products") or [],
                    "tag_bucket_after_snapshot": after_snapshot,
                }, (after_snapshot.get("search") or {}).get("products") or [])
                verification = {
                    **verification_payload["world_knowledge_verification"],
                    "action_id": int(action.id),
                }
                verification_duration_seconds = round(
                    max(perf_counter() - verification_started_at, 0.0),
                    6,
                )
            except Exception as exc:
                verification_duration_seconds = round(
                    max(perf_counter() - verification_started_at, 0.0),
                    6,
                )
                safe_error = sanitize_action_error(exc)
                failed_after_snapshot = sanitize_action_error_details({
                    "verification_failed": True,
                    "error": safe_error,
                })
                action.after_snapshot = json.dumps(failed_after_snapshot, ensure_ascii=False) # type: ignore
                raise ActionExecutionError(
                    f"tag_bucket_config post-write verification failed: {safe_error}",
                    {
                        "real_action": "tag_bucket_config",
                        "request_payload": tag_bucket_payload,
                        "tool_response": response,
                        "before_snapshot": before_snapshot,
                        "after_snapshot": failed_after_snapshot,
                        "write_succeeded": True,
                        "verification_failed": True,
                        "verification_duration_seconds": verification_duration_seconds,
                        "world_knowledge_verification": {
                            "action_id": int(action.id),
                            "selected_tags": decision.get("selected_tags") or [],
                            "config_verified": False,
                            "result_effect": "pending",
                            "verification_failed": True,
                        },
                    },
                ) from exc
            action.after_snapshot = json.dumps({
                **after_snapshot,
                "world_knowledge_verification": verification,
            }, ensure_ascii=False) # type: ignore
            execution_result = {
                "real_action": "tag_bucket_config",
                "write_succeeded": True,
                "request_payload": tag_bucket_payload,
                "tool_response": response,
                "before_snapshot": before_snapshot,
                "after_snapshot": after_snapshot,
                "world_knowledge_verification": verification,
                "verification_duration_seconds": verification_duration_seconds,
            }
            if verification.get("config_verified") is False:
                raise ActionExecutionError(
                    "tag_bucket_config config readback mismatch",
                    execution_result,
                )
            return execution_result
        if action.action_type in {"kg_tag", "kg_relation"}:
            kg_action = self._kg_action_payload(action, payload, brand)
            try:
                snapshot = self.kg_client.snapshot(kg_action)
                response = self.kg_client.execute(
                    kg_action,
                    external_request_id=str(action.external_request_id or "") or None,
                )
            except Exception as exc:
                raise ActionExecutionError(f"{action.action_type} execution failed: {exc}") from exc
            if not self._tool_response_success(response):
                raise ActionExecutionError(
                    f"{action.action_type} execution failed: {response}",
                    {"kg_action": kg_action, "before_snapshot": snapshot, "tool_response": response},
                )
            try:
                rollback = self.kg_client.build_rollback(kg_action, snapshot, response)
            except Exception as exc:
                raise ActionExecutionError(
                    f"{action.action_type} rollback plan failed: {exc}",
                    {
                        "kg_action": kg_action,
                        "before_snapshot": snapshot,
                        "tool_response": response,
                        "write_succeeded": True,
                        "execution_unknown": True,
                    },
                ) from exc
            return {
                "real_action": action.action_type,
                "kg_action": kg_action,
                "before_snapshot": snapshot,
                "tool_response": response,
                "rollback_payload": rollback,
            }
        raise ActionValidationError(f"real execution is not supported for action_type: {action.action_type}")

    def _tag_bucket_payload(self, payload: Dict[str, Any], query: str) -> Dict[str, Any]:
        keyword = str(payload.get("keyword") or payload.get("query") or query or "").strip()
        filters = payload.get("filters")
        configs = payload.get("configs")
        if not keyword:
            raise ActionValidationError("tag_bucket_config requires keyword/query")
        if not isinstance(filters, list):
            raise ActionValidationError("tag_bucket_config requires filters list")
        if not isinstance(configs, list) or not configs:
            raise ActionValidationError("tag_bucket_config requires non-empty configs")
        return {"keyword": keyword, "filters": filters, "configs": configs}

    def _tag_bucket_verification_snapshot(
        self,
        brand: str,
        query: str,
        tag_bucket_payload: Dict[str, Any],
        *,
        phase: str = "verification",
    ) -> Dict[str, Any]:
        config = self.operational_service.get_tag_bucket_config(
            brand,
            str(tag_bucket_payload.get("keyword") or query),
            tag_bucket_payload.get("filters") or [],
        )
        if not isinstance(config, dict) or str(config.get("status") or "") not in {
            "available",
            "not_found",
        }:
            raise ActionExecutionError(
                f"tag_bucket_config {phase} config evidence unavailable",
                {
                    "write_succeeded": phase == "post-write",
                    "error_reason": "config_evidence_unavailable",
                },
            )
        search_all_with_total = getattr(self.operational_service, "search_all_products_with_total", None)
        if callable(getattr(type(self.operational_service), "search_all_products_with_total", None)):
            search_with_total = search_all_with_total
        else:
            search_with_total = getattr(self.operational_service, "search_products_with_total", None)
        if not callable(search_with_total):
            raise ActionExecutionError(
                f"tag_bucket_config {phase} search evidence unavailable: structured_search_not_supported",
                {
                    "write_succeeded": phase == "post-write",
                    "error_reason": "structured_search_not_supported",
                },
                retryable=False,
            )
        search = search_with_total(brand, query, page_size=16)
        error_reason = None
        if not isinstance(search, dict):
            error_reason = "invalid_search_structure"
        else:
            products = search.get("products")
            total_count = search.get("total_count")
            if search.get("success") is not True:
                error_reason = str(search.get("error_reason") or "search_failed")
            elif not isinstance(products, list):
                error_reason = "invalid_products"
            elif any(not isinstance(product, dict) for product in products):
                error_reason = "invalid_product_item"
            elif (
                isinstance(total_count, bool)
                or not isinstance(total_count, int)
                or total_count < 0
                or total_count != len(products)
            ):
                error_reason = "invalid_total_count"
            elif search.get("result_complete") is not True:
                error_reason = str(search.get("error_reason") or "result_incomplete")
        if error_reason is not None:
            deterministic_errors = {
                "duplicate_search_page",
                "duplicate_search_product",
                "empty_page_before_total",
                "invalid_product_item",
                "invalid_products",
                "invalid_search_response",
                "invalid_search_structure",
                "invalid_total_count",
                "result_count_exceeded_total",
                "structured_search_not_supported",
                "total_changed_between_pages",
            }
            raise ActionExecutionError(
                f"tag_bucket_config {phase} search evidence unavailable: {error_reason}",
                {
                    "write_succeeded": phase == "post-write",
                    "error_reason": error_reason,
                },
                retryable=error_reason not in deterministic_errors,
            )
        return {
            "config": config,
            "search": search,
        }

    @staticmethod
    def _selected_tags_from_tag_bucket_payload(payload: Dict[str, Any]) -> list[str]:
        selected_tags: list[str] = []
        for config in payload.get("configs") or []:
            if not isinstance(config, dict):
                continue
            for tag in str(config.get("tags") or "").split(","):
                normalized = tag.strip()
                if normalized and normalized not in selected_tags:
                    selected_tags.append(normalized)
        return selected_tags

    def _persist_tag_bucket_verification(
        self,
        action: TopQueryActionPlan,
        result: Dict[str, Any],
        db: Optional[Any] = None,
    ) -> None:
        verification = result.get("world_knowledge_verification")
        if not isinstance(verification, dict):
            return
        verification = {**verification, "action_id": int(action.id)}
        try:
            verification_duration = max(float(result.get("verification_duration_seconds") or 0.0), 0.0)
        except (TypeError, ValueError):
            verification_duration = 0.0
        verified = verification.get("config_verified") is True and verification.get("result_effect") == "improved"
        owns_session = db is None
        db = db or SessionLocal()
        try:
            existing_events = db.query(TopQueryAgentEvent).filter(
                TopQueryAgentEvent.task_id == int(action.task_id),
                TopQueryAgentEvent.event_type == "action_verified",
            ).all()
            event = next(
                (
                    item for item in existing_events
                    if _json_payload_action_id(item.output_payload) == int(action.id)
                ),
                None,
            )
            existing_steps = db.query(TopQueryStepStatus).filter(
                TopQueryStepStatus.task_id == int(action.task_id),
                TopQueryStepStatus.check_key == "open_factor",
            ).all()
            step = next(
                (
                    item for item in existing_steps
                    if _json_payload_action_id(item.evidence, nested_key="world_knowledge_verification") == int(action.id)
                ),
                None,
            )
            if event is None:
                event = TopQueryAgentEvent(
                    task_id=int(action.task_id),
                    brand=action.brand,
                    query_text=action.query_text,
                    agent_name="VerificationExecutor",
                    stage="verification",
                    event_type="action_verified",
                    summary="标签权重动作已回查配置并复核搜索结果",
                    output_payload=_bounded_tag_bucket_payload(verification),
                    duration=verification_duration,
                    token_usage=0,
                )
                db.add(event)
            else:
                event.output_payload = _bounded_tag_bucket_payload(verification) # type: ignore
                event.summary = "标签权重动作已回查配置并复核搜索结果" # type: ignore
                event.duration = verification_duration # type: ignore
                event.token_usage = 0 # type: ignore
            if step is None:
                step = self._new_tag_bucket_verification_step(action, result, verification, verified)
                db.add(step)
            else:
                step.status = "fixed" if verified else "failed" # type: ignore
                step.display_status = "fixed" if verified else "still_problematic" # type: ignore
                step.summary = "世界知识标签权重治理验证通过" if verified else "世界知识标签权重治理未完成有效性验证" # type: ignore
                step.evidence = _bounded_tag_bucket_payload({"world_knowledge_verification": verification}) # type: ignore
                step.before_snapshot = _bounded_tag_bucket_payload(result.get("before_snapshot") or {}) # type: ignore
                step.after_snapshot = _bounded_tag_bucket_payload({
                    **(result.get("after_snapshot") or {}),
                    "world_knowledge_verification": verification,
                }) # type: ignore
            if owns_session:
                db.commit()
        except Exception:
            if owns_session:
                db.rollback()
            raise
        finally:
            if owns_session:
                db.close()

    @staticmethod
    def _new_tag_bucket_verification_step(
        action: TopQueryActionPlan,
        result: Dict[str, Any],
        verification: Dict[str, Any],
        verified: bool,
    ) -> TopQueryStepStatus:
        return TopQueryStepStatus(
            task_id=int(action.task_id),
            brand=action.brand,
            query_text=action.query_text,
            check_key="open_factor",
            status="fixed" if verified else "failed",
            display_status="fixed" if verified else "still_problematic",
            summary="世界知识标签权重治理验证通过" if verified else "世界知识标签权重治理未完成有效性验证",
            evidence=_bounded_tag_bucket_payload({"world_knowledge_verification": verification}),
            before_snapshot=_bounded_tag_bucket_payload(result.get("before_snapshot") or {}),
            after_snapshot=_bounded_tag_bucket_payload({
                **(result.get("after_snapshot") or {}),
                "world_knowledge_verification": verification,
            }),
        )

    def _kg_action_payload(self, action: TopQueryActionPlan, payload: Dict[str, Any], brand: str) -> Dict[str, Any]:
        kg_payload = payload.get("kg_action") or payload.get("action") or payload
        action_type = kg_payload.get("actionType") or kg_payload.get("action_type")
        if not action_type:
            if action.action_type == "kg_tag":
                action_type = payload.get("kgActionType") or "add_tag"
            elif action.action_type == "kg_relation":
                action_type = payload.get("kgActionType") or "add_relationship"
        params = kg_payload.get("params") or payload.get("params")
        if not isinstance(params, dict) or not params:
            raise ActionValidationError(f"{action.action_type} requires params")
        return {"brand": brand, "actionType": action_type, "params": params}

    def _tool_response_success(self, response: Any) -> bool:
        if response is True:
            return True
        if isinstance(response, dict):
            return response.get("code") in {0, 1, 200} or response.get("message") in {"success", "dry_run_success"}
        if isinstance(response, str):
            return response.strip().lower().startswith("ok")
        return False

    def _payload_product_ids(self, payload: Dict[str, Any]) -> list[str]:
        raw_value = payload.get("product_ids") or payload.get("productIds") or payload.get("skus") or payload.get("sku_list")
        if raw_value is None:
            raw_value = payload.get("product_id") or payload.get("productId") or payload.get("sku")
        if raw_value is None:
            return []
        if isinstance(raw_value, str):
            separators = [";", ","]
            values = [raw_value]
            for separator in separators:
                values = [part for value in values for part in value.split(separator)]
            return [value.strip() for value in values if value.strip()]
        if isinstance(raw_value, list):
            return [str(value).strip() for value in raw_value if str(value).strip()]
        return [str(raw_value).strip()] if str(raw_value).strip() else []

    def _record_product_action_logs(
        self,
        action: TopQueryActionPlan,
        payload: Dict[str, Any],
        brand: str,
        query: str,
        product_ids: list[str],
        action_type: int,
    ) -> None:
        product_names = payload.get("product_names") if isinstance(payload.get("product_names"), dict) else {}
        product_images = payload.get("product_images") if isinstance(payload.get("product_images"), dict) else payload.get("image_urls") if isinstance(payload.get("image_urls"), dict) else {}
        if action_type == 1:
            default_reason = "Agent Loop 商品拉黑"
        elif (payload.get("check_key") or action.check_key) == "inventory":
            broken_rate_percent = payload.get("broken_rate_percent")
            default_reason = (
                f"Agent Loop 断码降桶: {broken_rate_percent}%"
                if broken_rate_percent is not None
                else "Agent Loop 断码降桶"
            )
        else:
            default_reason = "Agent Loop 分桶调整"
        reason = str(payload.get("reason") or action.reason or default_reason)
        for product_id in product_ids:
            save_action_log({
                "brand": brand,
                "query": query,
                "spu": product_id,
                "name": str(product_names.get(product_id) or ""),
                "image_url": str(product_images.get(product_id) or "") if isinstance(product_images, dict) else "",
                "type": action_type,
                "reason": reason,
            })

    def _record_inventory_recovery_tasks(self, brand: str, query: str, product_ids: list[str], payload: Dict[str, Any]) -> None:
        product_names = payload.get("product_names") if isinstance(payload.get("product_names"), dict) else {}
        product_images = payload.get("product_images") if isinstance(payload.get("product_images"), dict) else payload.get("image_urls") if isinstance(payload.get("image_urls"), dict) else {}
        broken_rate_by_product = (
            payload.get("broken_rate_percent_by_product")
            if isinstance(payload.get("broken_rate_percent_by_product"), dict)
            else {}
        )
        default_broken_rate_percent = payload.get("broken_rate_percent")
        for product_id in product_ids:
            product_name = str(product_names.get(product_id) or "")
            broken_rate_percent = broken_rate_by_product.get(product_id, default_broken_rate_percent)
            try:
                latest_broken_rate_percent = int(round(float(broken_rate_percent)))
            except (TypeError, ValueError):
                latest_broken_rate_percent = 0
            save_inventory_task({
                "brand": brand,
                "spu": product_id,
                "name": product_name,
                "image_url": str(product_images.get(product_id) or "") if isinstance(product_images, dict) else "",
                "query": query,
                "stock": latest_broken_rate_percent,
            })

    def _record_query_demote_group_local_audit(
        self,
        actions: list[TopQueryActionPlan],
        payloads: list[Dict[str, Any]],
        action_deltas: list[tuple[list[str], list[str]]],
        *,
        brand: str,
        query: str,
        before_ids: list[str],
        desired_ids: list[str],
    ) -> None:
        overall_removed_ids = {
            product_id
            for _, action_remove_ids in action_deltas
            for product_id in action_remove_ids
        }
        actual_removed_ids = set(before_ids) - set(desired_ids)
        audited_removed_ids: set[str] = set()
        for action, payload, (action_add_ids, action_remove_ids) in zip(
            actions,
            payloads,
            action_deltas,
        ):
            logged_ids = sorted(set(action_add_ids) - overall_removed_ids)
            if logged_ids:
                self._record_product_action_logs(
                    action,
                    payload,
                    brand,
                    query,
                    logged_ids,
                    action_type=2,
                )
                if (payload.get("check_key") or action.check_key) == "inventory":
                    self._record_inventory_recovery_tasks(
                        brand,
                        query,
                        logged_ids,
                        payload,
                    )
            product_names = (
                payload.get("product_names")
                if isinstance(payload.get("product_names"), dict)
                else {}
            )
            product_images = (
                payload.get("product_images")
                if isinstance(payload.get("product_images"), dict)
                else payload.get("image_urls")
                if isinstance(payload.get("image_urls"), dict)
                else {}
            )
            recovery_reason = str(
                payload.get("reason")
                or action.reason
                or "Agent Loop 分桶恢复"
            )
            for product_id in action_remove_ids:
                if (
                    product_id not in actual_removed_ids
                    or product_id in audited_removed_ids
                ):
                    continue
                save_action_log({
                    "brand": brand,
                    "query": query,
                    "spu": product_id,
                    "name": str(product_names.get(product_id) or ""),
                    "image_url": str(product_images.get(product_id) or "") if isinstance(product_images, dict) else "",
                    "type": 3,
                    "reason": recovery_reason,
                })
                mark_inventory_task_recovered_by_details(
                    brand,
                    query,
                    product_id,
                    -1,
                )
                audited_removed_ids.add(product_id)

    @_task_execution_locked()
    def mark_task_processed(self, task_id: int, operator: str) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            task = db.query(TopQueryTask).filter(
                TopQueryTask.id == task_id,
            ).with_for_update().first()
            if not task:
                raise ValueError(f"top query task not found: {task_id}")
            assert_query_governance_allowed(str(task.brand or ""), str(task.query_text or ""))
            status = str(task.status or "").lower()
            if status == "completed":
                return {"task_id": task_id, "status": "completed"}
            if status != "manual_required":
                raise ActionStateConflictError(
                    f"task cannot be marked processed from status: {status or 'unknown'}"
                )
            try:
                raw_payload = json.loads(task.raw_payload or "{}")
            except (TypeError, json.JSONDecodeError):
                raw_payload = {}
            if not isinstance(raw_payload, dict):
                raw_payload = {}
            processed_at = datetime.datetime.now()
            raw_payload["manual_processing"] = {
                "operator": operator,
                "processedAt": processed_at.isoformat(),
            }
            updated = db.query(TopQueryTask).filter(
                TopQueryTask.id == task_id,
                TopQueryTask.status == "manual_required",
            ).update({
                TopQueryTask.status: "completed",
                TopQueryTask.display_status: "fixed",
                TopQueryTask.completed_at: processed_at,
                TopQueryTask.raw_payload: json.dumps(raw_payload, ensure_ascii=False),
            }, synchronize_session=False)
            if updated != 1:
                raise ActionStateConflictError("task processing state changed")
            db.commit()
            return {"task_id": task_id, "status": "completed"}
        finally:
            db.close()
