from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.persistence.models import Base
from src.llm.node_config import capture_snapshot
from src.query_governance.action_execution import VerifiedActionGateway
from src.query_governance.checkpoint import SqlAlchemyCheckpointStore
from src.query_governance.models import (
    NodeResult,
    NodeStatus,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
    QuerySpec,
)
from src.query_governance.ports import ActionGateway, NodeRunner, SearchGateway
from src.query_governance.read_api import QueryGovernanceReadService
from src.query_governance.workflow import QueryGovernanceWorkflow
from src.query_governance.vector_reorder import (
    VectorReorderClient,
    VectorReorderConfig,
    VectorReorderNodeRunner,
)


@dataclass
class _Search(SearchGateway):
    calls: list[tuple[str, str]] = field(default_factory=list)

    def mixed(self, brand: str, query: str):
        self.calls.append((brand, query))
        return [{"product_id": f"{query}-1", "rank": 1, "ext": {"bucket": "productIdsByCode"}}]


class _Runner(NodeRunner):
    def __init__(self, node: str, events: list[str], results=None):
        self.node = node
        self.events = events
        self.results = results or {}

    def run(self, spec, context):
        self.events.append(f"{self.node}:{spec.query}")
        return self.results.get(spec.query, NodeResult.executed({"node": self.node}))


class _ActionGateway(ActionGateway):
    def __init__(self, result: NodeResult):
        self.result = result
        self.calls: list[tuple[str, object]] = []

    def execute_and_verify(self, spec, action, context):
        self.calls.append((spec.query_id, action))
        return self.result


class _BatchRelevanceRunner:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def run(self, inputs, context):
        del context
        self.calls.append(tuple(item.spec.query for item in inputs))
        results = {}
        for item in inputs:
            result = NodeResult.executed(
                {
                    "query": item.spec.query,
                    "product_count": len(item.products),
                    "requested_product_ids": [item.products[0]["product_id"]],
                    "detected_product_ids": [item.products[0]["product_id"]],
                    "reused_product_ids": [],
                    "relevant_product_ids": [item.products[0]["product_id"]],
                    "blacklist_candidates": [],
                    "warnings": [],
                    "telemetry": {
                        "batch_index": 1,
                        "batch_count": 1,
                        "product_count": len(item.products),
                        "image_download_success_count": 1,
                        "image_download_failure_count": 0,
                    },
                }
            )
            result.telemetry = {
                "roundSeconds": 0.2,
                "batchModelSeconds": 0.1,
                "batchQueryCount": len(inputs),
                "modelCallCount": 1,
                "retryCount": 0,
                "batchKey": "model:1",
            }
            results[item.spec.query] = result
        return results


class _BatchReorderRunner:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def run_batch(self, items):
        self.calls.append(tuple(spec.query for spec, _context in items))
        return {
            spec.query_id: NodeResult.executed(
                {"query": spec.query}, action={"type": "sort", "query": spec.query}
            )
            for spec, _context in items
        }


class _RecordingCheckpointStore(SqlAlchemyCheckpointStore):
    def __init__(self, session_factory):
        super().__init__(session_factory)
        self.transitions: list[tuple[str, str, str]] = []

    def begin_node(self, batch_id, spec, node, expected_version):
        self.transitions.append(("begin", spec.query_id, node))
        return super().begin_node(batch_id, spec, node, expected_version)

    def record_node(self, batch_id, spec, node, result, expected_version):
        self.transitions.append(("record", spec.query_id, node))
        return super().record_node(
            batch_id, spec, node, result, expected_version
        )


def _build_workflow(action_result: NodeResult):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    store = SqlAlchemyCheckpointStore(sessionmaker(bind=engine))
    events: list[str] = []
    action_gateway = _ActionGateway(action_result)
    runners = {
        "recall": _Runner("recall", events),
        "reorder": _Runner(
            "reorder",
            events,
            {"裙子": NodeResult.executed({"proposal": "sort"}, action={"type": "sort"})},
        ),
        "relevance": _Runner("relevance", events),
    }
    snapshot = capture_snapshot(
        node_keys=(
            "query_governance.kg_agent",
            "query_governance.preference_generation",
            "query_governance.relevance_multimodal",
            "query_governance.relevance_rules",
        ),
        version=11,
    )
    workflow = QueryGovernanceWorkflow(
        search=_Search(),
        store=store,
        checkpoint=store,
        runners=runners,
        action_gateway=action_gateway,
        model_config_snapshot=snapshot,
    )
    return engine, store, workflow, action_gateway, events


