"""Deterministic node state transitions for the Query governance rewrite."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from sqlalchemy import select

from .models import NODE_ORDER, NodeStatus


class InvalidNodeTransition(ValueError):
    """Raised when a node is moved through an unsupported state transition."""


class NodeStatePersistenceError(RuntimeError):
    """Raised when a node cannot be persisted against an existing Query task."""


@dataclass(frozen=True)
class NodeIdentity:
    """Stable identity of one node execution within a governance batch."""

    batch_id: str
    brand: str
    query: str
    node: str

    def __post_init__(self) -> None:
        for field_name in ("batch_id", "brand", "query", "node"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required")
        if self.node not in NODE_ORDER:
            raise ValueError(f"unknown governance node: {self.node}")


@dataclass(frozen=True)
class NodeStateRecord:
    """Persisted state and evidence for one batch/brand/Query/node tuple."""

    identity: NodeIdentity
    status: NodeStatus = NodeStatus.PENDING
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def batch_id(self) -> str:
        return self.identity.batch_id

    @property
    def brand(self) -> str:
        return self.identity.brand

    @property
    def query(self) -> str:
        return self.identity.query

    @property
    def node(self) -> str:
        return self.identity.node

    @property
    def terminal(self) -> bool:
        return self.status in {
            NodeStatus.EXECUTED,
            NodeStatus.SKIPPED,
            NodeStatus.ERROR,
        }


class NodeStateStore(Protocol):
    """Persistence port implemented by the database adapter in the DB task."""

    def get(self, identity: NodeIdentity) -> NodeStateRecord | None:
        ...

    def save(self, record: NodeStateRecord) -> NodeStateRecord:
        ...


class InMemoryNodeStateStore:
    """Small deterministic store used by unit tests and local composition."""

    def __init__(self) -> None:
        self._records: dict[NodeIdentity, NodeStateRecord] = {}

    def get(self, identity: NodeIdentity) -> NodeStateRecord | None:
        record = self._records.get(identity)
        return replace(record) if record is not None else None

    def save(self, record: NodeStateRecord) -> NodeStateRecord:
        self._records[record.identity] = record
        return replace(record)


class SqlAlchemyNodeStateStore:
    """Persist node records in the rewrite's task and step tables.

    The database task owns the concrete ORM models. They are injected here so
    this state service does not import or duplicate schema definitions.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        task_model: type[Any],
        step_model: type[Any],
        workflow_run_model: type[Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._task_model = task_model
        self._step_model = step_model
        self._workflow_run_model = workflow_run_model

    def save_model_config_version(self, batch_id: str, version: int) -> None:
        """Persist the task-start model configuration version once per batch."""
        if self._workflow_run_model is None:
            return
        with self._session_factory() as session:
            run = session.execute(
                select(self._workflow_run_model).where(
                    self._workflow_run_model.batch_id == batch_id
                )
            ).scalar_one_or_none()
            if run is None:
                run = self._workflow_run_model(
                    batch_id=batch_id,
                    status="running",
                    model_config_version=version,
                )
                session.add(run)
            elif run.model_config_version is None:
                run.model_config_version = version
            elif run.model_config_version != version:
                raise NodeStatePersistenceError(
                    "model configuration version is already pinned for this batch"
                )
            session.commit()

    def get(self, identity: NodeIdentity) -> NodeStateRecord | None:
        with self._session_factory() as session:
            task = self._find_task(session, identity)
            if task is None:
                return None
            step = session.execute(
                select(self._step_model).where(
                    self._step_model.task_id == task.id,
                    self._step_model.node_key == identity.node,
                )
            ).scalar_one_or_none()
            return self._from_row(identity, step) if step is not None else None

    def save(self, record: NodeStateRecord) -> NodeStateRecord:
        with self._session_factory() as session:
            task = self._find_task(session, record.identity)
            if task is None:
                raise NodeStatePersistenceError(
                    "cannot persist node without an existing Query governance task"
                )
            step = session.execute(
                select(self._step_model).where(
                    self._step_model.task_id == task.id,
                    self._step_model.node_key == record.node,
                )
            ).scalar_one_or_none()
            if step is None:
                step = self._step_model(
                    task_id=task.id,
                    node_key=record.node,
                )
                session.add(step)
            self._write_row(step, record)
            session.commit()
            return self._from_row(record.identity, step)

    def _find_task(self, session: Any, identity: NodeIdentity) -> Any:
        return session.execute(
            select(self._task_model).where(
                self._task_model.batch_id == identity.batch_id,
                self._task_model.brand == identity.brand,
                self._task_model.query_text == identity.query,
            )
        ).scalar_one_or_none()

    @staticmethod
    def _write_row(row: Any, record: NodeStateRecord) -> None:
        row.status = record.status.value
        row.result_json = (
            json.dumps(record.result, ensure_ascii=False, separators=(",", ":"))
            if record.result is not None
            else None
        )
        row.error_code = record.error_code
        row.error_message = record.error_message
        row.duration_seconds = record.duration_seconds
        row.started_at = record.started_at
        row.completed_at = record.completed_at

    @staticmethod
    def _from_row(identity: NodeIdentity, row: Any) -> NodeStateRecord:
        try:
            status = NodeStatus(row.status)
        except ValueError as exc:
            raise NodeStatePersistenceError(
                f"unknown persisted node status: {row.status}"
            ) from exc
        result = (
            json.loads(row.result_json) if row.result_json is not None else None
        )
        return NodeStateRecord(
            identity=identity,
            status=status,
            result=result,
            error_code=row.error_code,
            error_message=row.error_message,
            started_at=row.started_at,
            completed_at=row.completed_at,
            duration_seconds=row.duration_seconds,
            created_at=getattr(row, "created_at", None),
            updated_at=getattr(row, "updated_at", None),
        )


class DeterministicNodeStateService:
    """Own legal status transitions and persist every transition immediately."""

    _ALLOWED_TRANSITIONS = {
        NodeStatus.PENDING: frozenset(
            {NodeStatus.RUNNING, NodeStatus.SKIPPED, NodeStatus.ERROR}
        ),
        NodeStatus.RUNNING: frozenset(
            {NodeStatus.EXECUTED, NodeStatus.SKIPPED, NodeStatus.ERROR}
        ),
        NodeStatus.EXECUTED: frozenset(),
        NodeStatus.SKIPPED: frozenset(),
        NodeStatus.ERROR: frozenset(),
    }

    def __init__(self, store: NodeStateStore) -> None:
        self.store = store

    def ensure_pending(self, identity: NodeIdentity) -> NodeStateRecord:
        existing = self.store.get(identity)
        if existing is not None:
            return existing
        now = self._now()
        return self.store.save(
            NodeStateRecord(
                identity=identity,
                created_at=now,
                updated_at=now,
            )
        )

    def get(self, identity: NodeIdentity) -> NodeStateRecord | None:
        return self.store.get(identity)

    def start(self, identity: NodeIdentity) -> NodeStateRecord:
        current = self.ensure_pending(identity)
        return self._save(self._transition(current, NodeStatus.RUNNING))

    def complete(
        self, identity: NodeIdentity, result: Any = None
    ) -> NodeStateRecord:
        current = self._require(identity)
        transitioned = self._transition(current, NodeStatus.EXECUTED)
        return self._save(
            replace(transitioned, result=result, error_code=None, error_message=None)
        )

    def skip(self, identity: NodeIdentity, reason: str) -> NodeStateRecord:
        if not str(reason).strip():
            raise ValueError("skip reason is required")
        current = self._require(identity)
        transitioned = self._transition(current, NodeStatus.SKIPPED)
        return self._save(
            replace(
                transitioned,
                result={"reason": reason},
                error_code=None,
                error_message=None,
            )
        )

    def fail(
        self,
        identity: NodeIdentity,
        error_code: str,
        error_message: str,
    ) -> NodeStateRecord:
        if not str(error_code).strip():
            raise ValueError("error code is required")
        if not str(error_message).strip():
            raise ValueError("error message is required")
        current = self._require(identity)
        transitioned = self._transition(current, NodeStatus.ERROR)
        return self._save(
            replace(
                transitioned,
                result=None,
                error_code=error_code,
                error_message=error_message,
            )
        )

    def _require(self, identity: NodeIdentity) -> NodeStateRecord:
        current = self.store.get(identity)
        if current is None:
            raise InvalidNodeTransition(
                f"node {identity.node} has not been created"
            )
        return current

    def _transition(
        self, current: NodeStateRecord, target: NodeStatus
    ) -> NodeStateRecord:
        allowed = self._ALLOWED_TRANSITIONS[current.status]
        if target not in allowed:
            raise InvalidNodeTransition(
                f"cannot move {current.node} from {current.status.value} to {target.value}"
            )
        now = self._now()
        started_at = current.started_at
        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if target is NodeStatus.RUNNING:
            started_at = now
        elif target in {
            NodeStatus.EXECUTED,
            NodeStatus.SKIPPED,
            NodeStatus.ERROR,
        } and started_at is None:
            # A gate can finish without a model or external call. Keep a
            # complete timing record for that path as well.
            started_at = now
        completed_at = now if target in {
            NodeStatus.EXECUTED,
            NodeStatus.SKIPPED,
            NodeStatus.ERROR,
        } else None
        duration_seconds = None
        if started_at is not None and completed_at is not None:
            duration_seconds = max(
                0.0, (completed_at - started_at).total_seconds()
            )
        return replace(
            current,
            status=target,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            updated_at=now,
        )

    def _save(self, record: NodeStateRecord) -> NodeStateRecord:
        return self.store.save(record)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
