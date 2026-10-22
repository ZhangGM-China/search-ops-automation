from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.llm.node_config import QUERY_GOVERNANCE_NODE_KEYS, capture_snapshot
from src.persistence.models import Base
from src.query_governance.async_tasks import (
    AsyncTaskStatus,
    QueryGovernanceAsyncTaskRepository,
    QueryGovernanceAsyncTaskService,
)
from src.query_governance.checkpoint import SqlAlchemyCheckpointStore
from src.query_governance.models import NodeResult, NodeStatus, QuerySpec
from src.query_governance.workflow import QueryGovernanceWorkflow


MODEL_CONFIG_SNAPSHOT = capture_snapshot(
    node_keys=QUERY_GOVERNANCE_NODE_KEYS
)


class Search:
    def mixed(self, brand: str, query: str) -> list[dict]:
        return [{"product_id": f"{brand}-{query}-1", "rank": 1}]


class Store:
    def get_s0(self, brand: str, query: str):
        return None

    def save_s0_if_absent(self, brand: str, query: str, snapshot: dict):
        return snapshot

    def save_s0_failure(self, *_args):
        return None

    def save_step(self, *_args):
        return None


class ActionGateway:
    def execute_and_verify(self, spec, action, context):
        return NodeResult.executed({"action": action})


class RecordingNode:
    def __init__(self, name: str, events: list[str]):
        self.name = name
        self.events = events

    def run(self, spec: QuerySpec, context: dict) -> NodeResult:
        self.events.append(f"{self.name}:{spec.query}")
        return NodeResult.executed({"node": self.name})


class AsyncSubmittingRecall(RecordingNode):
    def __init__(
        self,
        events: list[str],
        expected_status: AsyncTaskStatus,
    ) -> None:
        super().__init__("recall", events)
        self.expected_status = expected_status
        self.async_task_id: int | None = None

    def run(self, spec: QuerySpec, context: dict) -> NodeResult:
        self.events.append(f"recall:{spec.query}")
        service = context["async_tasks"]
        submission = service.enqueue_for_query(
            batch_id=context["batch_id"],
            brand=spec.brand,
            query_text=spec.query,
            trigger_source="recall_anomaly",
            evidence={"result_count": 0},
            idempotency_key=f"kg:{context['batch_id']}:{spec.brand}:{spec.query}:recall",
        )
        self.async_task_id = submission.task_id
        if self.expected_status is AsyncTaskStatus.PLAN_GENERATED:
            service.mark_processing(submission.task_id)
            submission = service.record_plan(
                submission.task_id,
                {"operation": "add_relation"},
            )
        elif self.expected_status is AsyncTaskStatus.ERROR:
            submission = service.record_error(
                submission.task_id,
                "KG_AGENT_FAILED",
                "KG Agent failed",
            )
        assert submission.status is self.expected_status
        return NodeResult.executed({"async_task_id": submission.task_id})


@pytest.mark.parametrize(
    "async_status",
    [
        AsyncTaskStatus.PENDING,
        AsyncTaskStatus.PLAN_GENERATED,
        AsyncTaskStatus.ERROR,
    ],
)
def test_async_kg_task_state_never_blocks_later_mainline_nodes(async_status):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    events: list[str] = []
    recall = AsyncSubmittingRecall(events, async_status)
    async_tasks = QueryGovernanceAsyncTaskService(
        QueryGovernanceAsyncTaskRepository(factory)
    )
    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=Store(),
        runners={
            "recall": recall,
            "reorder": RecordingNode("reorder", events),
            "relevance": RecordingNode("relevance", events),
        },
        action_gateway=ActionGateway(),
        checkpoint=SqlAlchemyCheckpointStore(factory),
        async_tasks=async_tasks,
        model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
    )

    result = workflow.run_batch(
        [QuerySpec("q1", "atlas", "dress")],
        batch_id="batch-1",
        data_date=date(2026, 8, 29),
    )["q1"]

    assert events == ["reorder:dress", "relevance:dress", "recall:dress"]
    assert result.nodes["reorder"].status is NodeStatus.EXECUTED
    assert result.nodes["relevance"].status is NodeStatus.EXECUTED
    stored = async_tasks.get(recall.async_task_id)
    assert stored.status is async_status
    if async_status is AsyncTaskStatus.ERROR:
        assert stored.error_code == "KG_AGENT_FAILED"
        assert stored.error_message == "KG Agent failed"
    engine.dispose()


def test_async_task_completion_does_not_reenter_a_completed_mainline():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    events: list[str] = []
    recall = AsyncSubmittingRecall(events, AsyncTaskStatus.PENDING)
    async_tasks = QueryGovernanceAsyncTaskService(
        QueryGovernanceAsyncTaskRepository(factory)
    )
    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=Store(),
        runners={
            "recall": recall,
            "reorder": RecordingNode("reorder", events),
            "relevance": RecordingNode("relevance", events),
        },
        action_gateway=ActionGateway(),
        checkpoint=SqlAlchemyCheckpointStore(factory),
        async_tasks=async_tasks,
        model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
    )
    spec = QuerySpec("q1", "atlas", "dress")

    workflow.run_batch(
        [spec], batch_id="batch-1", data_date=date(2026, 8, 29)
    )
    async_tasks.mark_processing(recall.async_task_id)
    async_tasks.record_plan(recall.async_task_id, {"operation": "add_relation"})
    events.clear()

    workflow.run_batch(
        [spec], batch_id="batch-1", data_date=date(2026, 8, 29)
    )

    assert events == []
    assert async_tasks.get(recall.async_task_id).status is AsyncTaskStatus.PLAN_GENERATED
    engine.dispose()


def test_async_submission_without_parent_query_is_reported_without_raising():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    async_tasks = QueryGovernanceAsyncTaskService(
        QueryGovernanceAsyncTaskRepository(factory)
    )

    result = async_tasks.enqueue_for_query(
        batch_id="missing-batch",
        brand="atlas",
        query_text="dress",
        trigger_source="recall_anomaly",
        evidence={"result_count": 0},
        idempotency_key="kg:missing-batch:atlas:dress:recall",
    )

    assert result.status is AsyncTaskStatus.ERROR
    assert result.error_code == "ASYNC_PARENT_TASK_NOT_FOUND"
    engine.dispose()


def test_async_parent_lookup_failure_is_reported_without_raising():
    class BrokenRepository:
        def find_parent_task_id(self, **_kwargs):
            raise RuntimeError("database unavailable")

    service = QueryGovernanceAsyncTaskService(BrokenRepository())

    result = service.enqueue_for_query(
        batch_id="batch-1",
        brand="atlas",
        query_text="dress",
        trigger_source="recall_anomaly",
        evidence={},
        idempotency_key="kg:batch-1:atlas:dress:recall",
    )

    assert result.status is AsyncTaskStatus.ERROR
    assert result.error_code == "ASYNC_TASK_LOOKUP_FAILED"
    assert "database unavailable" in result.error_message