def _specs():
    return [
        QuerySpec("q1", "atlas", "裙子"),
        QuerySpec("q2", "atlas", "外套"),
    ]


def test_workflow_uses_one_cross_query_relevance_batch_for_fresh_queries():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = SqlAlchemyCheckpointStore(sessionmaker(bind=engine))
    events: list[str] = []
    batch_runner = _BatchRelevanceRunner()
    workflow = QueryGovernanceWorkflow(
        search=_Search(),
        store=store,
        checkpoint=store,
        runners={
            "recall": _Runner("recall", events),
            "reorder": _Runner("reorder", events),
            "relevance": _Runner("relevance", events),
        },
        action_gateway=_ActionGateway(NodeResult.executed({"verified": True})),
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            ),
            version=11,
        ),
        relevance_batch_runner=batch_runner,
    )
    try:
        result = workflow.run_batch(
            _specs(), batch_id="batch-batched", data_date=date(2026, 8, 29)
        )
        assert batch_runner.calls == [("裙子", "外套")]
        assert all(
            result[item].nodes["relevance"].status is NodeStatus.EXECUTED
            for item in ("q1", "q2")
        )
        assert result["q1"].nodes["relevance"].result["telemetry"] == {
            "batch_count": 1,
            "product_count": 1,
            "image_download_success_count": 1,
            "image_download_failure_count": 0,
        }
        assert not any(event.startswith("relevance:") for event in events)
        for query_id in ("q1", "q2"):
            rounds = result[query_id].nodes["relevance"].result["rounds"]
            assert rounds[0]["telemetry"]["batchKey"] == "model:1"
    finally:
        engine.dispose()


def test_workflow_limits_cross_query_relevance_checkpoint_group_size():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = SqlAlchemyCheckpointStore(sessionmaker(bind=engine))
    events: list[str] = []
    batch_runner = _BatchRelevanceRunner()
    workflow = QueryGovernanceWorkflow(
        search=_Search(),
        store=store,
        checkpoint=store,
        runners={
            "recall": _Runner("recall", events),
            "reorder": _Runner("reorder", events),
            "relevance": _Runner("relevance", events),
        },
        action_gateway=_ActionGateway(NodeResult.executed({"verified": True})),
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            ),
            version=11,
        ),
        relevance_batch_runner=batch_runner,
        relevance_query_batch_size=5,
    )
    specs = [QuerySpec(f"q{index}", "atlas", f"q{index}") for index in range(6)]
    try:
        result = workflow.run_batch(
            specs, batch_id="batch-relevance-chunks", data_date=date(2026, 8, 29)
        )
        assert batch_runner.calls == [
            ("q0", "q1", "q2", "q3", "q4"),
            ("q5",),
        ]
        assert all(
            result[spec.query_id].nodes["relevance"].status is NodeStatus.EXECUTED
            for spec in specs
        )
    finally:
        engine.dispose()


def test_workflow_retries_running_relevance_without_action_in_bounded_batch():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = SqlAlchemyCheckpointStore(sessionmaker(bind=engine))
    events: list[str] = []

    class _InterruptOnce(_BatchRelevanceRunner):
        def __init__(self):
            super().__init__()
            self.interrupted = False

        def run(self, inputs, context):
            if not self.interrupted:
                self.interrupted = True
                raise SystemExit("simulated worker interruption")
            return super().run(inputs, context)

    batch_runner = _InterruptOnce()
    workflow = QueryGovernanceWorkflow(
        search=_Search(),
        store=store,
        checkpoint=store,
        runners={
            "recall": _Runner("recall", events),
            "reorder": _Runner("reorder", events),
            "relevance": _Runner("relevance", events),
        },
        action_gateway=_ActionGateway(NodeResult.executed({"verified": True})),
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            ),
            version=11,
        ),
        relevance_batch_runner=batch_runner,
        relevance_query_batch_size=5,
    )
    specs = [QuerySpec(f"q{index}", "atlas", f"q{index}") for index in range(6)]
    try:
        try:
            workflow.run_batch(
                specs, batch_id="batch-retry-running", data_date=date(2026, 8, 29)
            )
        except SystemExit:
            pass
        result = workflow.run_batch(
            specs, batch_id="batch-retry-running", data_date=date(2026, 8, 29)
        )
        assert batch_runner.calls == [
            ("q0", "q1", "q2", "q3", "q4"),
            ("q5",),
        ]
        assert all(
            result[spec.query_id].nodes["relevance"].status is NodeStatus.EXECUTED
            for spec in specs
        )
    finally:
        engine.dispose()


