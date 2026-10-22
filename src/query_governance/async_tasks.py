"""Persistence boundary for asynchronous KG governance tasks.

The deterministic mainline only submits a small, durable task record.  A
separate worker may later claim the record and generate a plan; this module
does not know how to call the KG Agent and never waits for it.
"""

from __future__ import annotations

import datetime as _datetime
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .models import QueryGovernanceAsyncTask, QueryGovernanceTask


class AsyncTaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PLAN_GENERATED = "plan_generated"
    ERROR = "error"


@dataclass(frozen=True)
class AsyncTaskSubmission:
    """Result of a non-blocking task submission."""

    task_id: int | None
    status: AsyncTaskStatus
    created: bool = False
    plan: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class AsyncTaskRecord:
    id: int
    task_id: int
    batch_id: str
    brand: str
    query_text: str
    trigger_source: str
    idempotency_key: str
    status: AsyncTaskStatus
    evidence: Mapping[str, Any] | None
    langfuse_context: Mapping[str, Any] | None
    plan: Mapping[str, Any] | None
    error_code: str | None
    error_message: str | None
    created_at: _datetime.datetime | None
    updated_at: _datetime.datetime | None


class AsyncTaskRepository(Protocol):
    def find_parent_task_id(
        self, *, batch_id: str, brand: str, query_text: str
    ) -> int | None:
        ...

    def create_or_get(self, **kwargs: Any) -> tuple[AsyncTaskRecord, bool]:
        ...

    def get(self, task_id: int) -> AsyncTaskRecord | None:
        ...

    def update(self, task_id: int, **fields: Any) -> AsyncTaskRecord:
        ...

    def list_pending(self, *, limit: int = 100) -> list[AsyncTaskRecord]:
        ...


