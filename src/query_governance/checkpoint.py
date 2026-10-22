"""Durable Checkpoint support for the deterministic Query workflow."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from sqlalchemy import select, update

from .models import (
    NODE_ORDER,
    NodeResult,
    NodeStatus,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
    QuerySpec,
)


class CheckpointError(RuntimeError):
    """Base error for invalid new-architecture checkpoint state."""


class CheckpointConflictError(CheckpointError):
    """Raised when a writer uses a stale checkpoint version."""


class LegacyCheckpointError(CheckpointError):
    """Raised when a run is not backed by new Query/node state."""


@dataclass(frozen=True)
class BatchCheckpoint:
    batch_id: str
    status: str
    current_node: str | None
    version: int
    last_completed: Mapping[str, str] | None = None
    active: Mapping[str, str] | None = None
    resumed_after_yield: bool = False


@dataclass(frozen=True)
class InFlightAction:
    spec: QuerySpec
    node: str
    action: Any


@dataclass(frozen=True)
class _BatchS0Store:
    owner: "SqlAlchemyCheckpointStore"
    batch_id: str

    def get_s0(self, brand: str, query: str) -> Mapping[str, Any] | None:
        return self.owner._get_s0(self.batch_id, brand, query)

    def save_s0_if_absent(
        self, brand: str, query: str, snapshot: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self.owner._save_s0_if_absent(
            self.batch_id, brand, query, snapshot
        )

    def save_s0_failure(
        self,
        brand: str,
        query: str,
        error_code: str,
        error_message: str,
    ) -> None:
        self.owner._save_s0_failure(
            self.batch_id, brand, query, error_code, error_message
        )

    def save_step(
        self, brand: str, query: str, node: str, result: NodeResult
    ) -> None:
        raise CheckpointError(
            "checkpointed workflows persist nodes through record_node"
        )


_TERMINAL_VALUES = frozenset(
    {NodeStatus.EXECUTED.value, NodeStatus.SKIPPED.value, NodeStatus.ERROR.value}
)

# These failures are produced by transient upstream/model responses.  A
# checkpointed run must retain successful nodes, while allowing a later
# scheduler/recovery invocation to reopen only the failed node.
_RETRYABLE_NODE_ERRORS = frozenset(
    {
        "MIXED_FAILED",
        "RELEVANCE_OUTPUT_INVALID",
        "RELEVANCE_MODEL_TIMEOUT",
        "RELEVANCE_MODEL_FAILED",
        "RELEVANCE_BATCH_FAILED",
        "RELEVANCE_SEARCH_FAILED",
    }
)

# Recovery resumes a run only after this heartbeat gap. The original
# ``started_at`` belongs to the abandoned attempt; keeping it would make the
# dashboard count the outage window as processing time.
_RECOVERY_STALE_AFTER = dt.timedelta(minutes=30)


class SqlAlchemyCheckpointStore:
    """Persist the batch cursor and Query/node state atomically."""

    def __init__(
        self,
        session_factory: Any,
        *,
        task_model: type[Any] = QueryGovernanceTask,
        step_model: type[Any] = QueryGovernanceStepStatus,
        workflow_model: type[Any] = QueryGovernanceWorkflowRun,
    ) -> None:
        self._session_factory = session_factory
        self._task_model = task_model
        self._step_model = step_model
        self._workflow_model = workflow_model
        self._active_batch_id: str | None = None

    def start_batch(
        self,
        batch_id: str,
        specs: Iterable[QuerySpec],
        *,
        data_date: dt.date | None = None,
    ) -> BatchCheckpoint:
        items = list(specs)
        resumed_after_yield = False
        if not str(batch_id).strip():
            raise ValueError("batch_id is required")
        if not items:
            raise ValueError("at least one Query is required")
        if len({(item.brand, item.query) for item in items}) != len(items):
            raise ValueError("duplicate brand/query in one batch")

        with self._session_factory() as session:
            run = self._find_run(session, batch_id)
            existing_tasks = self._tasks(session, batch_id)
            if run is not None and not existing_tasks:
                raise LegacyCheckpointError(
                    "workflow run has no new-architecture Query state"
                )
            if run is None:
                payload = self._new_payload(batch_id, items)
                run = self._workflow_model(
                    batch_id=batch_id,
                    status="running",
                    current_node=None,
                    checkpoint_payload=self._dump(payload),
                    checkpoint_version=1,
                    heartbeat_at=self._now(),
                )
                session.add(run)
                session.flush()
                for rank, spec in enumerate(items, start=1):
                    task = self._find_task_values(session, batch_id, spec.brand, spec.query)
                    if task is None:
                        task = self._task_model(
                            batch_id=batch_id,
                            brand=spec.brand,
                            data_date=data_date or dt.date.today(),
                            query_text=spec.query,
                            query_rank=rank,
                            status="pending",
                            experiment_id=(
                                int(spec.experiment_id)
                                if spec.experiment_id is not None
                                else None
                            ),
                            group_type=spec.group_type,
                        )
                        session.add(task)
                        session.flush()
                    elif task.query_rank is None:
                        task.query_rank = rank
                    existing_steps = {
                        step.node_key
                        for step in session.execute(
                            select(self._step_model).where(
                                self._step_model.task_id == task.id
                            )
                        ).scalars()
                    }
                    for node in NODE_ORDER:
                        if node not in existing_steps:
                            session.add(
                                self._step_model(
                                    task_id=task.id,
                                    node_key=node,
                                    status=NodeStatus.PENDING.value,
                                )
                            )
                session.commit()
            else:
                payload = self._decode_payload(run.checkpoint_payload)
                resumed_after_yield = bool(payload.get("invocation_yielded"))
                self._assert_query_order(existing_tasks, items, payload)
                restarted = self._restart_stale_attempt_clock(run)
                if not resumed_after_yield:
                    self._reset_queries_after_s0_failure(session, run, batch_id)
                    self._reset_retryable_node_errors(session, run, batch_id)
                if restarted:
                    session.commit()
                if payload.get("invocation_yielded"):
                    payload["invocation_yielded"] = False
                    self._cas_run(session, run, run.checkpoint_version,
                                  current_node=run.current_node, status=run.status, payload=payload)
                    session.commit()
            self._active_batch_id = batch_id
            return replace(self._checkpoint(session, run), resumed_after_yield=resumed_after_yield)

    def save_model_config_version(self, batch_id: str, version: int) -> None:
        """Pin the model configuration version for an existing batch."""
        if version is None:
            return
        with self._session_factory() as session:
            run = self._require_run(session, batch_id)
            if run.model_config_version is None:
                run.model_config_version = int(version)
            elif run.model_config_version != int(version):
                raise CheckpointError(
                    "model configuration version is already pinned for this batch"
                )
            session.commit()

    def get_checkpoint(self, batch_id: str) -> BatchCheckpoint:
        with self._session_factory() as session:
            run = self._find_run(session, batch_id)
            if run is None:
                raise CheckpointError(f"checkpoint batch not found: {batch_id}")
            return self._checkpoint(session, run)

    def begin_node(
        self,
        batch_id: str,
        spec: QuerySpec,
        node: str,
        expected_version: int,
    ) -> BatchCheckpoint:
        self._validate_node(node)
        with self._session_factory() as session:
            run = self._require_run(session, batch_id)
            task = self._require_task(session, batch_id, spec)
            step = self._require_step(session, task.id, node)
            retryable_s0_failure = (
                node != "s0"
                and task.s0_snapshot is not None
                and self._is_s0_failed_skip(step)
            )
            if step.status in _TERMINAL_VALUES and not (
                (
                    node == "s0"
                    and step.status == NodeStatus.ERROR.value
                    and task.s0_snapshot is None
                )
                or retryable_s0_failure
            ):
                raise CheckpointConflictError(
                    f"cannot start terminal node {spec.query}/{node}"
                )
            now = self._now()
            step.status = NodeStatus.RUNNING.value
            step.started_at = now
            step.completed_at = None
            step.duration_seconds = None
            step.result_json = None
            step.error_code = None
            step.error_message = None
            task.started_at = task.started_at or now
            task.status = "running"
            payload = self._decode_payload(run.checkpoint_payload)
            payload["active"] = self._identity_payload(spec, node)
            payload["active_action"] = None
            updated = self._cas_run(
                session,
                run,
                expected_version,
                current_node=node,
                status="running",
                payload=payload,
            )
            session.commit()
            return updated

    def save_action(
        self,
        batch_id: str,
        spec: QuerySpec,
        node: str,
        action: Any,
        expected_version: int,
    ) -> BatchCheckpoint:
        self._validate_node(node)
        with self._session_factory() as session:
            run = self._require_run(session, batch_id)
            task = self._require_task(session, batch_id, spec)
            step = self._require_step(session, task.id, node)
            if step.status != NodeStatus.RUNNING.value:
                raise CheckpointConflictError(
                    f"cannot save action for non-running node {spec.query}/{node}"
                )
            step.result_json = self._dump({"result": None, "action": action})
            payload = self._decode_payload(run.checkpoint_payload)
            active = self._identity_payload(spec, node)
            payload["active"] = active
            action_metadata: dict[str, Any] = {}
            if isinstance(action, Mapping):
                action_type = action.get("type")
                if action_type is not None:
                    action_metadata["type"] = str(action_type)
                product_ids = action.get("product_ids")
                if isinstance(product_ids, (list, tuple, set)):
                    action_metadata["product_count"] = len(product_ids)
                elif action.get("product_id") is not None:
                    action_metadata["product_count"] = 1
            payload["active_action"] = action_metadata
            updated = self._cas_run(
                session,
                run,
                expected_version,
                current_node=node,
                status="running",
                payload=payload,
            )
            session.commit()
            return updated

    def record_node(
        self,
        batch_id: str,
        spec: QuerySpec,
        node: str,
        result: NodeResult,
        expected_version: int,
    ) -> BatchCheckpoint:
        self._validate_node(node)
        if not result.terminal:
            raise ValueError("checkpoint can only persist a terminal node result")
        with self._session_factory() as session:
            run = self._require_run(session, batch_id)
            task = self._require_task(session, batch_id, spec)
            step = self._require_step(session, task.id, node)
            if step.status in _TERMINAL_VALUES:
                existing = self._result(step)
                if existing.status != result.status:
                    raise CheckpointConflictError(
                        f"terminal node already recorded: {spec.query}/{node}"
                    )
                return self._checkpoint(session, run)

            now = self._now()
            started_at = self._aware(step.started_at or now)
            payload = self._decode_payload(run.checkpoint_payload)
            step.status = result.status.value
            step.result_json = self._dump(
                {
                    "result": result.result,
                    "action": result.action,
                    "telemetry": dict(result.telemetry or {}),
                }
            )
            step.error_code = result.error_code
            step.error_message = result.error_message
            step.completed_at = now
            step.duration_seconds = max(0.0, (now - started_at).total_seconds())
            if self._task_will_be_terminal(session, task.id, node):
                task.status = (
                    "error"
                    if self._task_has_error(session, task.id)
                    else "completed"
                )
                task_error = self._task_error(session, task.id)
                task.error_code = task_error[0] if task.status == "error" else None
                task.error_message = task_error[1] if task.status == "error" else None
                task.completed_at = now
            payload["last_completed"] = self._identity_payload(spec, node)
            payload["active"] = None
            payload["active_action"] = None
            updated = self._cas_run(
                session,
                run,
                expected_version,
                current_node=node,
                status="running",
                payload=payload,
            )
            session.commit()
            return updated

    def yield_batch(self, batch_id: str, expected_version: int) -> BatchCheckpoint:
        with self._session_factory() as session:
            run = self._require_run(session, batch_id)
            payload = self._decode_payload(run.checkpoint_payload)
            payload["invocation_yielded"] = True
            updated = self._cas_run(session, run, expected_version,
                                    current_node=run.current_node, status="running", payload=payload)
            session.commit()
            return updated

    def finish_batch(
        self, batch_id: str, expected_version: int
    ) -> BatchCheckpoint:
        with self._session_factory() as session:
            run = self._require_run(session, batch_id)
            if run.status in {"completed", "error"}:
                return self._checkpoint(session, run)
            if not self._batch_is_terminal(session, batch_id):
                raise CheckpointError("cannot finish batch with unfinished nodes")
            payload = self._decode_payload(run.checkpoint_payload)
            payload["active"] = None
            payload["active_action"] = None
            payload["invocation_yielded"] = False
            status = "error" if self._batch_fully_failed(session, batch_id) else "completed"
            updated = self._cas_run(
                session,
                run,
                expected_version,
                current_node=None,
                status=status,
                finished_at=self._now(),
                payload=payload,
            )
            session.commit()
            return updated

    def get_node(
        self, batch_id: str, spec: QuerySpec, node: str
    ) -> NodeResult | None:
        self._validate_node(node)
        with self._session_factory() as session:
            task = self._find_task(session, batch_id, spec)
            if task is None:
                return None
            step = self._find_step(session, task.id, node)
            return self._result(step) if step is not None else None

    def get_nodes(
        self, batch_id: str, specs: Iterable[QuerySpec], node: str
    ) -> dict[tuple[str, str], NodeResult]:
        """Load the persisted state for a batch stage in one database query.

        Continuation slices inspect every Query before deciding which nodes to
        start.  Reading each task and step in its own session turns that
        inspection into hundreds of serial round trips on MySQL, so resume
        loads the stage rows together and keeps the existing per-Query API for
        callers that still need it.
        """
        self._validate_node(node)
        identities = {(spec.brand, spec.query) for spec in specs}
        if not identities:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    self._task_model.brand,
                    self._task_model.query_text,
                    self._step_model,
                )
                .join(
                    self._step_model,
                    self._step_model.task_id == self._task_model.id,
                )
                .where(
                    self._task_model.batch_id == batch_id,
                    self._step_model.node_key == node,
                )
            ).all()
            return {
                (str(brand), str(query)): self._result(step)
                for brand, query, step in rows
                if (str(brand), str(query)) in identities
            }

    def get_inflight_action(
        self, batch_id: str, spec: QuerySpec, node: str
    ) -> InFlightAction | None:
        with self._session_factory() as session:
            task = self._find_task(session, batch_id, spec)
            if task is None:
                return None
            step = self._find_step(session, task.id, node)
            if step is None or step.status != NodeStatus.RUNNING.value:
                return None
            action = self._decode_result(step.result_json).get("action")
            return (
                InFlightAction(spec=spec, node=node, action=action)
                if action is not None
                else None
            )

    def all_nodes_terminal(self, batch_id: str) -> bool:
        with self._session_factory() as session:
            return self._batch_is_terminal(session, batch_id)

    def s0_store(self, batch_id: str) -> _BatchS0Store:
        return _BatchS0Store(self, batch_id)

    # Compatibility helpers for direct local callers. New workflow code uses
    # s0_store(batch_id) so concurrent workflows cannot share this scope.
    def get_s0(self, brand: str, query: str) -> Mapping[str, Any] | None:
        if self._active_batch_id is None:
            raise CheckpointError("no active batch")
        return self._get_s0(self._active_batch_id, brand, query)

    def save_s0_if_absent(
        self, brand: str, query: str, snapshot: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if self._active_batch_id is None:
            raise CheckpointError("no active batch")
        return self._save_s0_if_absent(
            self._active_batch_id, brand, query, snapshot
        )

    def save_s0_failure(
        self, brand: str, query: str, error_code: str, error_message: str
    ) -> None:
        if self._active_batch_id is None:
            return
        self._save_s0_failure(
            self._active_batch_id, brand, query, error_code, error_message
        )

    def _get_s0(
        self, batch_id: str, brand: str, query: str
    ) -> Mapping[str, Any] | None:
        with self._session_factory() as session:
            task = self._find_task_values(session, batch_id, brand, query)
            if task is None or task.s0_snapshot is None:
                return None
            return json.loads(task.s0_snapshot)

    def _save_s0_if_absent(
        self,
        batch_id: str,
        brand: str,
        query: str,
        snapshot: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        with self._session_factory() as session:
            task = self._find_task_values(session, batch_id, brand, query)
            if task is None:
                raise CheckpointError("cannot save S0 before starting a batch")
            if task.s0_snapshot is None:
                task.s0_snapshot = self._dump(snapshot)
                session.commit()
                return dict(snapshot)
            return json.loads(task.s0_snapshot)

    def _save_s0_failure(
        self,
        batch_id: str,
        brand: str,
        query: str,
        error_code: str,
        error_message: str,
    ) -> None:
        with self._session_factory() as session:
            task = self._find_task_values(session, batch_id, brand, query)
            if task is None:
                return
            task.error_code = error_code
            task.error_message = error_message
            session.commit()

    def _checkpoint(self, session: Any, run: Any) -> BatchCheckpoint:
        payload = self._decode_payload(run.checkpoint_payload)
        return BatchCheckpoint(
            batch_id=str(run.batch_id),
            status=str(run.status),
            current_node=run.current_node,
            version=int(run.checkpoint_version),
            last_completed=payload.get("last_completed"),
            active=payload.get("active"),
        )

    def _cas_run(
        self,
        session: Any,
        run: Any,
        expected_version: int,
        *,
        current_node: str | None,
        status: str,
        payload: Mapping[str, Any],
        finished_at: dt.datetime | None = None,
    ) -> BatchCheckpoint:
        new_version = expected_version + 1
        values: dict[str, Any] = {
            "current_node": current_node,
            "checkpoint_payload": self._dump(payload),
            "checkpoint_version": new_version,
            "status": status,
            "heartbeat_at": self._now(),
            "updated_at": self._now(),
        }
        if finished_at is not None:
            values["finished_at"] = finished_at
        result = session.execute(
            update(self._workflow_model)
            .where(
                self._workflow_model.id == run.id,
                self._workflow_model.checkpoint_version == expected_version,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise CheckpointConflictError(
                f"checkpoint version conflict for batch {run.batch_id}"
            )
        run.current_node = current_node
        run.checkpoint_payload = values["checkpoint_payload"]
        run.checkpoint_version = new_version
        run.status = status
        run.heartbeat_at = values["heartbeat_at"]
        if finished_at is not None:
            run.finished_at = finished_at
        return self._checkpoint(session, run)

    def _tasks(self, session: Any, batch_id: str) -> list[Any]:
        return list(
            session.execute(
                select(self._task_model)
                .where(self._task_model.batch_id == batch_id)
                .order_by(self._task_model.query_rank, self._task_model.id)
            ).scalars()
        )

    def _assert_query_order(
        self,
        tasks: list[Any],
        specs: list[QuerySpec],
        payload: Mapping[str, Any],
    ) -> None:
        saved = [(str(task.brand), str(task.query_text)) for task in tasks]
        current = [(spec.brand, spec.query) for spec in specs]
        if saved != current:
            raise CheckpointError("batch Query order does not match checkpoint")
        ordered = [
            (item.get("brand"), item.get("query"))
            for item in payload.get("query_order", [])
        ]
        if ordered != current:
            raise CheckpointError("checkpoint Query order does not match input")

    @staticmethod
    def _new_payload(batch_id: str, specs: list[QuerySpec]) -> dict[str, Any]:
        return {
            "schema": "query_governance_checkpoint_v1",
            "batch_id": batch_id,
            "query_order": [
                {
                    "query_id": spec.query_id,
                    "brand": spec.brand,
                    "query": spec.query,
                }
                for spec in specs
            ],
            "last_completed": None,
            "active": None,
            "active_action": None,
        }

    @staticmethod
    def _identity_payload(spec: QuerySpec, node: str) -> dict[str, str]:
        return {
            "query_id": spec.query_id,
            "brand": spec.brand,
            "query": spec.query,
            "node": node,
        }

    @staticmethod
    def _decode_payload(payload: str | None) -> dict[str, Any]:
        if not payload:
            raise LegacyCheckpointError(
                "checkpoint payload is missing or belongs to the legacy Agent Loop"
            )
        try:
            decoded = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LegacyCheckpointError("checkpoint payload is not valid JSON") from exc
        if not isinstance(decoded, dict) or decoded.get("schema") != "query_governance_checkpoint_v1":
            raise LegacyCheckpointError(
                "legacy Agent Loop checkpoint cannot be resumed"
            )
        return decoded

    def _find_run(self, session: Any, batch_id: str) -> Any:
        return session.execute(
            select(self._workflow_model).where(
                self._workflow_model.batch_id == batch_id
            )
        ).scalar_one_or_none()

    def _require_run(self, session: Any, batch_id: str) -> Any:
        run = self._find_run(session, batch_id)
        if run is None:
            raise CheckpointError(f"checkpoint batch not found: {batch_id}")
        return run

    def _find_task(self, session: Any, batch_id: str, spec: QuerySpec) -> Any:
        return self._find_task_values(session, batch_id, spec.brand, spec.query)

    def _find_task_values(
        self, session: Any, batch_id: str | None, brand: str, query: str
    ) -> Any:
        statement = select(self._task_model).where(
            self._task_model.brand == brand,
            self._task_model.query_text == query,
        )
        if batch_id is not None:
            statement = statement.where(self._task_model.batch_id == batch_id)
        return session.execute(
            statement.order_by(self._task_model.id.desc())
        ).scalars().first()

    def _require_task(self, session: Any, batch_id: str, spec: QuerySpec) -> Any:
        task = self._find_task(session, batch_id, spec)
        if task is None:
            raise CheckpointError(f"Query task not found: {batch_id}/{spec.query}")
        return task

    def _find_step(self, session: Any, task_id: int, node: str) -> Any:
        return session.execute(
            select(self._step_model).where(
                self._step_model.task_id == task_id,
                self._step_model.node_key == node,
            )
        ).scalar_one_or_none()

    def _require_step(self, session: Any, task_id: int, node: str) -> Any:
        step = self._find_step(session, task_id, node)
        if step is None:
            raise CheckpointError(f"node step not found: {task_id}/{node}")
        return step

    def _task_will_be_terminal(
        self, session: Any, task_id: int, current_node: str
    ) -> bool:
        rows = session.execute(
            select(self._step_model.node_key, self._step_model.status).where(
                self._step_model.task_id == task_id
            )
        ).all()
        return all(
            node == current_node or status in _TERMINAL_VALUES
            for node, status in rows
        )

    def _task_has_error(self, session: Any, task_id: int) -> bool:
        return self._task_error(session, task_id) != (None, None)

    def _task_error(
        self, session: Any, task_id: int
    ) -> tuple[str | None, str | None]:
        row = session.execute(
            select(self._step_model.error_code, self._step_model.error_message)
            .where(
                self._step_model.task_id == task_id,
                self._step_model.status == NodeStatus.ERROR.value,
            )
            .order_by(self._step_model.id.asc())
            .limit(1)
        ).first()
        return (row[0], row[1]) if row is not None else (None, None)

    def _reset_queries_after_s0_failure(
        self, session: Any, run: Any, batch_id: str
    ) -> None:
        """Reopen downstream nodes when an S0 failure is retried.

        A failed run is terminal for observability, but a subsequent scheduler
        invocation must be able to retry S0 and execute the remaining nodes
        instead of treating the prior failure's synthetic skips as complete.
        """
        changed = False
        tasks = session.execute(
            select(self._task_model).where(self._task_model.batch_id == batch_id)
        ).scalars().all()
        for task in tasks:
            s0 = self._find_step(session, task.id, "s0")
            if s0 is None or s0.status != NodeStatus.ERROR.value or task.s0_snapshot is not None:
                continue
            steps = session.execute(
                select(self._step_model).where(self._step_model.task_id == task.id)
            ).scalars().all()
            for step in steps:
                if step.node_key == "s0":
                    continue
                step.status = NodeStatus.PENDING.value
                step.result_json = None
                step.error_code = None
                step.error_message = None
                step.duration_seconds = None
                step.started_at = None
                step.completed_at = None
            task.status = "pending"
            task.error_code = None
            task.error_message = None
            task.completed_at = None
            changed = True
        if changed:
            run.status = "running"
            run.finished_at = None
            run.current_node = None
            run.heartbeat_at = self._now()
            session.commit()

    def _reset_retryable_node_errors(
        self, session: Any, run: Any, batch_id: str
    ) -> None:
        """Reopen transient terminal node errors on a later batch invocation.

        Successful and non-retryable terminal nodes remain immutable.  Only
        the failed node is reset, so recovery does not repeat completed S0,
        recall, reorder, or relevance work.
        """
        changed = False
        tasks = session.execute(
            select(self._task_model).where(self._task_model.batch_id == batch_id)
        ).scalars().all()
        for task in tasks:
            steps = session.execute(
                select(self._step_model).where(self._step_model.task_id == task.id)
            ).scalars().all()
            reopened = False
            for step in steps:
                if (
                    step.status != NodeStatus.ERROR.value
                    or step.error_code not in _RETRYABLE_NODE_ERRORS
                ):
                    continue
                # S0 failures without a snapshot are already handled by the
                # dedicated reset above; this guard keeps the two paths clear.
                if step.node_key == "s0" and task.s0_snapshot is not None:
                    continue
                step.status = NodeStatus.PENDING.value
                step.result_json = None
                step.error_code = None
                step.error_message = None
                step.duration_seconds = None
                step.started_at = None
                step.completed_at = None
                reopened = True
            if reopened:
                task.status = "pending"
                task.error_code = None
                task.error_message = None
                task.completed_at = None
                changed = True
        if changed:
            run.status = "running"
            run.finished_at = None
            run.current_node = None
            run.heartbeat_at = self._now()
            session.commit()

    def _restart_stale_attempt_clock(self, run: Any) -> bool:
        """Start a fresh wall-clock interval when resuming an abandoned run.

        Completed runs and healthy in-flight runs retain their original
        timestamp. A stale running run gets a new attempt start so a worker
        outage is not reported as execution time after recovery.
        """

        if str(run.status) != "running":
            return False
        now = self._now()
        heartbeat = run.heartbeat_at
        if heartbeat is None:
            stale = (
                run.started_at is None
                or now - self._aware(run.started_at) >= _RECOVERY_STALE_AFTER
            )
        else:
            stale = now - self._aware(heartbeat) >= _RECOVERY_STALE_AFTER
        if stale:
            run.started_at = now.replace(tzinfo=None)
            return True
        return False

    def _batch_is_terminal(self, session: Any, batch_id: str) -> bool:
        statuses = session.execute(
            select(self._step_model.status)
            .join(self._task_model, self._step_model.task_id == self._task_model.id)
            .where(self._task_model.batch_id == batch_id)
        ).scalars().all()
        return bool(statuses) and all(
            status in _TERMINAL_VALUES for status in statuses
        )

    def _batch_fully_failed(self, session: Any, batch_id: str) -> bool:
        statuses = session.execute(
            select(self._step_model.status)
            .join(self._task_model, self._step_model.task_id == self._task_model.id)
            .where(self._task_model.batch_id == batch_id)
        ).scalars().all()
        return bool(statuses) and NodeStatus.ERROR.value in statuses and NodeStatus.EXECUTED.value not in statuses

    @staticmethod
    def _is_s0_failed_skip(step: Any) -> bool:
        if step.status != NodeStatus.SKIPPED.value or not step.result_json:
            return False
        try:
            payload = json.loads(step.result_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        result = payload.get("result") if isinstance(payload, dict) else None
        return isinstance(result, dict) and result.get("reason") == "s0_failed"

    @classmethod
    def _result(cls, step: Any) -> NodeResult:
        payload = cls._decode_result(step.result_json)
        return NodeResult(
            status=NodeStatus(step.status),
            result=payload.get("result"),
            action=payload.get("action"),
            telemetry=(
                dict(payload.get("telemetry"))
                if isinstance(payload.get("telemetry"), dict)
                else {}
            ),
            error_code=step.error_code,
            error_message=step.error_message,
        )

    @staticmethod
    def _decode_result(payload: str | None) -> dict[str, Any]:
        if not payload:
            return {"result": None, "action": None, "telemetry": {}}
        decoded = json.loads(payload)
        if isinstance(decoded, dict) and "result" in decoded:
            return decoded
        return {"result": decoded, "action": None}

    @staticmethod
    def _dump(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise CheckpointError("checkpoint value is not JSON serializable") from exc

    @staticmethod
    def _validate_node(node: str) -> None:
        if node not in NODE_ORDER:
            raise ValueError(f"unknown governance node: {node}")

    @staticmethod
    def _now() -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    @staticmethod
    def _aware(value: dt.datetime) -> dt.datetime:
        return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


__all__ = [
    "BatchCheckpoint",
    "CheckpointConflictError",
    "CheckpointError",
    "InFlightAction",
    "LegacyCheckpointError",
    "SqlAlchemyCheckpointStore",
]