def test_workflow_uses_cross_query_reorder_batch_and_serializes_actions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    checkpoint = _RecordingCheckpointStore(sessionmaker(bind=engine))
    events: list[str] = []
    reorder_batch = _BatchReorderRunner()
    action_gateway = _ActionGateway(NodeResult.executed({"verified": True}))
    workflow = QueryGovernanceWorkflow(
        search=_Search(),
        store=checkpoint,
        checkpoint=checkpoint,
        runners={node: _Runner(node, events) for node in ("recall", "reorder", "relevance")},
        action_gateway=action_gateway,
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            ),
            version=11,
        ),
        reorder_batch_runner=reorder_batch,
    )
    try:
        result = workflow.run_batch(
            _specs(), batch_id="batch-reorder-batched", data_date=date(2026, 8, 29)
        )
        assert reorder_batch.calls == [("裙子", "外套")]
        assert [query_id for query_id, _action in action_gateway.calls] == ["q1", "q2"]
        assert all(
            result[query_id].nodes["reorder"].status is NodeStatus.EXECUTED
            for query_id in ("q1", "q2")
        )
        persisted = checkpoint.get_checkpoint("batch-reorder-batched")
        assert persisted.status == "completed"
        assert not any(event.startswith("reorder:") for event in events)
    finally:
        engine.dispose()


def test_workflow_collects_s0_queries_concurrently_with_bounded_workers():
    engine = create_engine(
        f"sqlite:////tmp/aiops-s0-parallel-{uuid.uuid4().hex}.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    store = SqlAlchemyCheckpointStore(sessionmaker(bind=engine))

    class SlowSearch(_Search):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0
            self._lock = threading.Lock()

        def mixed(self, brand: str, query: str):
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.03)
                return super().mixed(brand, query)
            finally:
                with self._lock:
                    self.active -= 1

    search = SlowSearch()
    events: list[str] = []
    workflow = QueryGovernanceWorkflow(
        search=search,
        store=store,
        checkpoint=store,
        runners={node: _Runner(node, events) for node in ("recall", "reorder", "relevance")},
        action_gateway=_ActionGateway(NodeResult.executed()),
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            )
        ),
        query_concurrency=2,
    )
    try:
        started = time.perf_counter()
        workflow.run_batch(
            [QuerySpec(f"q{i}", "atlas", f"query-{i}") for i in range(4)],
            batch_id="batch-s0-parallel",
            data_date=date(2026, 8, 29),
        )
        elapsed = time.perf_counter() - started
        assert search.max_active == 2
        # The workflow performs a second Mixed pass for relevance; the bound
        # still proves the first four-call S0 fan-out is not serial.
        assert elapsed < 0.4
        with Session(engine) as db:
            s0_steps = db.scalars(
                select(QueryGovernanceStepStatus)
                .join(QueryGovernanceTask)
                .where(
                    QueryGovernanceTask.batch_id == "batch-s0-parallel",
                    QueryGovernanceStepStatus.node_key == "s0",
                )
            ).all()
        assert len(s0_steps) == 4
        for step in s0_steps:
            payload = json.loads(step.result_json)
            execution = payload["telemetry"]["executionSeconds"]
            assert step.duration_seconds < 0.2
            assert execution <= step.duration_seconds
    finally:
        engine.dispose()