def _json_dump(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid async task JSON payload") from exc
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise ValueError("async task JSON payload must be an object")
    return parsed


class QueryGovernanceAsyncTaskRepository:
    """SQLAlchemy repository for the rewrite's async task table."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        model: type[Any] = QueryGovernanceAsyncTask,
    ) -> None:
        self._session_factory = session_factory
        self._model = model

    def create_or_get(self, **kwargs: Any) -> tuple[AsyncTaskRecord, bool]:
        required = (
            "task_id", "batch_id", "brand", "query_text", "trigger_source",
            "idempotency_key",
        )
        missing = [name for name in required if not str(kwargs.get(name) or "").strip()]
        if missing:
            raise ValueError("missing async task fields: " + ", ".join(missing))
        evidence = kwargs.get("evidence")
        langfuse_context = kwargs.get("langfuse_context")
        with self._session_factory() as session:
            existing = self._find_by_key(session, str(kwargs["idempotency_key"]))
            if existing is not None:
                self._assert_same_payload(existing, kwargs, evidence)
                return self._to_record(existing), False

            row = self._model(
                task_id=kwargs["task_id"],
                batch_id=kwargs["batch_id"],
                brand=kwargs["brand"],
                query_text=kwargs["query_text"],
                trigger_source=kwargs["trigger_source"],
                idempotency_key=kwargs["idempotency_key"],
                status=AsyncTaskStatus.PENDING.value,
                evidence_json=_json_dump(evidence),
                langfuse_context_json=_json_dump(langfuse_context),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self._find_by_key(session, str(kwargs["idempotency_key"]))
                if existing is None:
                    raise
                self._assert_same_payload(existing, kwargs, evidence)
                return self._to_record(existing), False
            session.refresh(row)
            return self._to_record(row), True

    def find_parent_task_id(
        self, *, batch_id: str, brand: str, query_text: str
    ) -> int | None:
        """Resolve the durable Query task that owns an async KG task."""
        with self._session_factory() as session:
            return session.execute(
                select(QueryGovernanceTask.id).where(
                    QueryGovernanceTask.batch_id == batch_id,
                    QueryGovernanceTask.brand == brand,
                    QueryGovernanceTask.query_text == query_text,
                )
            ).scalar_one_or_none()

    def get(self, task_id: int) -> AsyncTaskRecord | None:
        with self._session_factory() as session:
            row = session.get(self._model, task_id)
            return self._to_record(row) if row is not None else None

    def update(self, task_id: int, **fields: Any) -> AsyncTaskRecord:
        with self._session_factory() as session:
            row = session.get(self._model, task_id)
            if row is None:
                raise KeyError(f"async task not found: {task_id}")
            for key, value in fields.items():
                if key in {"evidence_json", "langfuse_context_json", "plan_json"}:
                    setattr(row, key, _json_dump(value))
                elif key == "status":
                    row.status = value.value if isinstance(value, AsyncTaskStatus) else str(value)
                elif hasattr(row, key):
                    setattr(row, key, value)
            session.commit()
            session.refresh(row)
            return self._to_record(row)

    def list_pending(self, *, limit: int = 100) -> list[AsyncTaskRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._session_factory() as session:
            rows = session.execute(
                select(self._model)
                .where(self._model.status == AsyncTaskStatus.PENDING.value)
                .order_by(self._model.created_at, self._model.id)
                .limit(limit)
            ).scalars().all()
            return [self._to_record(row) for row in rows]

    def _find_by_key(self, session: Any, key: str) -> Any:
        return session.execute(
            select(self._model).where(self._model.idempotency_key == key)
        ).scalar_one_or_none()

    @staticmethod
    def _assert_same_payload(row: Any, kwargs: Mapping[str, Any], evidence: Mapping[str, Any] | None) -> None:
        same = (
            row.task_id == kwargs["task_id"]
            and row.batch_id == kwargs["batch_id"]
            and row.brand == kwargs["brand"]
            and row.query_text == kwargs["query_text"]
            and row.trigger_source == kwargs["trigger_source"]
            and (_json_load(row.evidence_json) or {}) == (dict(evidence or {}))
            and (_json_load(row.langfuse_context_json) or {})
            == dict(kwargs.get("langfuse_context") or {})
        )
        if not same:
            raise ValueError("idempotency key collision")

    @staticmethod
    def _to_record(row: Any) -> AsyncTaskRecord:
        try:
            status = AsyncTaskStatus(row.status)
        except ValueError as exc:
            raise ValueError(f"unknown async task status: {row.status}") from exc
        return AsyncTaskRecord(
            id=int(row.id),
            task_id=int(row.task_id),
            batch_id=row.batch_id,
            brand=row.brand,
            query_text=row.query_text,
            trigger_source=row.trigger_source,
            idempotency_key=row.idempotency_key,
            status=status,
            evidence=_json_load(row.evidence_json),
            langfuse_context=_json_load(row.langfuse_context_json),
            plan=_json_load(row.plan_json),
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class QueryGovernanceAsyncTaskService:
    """Non-blocking submission and explicit task lifecycle updates."""

    def __init__(self, repository: AsyncTaskRepository) -> None:
        self.repository = repository

    def enqueue(
        self,
        *,
        task_id: int,
        batch_id: str,
        brand: str,
        query_text: str,
        trigger_source: str,
        evidence: Mapping[str, Any] | None,
        idempotency_key: str,
        langfuse_context: Mapping[str, Any] | None = None,
    ) -> AsyncTaskSubmission:
        try:
            record, created = self.repository.create_or_get(
                task_id=task_id,
                batch_id=batch_id,
                brand=brand,
                query_text=query_text,
                trigger_source=trigger_source,
                evidence=evidence,
                idempotency_key=idempotency_key,
                langfuse_context=langfuse_context,
            )
        except ValueError:
            raise
        except Exception as exc:
            return AsyncTaskSubmission(
                task_id=None,
                status=AsyncTaskStatus.ERROR,
                error_code="ASYNC_TASK_PERSIST_FAILED",
                error_message=str(exc).strip() or exc.__class__.__name__,
            )
        return self._submission(record, created=created)

    def enqueue_for_query(
        self,
        *,
        batch_id: str,
        brand: str,
        query_text: str,
        trigger_source: str,
        evidence: Mapping[str, Any] | None,
        idempotency_key: str,
        langfuse_context: Mapping[str, Any] | None = None,
    ) -> AsyncTaskSubmission:
        """Submit an async task for a Query without blocking the mainline.

        The parent Query task is resolved from the new workflow tables.  A
        missing parent or persistence failure is returned as an async error;
        callers can record that warning while continuing their own node.
        """
        try:
            parent_task_id = self.repository.find_parent_task_id(
                batch_id=batch_id, brand=brand, query_text=query_text
            )
        except Exception as exc:
            return AsyncTaskSubmission(
                task_id=None,
                status=AsyncTaskStatus.ERROR,
                error_code="ASYNC_TASK_LOOKUP_FAILED",
                error_message=str(exc).strip() or exc.__class__.__name__,
            )
        if parent_task_id is None:
            return AsyncTaskSubmission(
                task_id=None,
                status=AsyncTaskStatus.ERROR,
                error_code="ASYNC_PARENT_TASK_NOT_FOUND",
                error_message=(
                    f"query task not found: {batch_id}/{brand}/{query_text}"
                ),
            )
        return self.enqueue(
            task_id=parent_task_id,
            batch_id=batch_id,
            brand=brand,
            query_text=query_text,
            trigger_source=trigger_source,
            evidence=evidence,
            idempotency_key=idempotency_key,
            langfuse_context=langfuse_context,
        )

    def get(self, task_id: int) -> AsyncTaskRecord | None:
        return self.repository.get(task_id)

    def list_pending(self, *, limit: int = 100) -> list[AsyncTaskRecord]:
        return self.repository.list_pending(limit=limit)

    def mark_processing(self, task_id: int) -> AsyncTaskSubmission:
        record = self._require(task_id)
        self._require_status(record, {AsyncTaskStatus.PENDING})
        return self._submission(
            self.repository.update(task_id, status=AsyncTaskStatus.PROCESSING),
            created=False,
        )

    def record_plan(self, task_id: int, plan: Mapping[str, Any]) -> AsyncTaskSubmission:
        if not isinstance(plan, Mapping):
            raise ValueError("plan must be an object")
        record = self._require(task_id)
        self._require_status(record, {AsyncTaskStatus.PROCESSING})
        return self._submission(
            self.repository.update(task_id, status=AsyncTaskStatus.PLAN_GENERATED, plan_json=plan),
            created=False,
        )

    def record_error(self, task_id: int, error_code: str, error_message: str) -> AsyncTaskSubmission:
        if not str(error_code).strip() or not str(error_message).strip():
            raise ValueError("error code and message are required")
        record = self._require(task_id)
        self._require_status(record, {AsyncTaskStatus.PENDING, AsyncTaskStatus.PROCESSING})
        return self._submission(
            self.repository.update(
                task_id,
                status=AsyncTaskStatus.ERROR,
                error_code=error_code,
                error_message=error_message,
            ),
            created=False,
        )

    def _require(self, task_id: int) -> AsyncTaskRecord:
        record = self.repository.get(task_id)
        if record is None:
            raise KeyError(f"async task not found: {task_id}")
        return record

    @staticmethod
    def _require_status(record: AsyncTaskRecord, allowed: set[AsyncTaskStatus]) -> None:
        if record.status not in allowed:
            raise ValueError(f"invalid async task transition from {record.status.value}")

    @staticmethod
    def _submission(record: AsyncTaskRecord, *, created: bool) -> AsyncTaskSubmission:
        return AsyncTaskSubmission(
            task_id=record.id,
            status=record.status,
            created=created,
            plan=record.plan,
            error_code=record.error_code,
            error_message=record.error_message,
        )


__all__ = [
    "AsyncTaskRecord",
    "AsyncTaskRepository",
    "AsyncTaskStatus",
    "AsyncTaskSubmission",
    "QueryGovernanceAsyncTaskRepository",
    "QueryGovernanceAsyncTaskService",
]
