"""SQLAlchemy persistence for immutable S0 snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from sqlalchemy import select, update

from .models import NodeResult, QueryGovernanceStepStatus, QueryGovernanceTask


class WorkflowStatePersistenceError(RuntimeError):
    pass


class SqlAlchemyWorkflowStateStore:
    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        batch_id: str,
        task_model: type[Any] = QueryGovernanceTask,
        step_model: type[Any] = QueryGovernanceStepStatus,
    ) -> None:
        if not str(batch_id).strip():
            raise ValueError("batch_id is required")
        self._session_factory = session_factory
        self._batch_id = str(batch_id)
        self._task_model = task_model
        self._step_model = step_model

    def get_s0(
        self, brand: str, query: str
    ) -> Mapping[str, Any] | None:
        with self._session_factory() as session:
            task = self._require_task(session, brand, query)
            return self._decode_snapshot(task.s0_snapshot)

    def save_s0_if_absent(
        self,
        brand: str,
        query: str,
        snapshot: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = json.dumps(
            snapshot, ensure_ascii=False, separators=(",", ":")
        )
        with self._session_factory() as session:
            task = self._require_task(session, brand, query)
            if task.s0_snapshot is None:
                session.execute(
                    update(self._task_model)
                    .where(
                        self._task_model.id == task.id,
                        self._task_model.s0_snapshot.is_(None),
                    )
                    .values(
                        s0_snapshot=payload,
                        error_code=None,
                        error_message=None,
                        updated_at=self._now(),
                    )
                )
                session.commit()
            authoritative = session.execute(
                select(self._task_model).where(
                    self._task_model.id == task.id
                )
            ).scalar_one()
            stored = self._decode_snapshot(authoritative.s0_snapshot)
            if stored is None:
                raise WorkflowStatePersistenceError(
                    "S0 snapshot was not persisted"
                )
            return stored

    def save_s0_failure(
        self,
        brand: str,
        query: str,
        error_code: str,
        error_message: str,
    ) -> None:
        with self._session_factory() as session:
            task = self._require_task(session, brand, query)
            if task.s0_snapshot is None:
                task.error_code = error_code
                task.error_message = error_message
                task.updated_at = self._now()
                session.commit()

    def save_step(
        self,
        brand: str,
        query: str,
        node: str,
        result: NodeResult,
    ) -> None:
        with self._session_factory() as session:
            task = self._require_task(session, brand, query)
            step = session.execute(
                select(self._step_model).where(
                    self._step_model.task_id == task.id,
                    self._step_model.node_key == node,
                )
            ).scalar_one_or_none()
            if step is None:
                step = self._step_model(task_id=task.id, node_key=node)
                session.add(step)
            step.status = result.status.value
            step.result_json = (
                json.dumps(
                    {
                        "result": result.result,
                        "action": result.action,
                        "telemetry": dict(result.telemetry or {}),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if result.result is not None
                else None
            )
            step.error_code = result.error_code
            step.error_message = result.error_message
            step.completed_at = self._now() if result.terminal else None
            session.commit()

    def _require_task(self, session: Any, brand: str, query: str) -> Any:
        task = session.execute(
            select(self._task_model).where(
                self._task_model.batch_id == self._batch_id,
                self._task_model.brand == brand,
                self._task_model.query_text == query,
            )
        ).scalar_one_or_none()
        if task is None:
            identity = f"{self._batch_id}/{brand}/{query}"
            raise WorkflowStatePersistenceError(
                f"Query governance task not found: {identity}"
            )
        return task

    @staticmethod
    def _decode_snapshot(value: Any) -> Mapping[str, Any] | None:
        if value is None:
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise WorkflowStatePersistenceError(
                "Stored S0 snapshot is invalid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise WorkflowStatePersistenceError(
                "Stored S0 snapshot must be an object"
            )
        return decoded

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)


__all__ = ["SqlAlchemyWorkflowStateStore", "WorkflowStatePersistenceError"]