def test_database_mainline_persists_completion_and_action_evidence():
    engine, store, workflow, action_gateway, events = _build_workflow(
        NodeResult.executed({"verified": True})
    )
    try:
        result = workflow.run_batch(
            _specs(), batch_id="batch-1", data_date=date(2026, 8, 29)
        )

        with Session(engine) as db:
            run = db.scalars(
                select(QueryGovernanceWorkflowRun).where(
                    QueryGovernanceWorkflowRun.batch_id == "batch-1"
                )
            ).one()
            tasks = db.scalars(
                select(QueryGovernanceTask)
                .where(QueryGovernanceTask.batch_id == "batch-1")
                .order_by(QueryGovernanceTask.query_rank)
            ).all()
            steps = db.scalars(
                select(QueryGovernanceStepStatus)
                .order_by(QueryGovernanceStepStatus.task_id, QueryGovernanceStepStatus.id)
            ).all()

        assert run.status == "completed"
        assert run.model_config_version == 11
        assert run.finished_at is not None
        assert [task.status for task in tasks] == ["completed", "completed"]
        assert all(task.started_at is not None for task in tasks)
        assert len(steps) == 8
        assert all(
            step.status in {"executed", "skipped", "error"} for step in steps
        )
        assert result["q1"].nodes["reorder"].action == {"type": "sort"}
        reorder_step = next(
            step
            for step in steps
            if step.task_id == tasks[0].id and step.node_key == "reorder"
        )
        assert json.loads(reorder_step.result_json)["action"] == {"type": "sort"}
        assert action_gateway.calls == [("q1", {"type": "sort"})]
        recall_positions = [
            i for i, event in enumerate(events) if event.startswith("recall:")
        ]
        reorder_positions = [i for i, event in enumerate(events) if event.startswith("reorder:")]
        relevance_positions = [i for i, event in enumerate(events) if event.startswith("relevance:")]
        assert max(reorder_positions) < min(relevance_positions)
        assert max(relevance_positions) < min(recall_positions)
        assert store.get_checkpoint("batch-1").status == "completed"
    finally:
        engine.dispose()


def test_s0_failure_closes_query_nodes_and_batch_as_error():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = SqlAlchemyCheckpointStore(sessionmaker(bind=engine))
    events: list[str] = []

    class FailingSearch(_Search):
        def mixed(self, brand: str, query: str):
            raise RuntimeError(f"Mixed unavailable for {query}")

    workflow = QueryGovernanceWorkflow(
        search=FailingSearch(),
        store=store,
        checkpoint=store,
        runners={node: _Runner(node, events) for node in ("recall", "reorder", "relevance")},
        action_gateway=_ActionGateway(NodeResult.executed()),
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            ),
            version=11,
        ),
    )
    try:
        result = workflow.run_batch(
            _specs(), batch_id="batch-s0-error", data_date=date(2026, 8, 29)
        )
        assert all(run.final_status == "error" for run in result.values())
        assert all(
            all(node.status in {NodeStatus.ERROR, NodeStatus.SKIPPED} for node in run.nodes.values())
            for run in result.values()
        )
        with Session(engine) as db:
            run = db.scalars(
                select(QueryGovernanceWorkflowRun).where(
                    QueryGovernanceWorkflowRun.batch_id == "batch-s0-error"
                )
            ).one()
            tasks = db.scalars(
                select(QueryGovernanceTask)
                .where(QueryGovernanceTask.batch_id == "batch-s0-error")
                .order_by(QueryGovernanceTask.query_rank)
            ).all()
            steps = db.scalars(
                select(QueryGovernanceStepStatus)
                .order_by(QueryGovernanceStepStatus.task_id, QueryGovernanceStepStatus.id)
            ).all()
        assert run.status == "error"
        assert [task.status for task in tasks] == ["error", "error"]
        assert all(task.error_code == "MIXED_FAILED" for task in tasks)
        assert all(step.status in {"error", "skipped"} for step in steps)
    finally:
        engine.dispose()


def test_checkpoint_persists_reorder_skip_and_still_runs_relevance():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = SqlAlchemyCheckpointStore(sessionmaker(bind=engine))
    events: list[str] = []
    workflow = QueryGovernanceWorkflow(
        search=_Search(),
        store=store,
        checkpoint=store,
        runners={node: _Runner(node, events) for node in ("recall", "reorder", "relevance")},
        action_gateway=_ActionGateway(NodeResult.executed()),
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            )
        ),
    )
    try:
        result = workflow.run_batch(
            [
                QuerySpec(
                    "q-normal",
                    "atlas",
                    "裙子",
                    reorder_eligible=False,
                    reorder_skip_reason="CTR/CVR 正常",
                ),
                QuerySpec("q-decline", "atlas", "外套", reorder_eligible=True),
            ],
            batch_id="batch-reorder-skip",
            data_date=date(2026, 8, 29),
        )

        assert result["q-normal"].nodes["reorder"].status is NodeStatus.SKIPPED
        assert "reorder:裙子" not in events
        assert result["q-normal"].nodes["relevance"].status is NodeStatus.EXECUTED
        with Session(engine) as db:
            tasks = db.scalars(
                select(QueryGovernanceTask)
                .where(QueryGovernanceTask.batch_id == "batch-reorder-skip")
                .order_by(QueryGovernanceTask.query_rank)
            ).all()
            skipped = db.scalars(
                select(QueryGovernanceStepStatus).where(
                    QueryGovernanceStepStatus.task_id == tasks[0].id,
                    QueryGovernanceStepStatus.node_key == "reorder",
                )
            ).one()
        assert skipped.status == "skipped"
        assert json.loads(skipped.result_json)["result"]["reason"] == "CTR/CVR 正常"
        assert store.get_checkpoint("batch-reorder-skip").status == "completed"
    finally:
        engine.dispose()


def test_real_action_gateway_rolls_back_and_persists_verification_evidence():
    action_events: list[str] = []

    class _Executor:
        def execute(self, spec, action, context):
            action_events.append("execute")
            return {"request_id": "req-1", "written": True}

        def rollback(self, spec, action, execution, context):
            action_events.append("rollback")
            return {"restored": True}

    def _verify(spec, action, products, execution):
        action_events.append("verify")
        actual = [item["product_id"] for item in products]
        return {
            "matched": actual == action["expected_product_ids"],
            "actual_product_ids": actual,
            "expected_product_ids": action["expected_product_ids"],
        }

    class _ActionSearch(_Search):
        def mixed(self, brand, query):
            self.calls.append((brand, query))
            if len(self.calls) == 2:
                action_events.append("mixed")
            return [
                {"product_id": "P-1", "rank": 1},
                {"product_id": "P-2", "rank": 2},
            ]

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = SqlAlchemyCheckpointStore(sessionmaker(bind=engine))
    workflow_events: list[str] = []
    search = _ActionSearch()
    workflow = QueryGovernanceWorkflow(
        search=search,
        store=store,
        checkpoint=store,
        runners={
            "recall": _Runner("recall", workflow_events),
            "reorder": _Runner(
                "reorder",
                workflow_events,
                {
                    "裙子": NodeResult.executed(
                        {"proposal": "sort"},
                        action={
                            "type": "query_reorder",
                            "expected_product_ids": ["P-2", "P-1"],
                        },
                    )
                },
            ),
            "relevance": _Runner("relevance", workflow_events),
        },
        action_gateway=VerifiedActionGateway(
            search=search,
            executor=_Executor(),
            verifier=_verify,
        ),
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            )
        ),
    )
    try:
        result = workflow.run_batch(
            [QuerySpec("q1", "atlas", "裙子")],
            batch_id="batch-1",
            data_date=date(2026, 8, 29),
        )

        assert action_events == ["execute", "mixed", "verify", "rollback"]
        assert result["q1"].nodes["reorder"].error_code == (
            "ACTION_VERIFICATION_FAILED"
        )
        assert result["q1"].nodes["relevance"].status is NodeStatus.EXECUTED
        with Session(engine) as db:
            task = db.scalars(select(QueryGovernanceTask)).one()
            reorder_step = db.scalars(
                select(QueryGovernanceStepStatus).where(
                    QueryGovernanceStepStatus.task_id == task.id,
                    QueryGovernanceStepStatus.node_key == "reorder",
                )
            ).one()
        evidence = json.loads(reorder_step.result_json)
        assert evidence["result"]["verification"]["matched"] is False
        assert evidence["result"]["rollback"] == {
            "status": "executed",
            "result": {"restored": True},
        }
        assert store.get_checkpoint("batch-1").status == "completed"
    finally:
        engine.dispose()


def test_relevance_mixed_failure_still_transitions_through_running():
    class _FailingRelevanceSearch(_Search):
        def mixed(self, brand, query):
            products = super().mixed(brand, query)
            if len(self.calls) == 2:
                raise RuntimeError("Mixed unavailable")
            return products

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = _RecordingCheckpointStore(sessionmaker(bind=engine))
    events: list[str] = []
    workflow = QueryGovernanceWorkflow(
        search=_FailingRelevanceSearch(),
        store=store,
        checkpoint=store,
        runners={node: _Runner(node, events) for node in ("recall", "reorder", "relevance")},
        action_gateway=_ActionGateway(NodeResult.executed()),
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            )
        ),
    )
    try:
        result = workflow.run_batch(
            [QuerySpec("q1", "atlas", "裙子")],
            batch_id="batch-1",
            data_date=date(2026, 8, 29),
        )

        assert result["q1"].nodes["relevance"].error_code == "RELEVANCE_SEARCH_FAILED"
        assert ("begin", "q1", "relevance") in store.transitions
        assert ("record", "q1", "relevance") in store.transitions
        assert store.transitions[-4:] == [
            ("begin", "q1", "relevance"),
            ("record", "q1", "relevance"),
            ("begin", "q1", "recall"),
            ("record", "q1", "recall"),
        ]
    finally:
        engine.dispose()


def test_reorder_verification_error_isolated_and_relevance_continues():
    engine, store, workflow, _, _ = _build_workflow(
        NodeResult.error("MIXED_VERIFY_FAILED", "回读失败，已回滚")
    )
    try:
        result = workflow.run_batch(
            _specs(), batch_id="batch-1", data_date=date(2026, 8, 29)
        )

        assert result["q1"].nodes["reorder"].status is NodeStatus.ERROR
        assert result["q1"].final_status == "error"
        assert result["q1"].nodes["relevance"].status is NodeStatus.EXECUTED
        assert result["q2"].nodes["relevance"].status is NodeStatus.EXECUTED
        assert store.get_checkpoint("batch-1").status == "completed"
    finally:
        engine.dispose()


class _VectorResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _VectorPost:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def __call__(self, url, *, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if json["query"] == "外套":
            raise RuntimeError("vector unavailable")
        return _VectorResponse(
            {
                "total": 2,
                "pageNum": 1,
                "pageSize": 30,
                "result": [
                    {"taggingUid": "p1", "productName": "商品 1"},
                    {"taggingUid": "p2", "productName": "商品 2"},
                ],
            }
        )


def test_vector_reorder_is_persisted_and_failure_does_not_block_relevance():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = SqlAlchemyCheckpointStore(sessionmaker(bind=engine))
    vector_post = _VectorPost()
    events: list[str] = []
    workflow = QueryGovernanceWorkflow(
        search=_Search(),
        store=store,
        checkpoint=store,
        runners={
            "recall": _Runner("recall", events),
            "reorder": VectorReorderNodeRunner(
                client=VectorReorderClient(request_func=vector_post),
                config=VectorReorderConfig(product_limit=1, concurrency=2),
            ),
            "relevance": _Runner("relevance", events),
        },
        action_gateway=_ActionGateway(NodeResult.executed()),
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            )
        ),
    )
    specs = [
        QuerySpec("q1", "atlas", "裙子"),
        QuerySpec("q2", "atlas", "外套"),
        QuerySpec("q3", "atlas", "鞋子"),
    ]
    try:
        result = workflow.run_batch(
            specs,
            batch_id="batch-vector",
            data_date=date(2026, 8, 29),
            preference_results={
                "q1": {
                    "should_reorder": True,
                    "preference_description": "轻薄裙装",
                },
                "q2": {
                    "should_reorder": True,
                    "preference_description": "通勤外套",
                },
                "q3": {"should_reorder": False, "reason": "证据不足"},
            },
        )

        assert result["q1"].nodes["reorder"].status is NodeStatus.EXECUTED
        assert result["q1"].nodes["reorder"].result["product_ids"] == ["p1"]
        assert result["q2"].nodes["reorder"].status is NodeStatus.ERROR
        assert result["q2"].nodes["reorder"].error_code == "VECTOR_REORDER_REQUEST_FAILED"
        assert result["q3"].nodes["reorder"].status is NodeStatus.SKIPPED
        assert result["q3"].nodes["reorder"].result["reason"] == "证据不足"
        assert all(
            result[query_id].nodes["relevance"].status is NodeStatus.EXECUTED
            for query_id in ("q1", "q2", "q3")
        )

        with Session(engine) as db:
            tasks = db.scalars(
                select(QueryGovernanceTask)
                .where(QueryGovernanceTask.batch_id == "batch-vector")
                .order_by(QueryGovernanceTask.query_rank)
            ).all()
            steps = db.scalars(
                select(QueryGovernanceStepStatus)
                .where(QueryGovernanceStepStatus.node_key == "reorder")
                .order_by(QueryGovernanceStepStatus.task_id)
            ).all()
        assert [task.status for task in tasks] == ["completed", "error", "completed"]
        assert [step.status for step in steps] == ["executed", "error", "skipped"]
        assert json.loads(steps[0].result_json)["result"]["product_ids"] == ["p1"]
        assert steps[1].error_code == "VECTOR_REORDER_REQUEST_FAILED"
        assert len(vector_post.calls) == 2
        detail = QueryGovernanceReadService(
            sessionmaker(bind=engine)
        ).query_detail(tasks[0].id)
        reorder = next(
            node for node in detail["nodes"] if node["nodeKey"] == "reorder"
        )
        assert reorder["result"]["result"]["product_ids"] == ["p1"]
    finally:
        engine.dispose()
