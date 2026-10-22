from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.llm.node_config import QUERY_GOVERNANCE_NODE_KEYS, capture_snapshot
from src.persistence.models import Base
from src.query_governance.checkpoint import (
    CheckpointConflictError,
    LegacyCheckpointError,
    SqlAlchemyCheckpointStore,
)
from src.query_governance.models import (
    ActionRecoveryResult,
    NODE_ORDER,
    NodeResult,
    NodeStatus,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
    QuerySpec,
)
from src.query_governance.ports import ActionGateway, NodeRunner, SearchGateway
from src.query_governance.workflow import QueryGovernanceWorkflow


MODEL_CONFIG_SNAPSHOT = capture_snapshot(
    node_keys=QUERY_GOVERNANCE_NODE_KEYS
)


def build_store():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, SqlAlchemyCheckpointStore(sessionmaker(bind=engine))


def specs():
    return [
        QuerySpec("q1", "atlas", "裙子"),
        QuerySpec("q2", "atlas", "外套"),
    ]


def test_checkpoint_persists_batch_cursor_and_node_in_one_transaction():
    engine, store = build_store()
    try:
        checkpoint = store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
        running = store.begin_node("batch-1", specs()[0], "s0", checkpoint.version)
        checkpoint = running
        checkpoint = store.record_node(
            "batch-1",
            specs()[0],
            "s0",
            NodeResult.executed({"products": []}),
            checkpoint.version,
        )

        with Session(engine) as db:
            task = db.scalars(
                select(QueryGovernanceTask).where(
                    QueryGovernanceTask.batch_id == "batch-1",
                    QueryGovernanceTask.query_text == "裙子",
                )
            ).one()
            step = db.scalars(
                select(QueryGovernanceStepStatus).where(
                    QueryGovernanceStepStatus.task_id == task.id,
                    QueryGovernanceStepStatus.node_key == "s0",
                )
            ).one()
            run = db.scalars(
                select(QueryGovernanceWorkflowRun).where(
                    QueryGovernanceWorkflowRun.batch_id == "batch-1"
                )
            ).one()

        assert step.status == NodeStatus.EXECUTED.value
        assert run.current_node == "s0"
        assert run.checkpoint_version == checkpoint.version
        assert checkpoint.last_completed == {
            "query_id": "q1",
            "brand": "atlas",
            "query": "裙子",
            "node": "s0",
        }
    finally:
        engine.dispose()


def test_resume_skips_terminal_nodes_and_retries_only_running_node():
    engine, store = build_store()
    try:
        initial = store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
        current = initial
        for node in ("s0", "recall"):
            current = store.begin_node("batch-1", specs()[0], node, current.version)
            current = store.record_node(
                "batch-1", specs()[0], node, NodeResult.executed({"node": node}), current.version
            )
        current = store.begin_node("batch-1", specs()[1], "s0", current.version)

        resumed = store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
        assert resumed.version == current.version
        assert store.get_node("batch-1", specs()[0], "s0").status is NodeStatus.EXECUTED
        assert store.get_node("batch-1", specs()[0], "recall").status is NodeStatus.EXECUTED
        assert store.get_node("batch-1", specs()[1], "s0").status is NodeStatus.RUNNING
    finally:
        engine.dispose()


def test_stale_resume_restarts_attempt_clock(monkeypatch):
    engine, store = build_store()
    try:
        store.start_batch("batch-stale-clock", specs(), data_date=date(2026, 8, 28))
        with Session(engine) as db:
            run = db.scalars(
                select(QueryGovernanceWorkflowRun).where(
                    QueryGovernanceWorkflowRun.batch_id == "batch-stale-clock"
                )
            ).one()
            original_started_at = run.started_at

        fixed_now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        with Session(engine) as db:
            run = db.scalars(
                select(QueryGovernanceWorkflowRun).where(
                    QueryGovernanceWorkflowRun.batch_id == "batch-stale-clock"
                )
            ).one()
            run.started_at = fixed_now.replace(tzinfo=None) - timedelta(hours=3)
            run.heartbeat_at = fixed_now.replace(tzinfo=None) - timedelta(hours=2)
            db.commit()

        monkeypatch.setattr(store, "_now", lambda: fixed_now)
        resumed = store.start_batch("batch-stale-clock", specs(), data_date=date(2026, 8, 28))

        with Session(engine) as db:
            run = db.scalars(
                select(QueryGovernanceWorkflowRun).where(
                    QueryGovernanceWorkflowRun.batch_id == "batch-stale-clock"
                )
            ).one()
        assert resumed.status == "running"
        assert original_started_at is not None
        assert run.started_at == fixed_now.replace(tzinfo=None)
    finally:
        engine.dispose()


def test_resume_reopens_only_retryable_terminal_errors():
    engine, store = build_store()
    try:
        checkpoint = store.start_batch("batch-retry-errors", specs(), data_date=date(2026, 8, 28))
        current = checkpoint
        for node in NODE_ORDER:
            current = store.begin_node("batch-retry-errors", specs()[0], node, current.version)
            result = (
                NodeResult.error("RELEVANCE_OUTPUT_INVALID", "temporary model output")
                if node == "relevance"
                else NodeResult.executed({"node": node})
            )
            current = store.record_node(
                "batch-retry-errors", specs()[0], node, result, current.version
            )

        resumed = store.start_batch(
            "batch-retry-errors", specs(), data_date=date(2026, 8, 28)
        )

        assert store.get_node("batch-retry-errors", specs()[0], "s0").status is NodeStatus.EXECUTED
        assert store.get_node("batch-retry-errors", specs()[0], "recall").status is NodeStatus.EXECUTED
        assert store.get_node("batch-retry-errors", specs()[0], "reorder").status is NodeStatus.EXECUTED
        assert store.get_node("batch-retry-errors", specs()[0], "relevance").status is NodeStatus.PENDING
        assert resumed.status == "running"
    finally:
        engine.dispose()


def test_checkpoint_compare_and_swap_rejects_stale_writer():
    engine, store = build_store()
    try:
        checkpoint = store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
        newer = store.begin_node("batch-1", specs()[0], "s0", checkpoint.version)
        with pytest.raises(CheckpointConflictError):
            store.begin_node("batch-1", specs()[1], "s0", checkpoint.version)
        assert newer.version > checkpoint.version
    finally:
        engine.dispose()


def test_legacy_checkpoint_payload_is_not_resumed():
    engine, store = build_store()
    try:
        with Session(engine) as db:
            db.add(
                QueryGovernanceTask(
                    batch_id="legacy-batch",
                    brand="atlas",
                    data_date=date(2026, 8, 28),
                    query_text="裙子",
                )
            )
            db.add(
                QueryGovernanceWorkflowRun(
                    batch_id="legacy-batch",
                    checkpoint_payload='{"step":"planner"}',
                    checkpoint_version=4,
                )
            )
            db.commit()
        with pytest.raises(LegacyCheckpointError):
            store.start_batch("legacy-batch", specs(), data_date=date(2026, 8, 28))
    finally:
        engine.dispose()


class Search(SearchGateway):
    def mixed(self, brand: str, query: str):
        return [{"product_id": f"{query}-1", "rank": 1, "ext": {"bucket": "productIdsByCode"}}]


class Runner(NodeRunner):
    def __init__(self, node: str, events: list[str]):
        self.node = node
        self.events = events

    def run(self, spec, context):
        self.events.append(f"{self.node}:{spec.query}")
        return NodeResult.executed({"node": self.node})


class Actions(ActionGateway):
    def __init__(self, recovery=None):
        self.recovery = recovery
        self.recovery_calls = []

    def execute_and_verify(self, spec, action, context):
        return NodeResult.executed({"action": action})

    def recover_interrupted(self, spec, node, action, context):
        self.recovery_calls.append((spec.query_id, node, action))
        return self.recovery or ActionRecoveryResult.unresolved(
            "ACTION_RESULT_UNKNOWN", "权威回读仍无法确认动作结果"
        )


def test_workflow_resume_uses_checkpointed_results_without_rerunning_them():
    engine, store = build_store()
    events: list[str] = []
    runners = {node: Runner(node, events) for node in ("recall", "reorder", "relevance")}
    try:
        workflow = QueryGovernanceWorkflow(
            search=Search(),
            store=store,
            checkpoint=store,
            runners=runners,
            action_gateway=Actions(),
            model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
        )
        workflow.run_batch(specs(), batch_id="batch-1", data_date=date(2026, 8, 28))
        events.clear()
        workflow.run_batch(specs(), batch_id="batch-1", data_date=date(2026, 8, 28))
        assert events == []
    finally:
        engine.dispose()


def test_checkpoint_payload_contains_new_schema_and_query_order():
    engine, store = build_store()
    try:
        store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
        with Session(engine) as db:
            run = db.scalars(
                select(QueryGovernanceWorkflowRun).where(
                    QueryGovernanceWorkflowRun.batch_id == "batch-1"
                )
            ).one()
        payload = __import__("json").loads(run.checkpoint_payload)
        assert payload["schema"] == "query_governance_checkpoint_v1"
        assert [item["query_id"] for item in payload["query_order"]] == ["q1", "q2"]
    finally:
        engine.dispose()


def test_s0_error_is_retryable_but_successful_s0_is_immutable():
    engine, store = build_store()
    try:
        checkpoint = store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
        checkpoint = store.begin_node("batch-1", specs()[0], "s0", checkpoint.version)
        checkpoint = store.record_node(
            "batch-1", specs()[0], "s0", NodeResult.error("MIXED_FAILED", "temporary"), checkpoint.version
        )
        checkpoint = store.begin_node("batch-1", specs()[0], "s0", checkpoint.version)
        store.save_s0_if_absent("atlas", "裙子", {"products": [{"rank": 1}]})
        store.record_node(
            "batch-1", specs()[0], "s0", NodeResult.executed({"products": [{"rank": 1}]}), checkpoint.version
        )
        assert store.get_s0("atlas", "裙子")["products"][0]["rank"] == 1
    finally:
        engine.dispose()


def test_interrupted_action_is_recovered_before_retry():
    engine, store = build_store()
    try:
        checkpoint = store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
        checkpoint = store.begin_node("batch-1", specs()[0], "reorder", checkpoint.version)
        store.save_action(
            "batch-1", specs()[0], "reorder", {"request_id": "req-1", "idempotency_key": "key-1"}, checkpoint.version
        )
        inflight = store.get_inflight_action("batch-1", specs()[0], "reorder")
        assert inflight is not None
        assert inflight.action["idempotency_key"] == "key-1"
        assert ActionRecoveryResult.safe_to_retry().status.value == "safe_to_retry"
    finally:
        engine.dispose()


def test_large_inflight_action_is_checkpointed_once_and_remains_recoverable():
    engine, store = build_store()
    try:
        checkpoint = store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
        checkpoint = store.begin_node("batch-1", specs()[0], "relevance", checkpoint.version)
        action = {
            "type": "product_blacklist",
            "product_ids": ["p1"],
            "_relevance_checkpoint": {
                "aggregate": {
                    "blacklist_candidates": [
                        {"product_id": "p1", "reason": "x" * 70_000}
                    ]
                },
                "seen_product_ids": ["p1"],
                "round_index": 1,
            },
        }

        store.save_action(
            "batch-1", specs()[0], "relevance", action, checkpoint.version
        )

        with Session(engine) as db:
            run = db.scalars(
                select(QueryGovernanceWorkflowRun).where(
                    QueryGovernanceWorkflowRun.batch_id == "batch-1"
                )
            ).one()
        payload = __import__("json").loads(run.checkpoint_payload)
        inflight = store.get_inflight_action("batch-1", specs()[0], "relevance")

        assert len(run.checkpoint_payload) < 2_000
        assert payload["active_action"] == {
            "type": "product_blacklist",
            "product_count": 1,
        }
        assert "action" not in payload["active"]
        assert inflight is not None
        assert (
            len(
                inflight.action["_relevance_checkpoint"]["aggregate"]
                ["blacklist_candidates"][0]["reason"]
            )
            > 65_535
        )
        assert inflight.action == action
    finally:
        engine.dispose()


def prepare_interrupted_reorder(store):
    checkpoint = store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
    for node in ("s0", "recall"):
        checkpoint = store.begin_node("batch-1", specs()[0], node, checkpoint.version)
        result = (
            NodeResult.executed({"products": [{"product_id": "dress-1"}]})
            if node == "s0"
            else NodeResult.executed({"node": node})
        )
        checkpoint = store.record_node(
            "batch-1", specs()[0], node, result, checkpoint.version
        )
    store.save_s0_if_absent(
        "atlas", "裙子", {"products": [{"product_id": "dress-1"}]}
    )
    checkpoint = store.begin_node(
        "batch-1", specs()[0], "reorder", checkpoint.version
    )
    return store.save_action(
        "batch-1",
        specs()[0],
        "reorder",
        {"request_id": "req-1", "idempotency_key": "key-1"},
        checkpoint.version,
    )


def test_workflow_uses_authoritative_action_result_without_duplicate_execution():
    engine, store = build_store()
    events = []
    actions = Actions(
        ActionRecoveryResult.completed(NodeResult.executed({"confirmed": True}))
    )
    try:
        prepare_interrupted_reorder(store)
        workflow = QueryGovernanceWorkflow(
            search=Search(),
            store=store,
            checkpoint=store,
            runners={node: Runner(node, events) for node in ("recall", "reorder", "relevance")},
            action_gateway=actions,
            model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
        )
        workflow.run_batch(specs(), batch_id="batch-1", data_date=date(2026, 8, 28))

        assert actions.recovery_calls == [
            (
                "q1",
                "reorder",
                {"request_id": "req-1", "idempotency_key": "key-1"},
            )
        ]
        assert "reorder:裙子" not in events
        assert store.get_node("batch-1", specs()[0], "reorder").result == {
            "confirmed": True
        }
    finally:
        engine.dispose()


@pytest.mark.parametrize("independent", [False, True])
def test_workflow_recovers_interrupted_relevance_blacklist_before_model_retry(independent):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = SqlAlchemyCheckpointStore(factory)
    spec = QuerySpec("q1", "atlas", "裙子")
    events: list[str] = []
    state = {
        "aggregate": {
            "query": "裙子",
            "product_count": 2,
            "reused_product_ids": [],
            "requested_product_ids": ["p1", "p2"],
            "detected_product_ids": ["p1", "p2"],
            "relevant_product_ids": ["p2"],
            "blacklist_candidates": [{"product_id": "p1", "reason": "不相关"}],
            "warnings": [],
            "rounds": [{"round": 1, "blacklisted_product_ids": ["p1"]}],
        },
        "seen_product_ids": ["p1", "p2"],
        "round_index": 1,
    }
    action = {
        "type": "product_blacklist",
        "product_ids": ["p1"],
        "_relevance_checkpoint": state,
    }
    checkpoint = store.start_batch("batch-1", [spec], data_date=date(2026, 8, 28))
    for node in ("s0", "recall", "reorder"):
        checkpoint = store.begin_node("batch-1", spec, node, checkpoint.version)
        checkpoint = store.record_node(
            "batch-1", spec, node, NodeResult.executed({"node": node}), checkpoint.version
        )
    checkpoint = store.begin_node("batch-1", spec, "relevance", checkpoint.version)
    store.save_action("batch-1", spec, "relevance", action, checkpoint.version)

    class RelevanceRecoveryActions(Actions):
        def recover_interrupted(self, spec, node, action, context):
            self.recovery_calls.append((spec.query_id, node, action))
            return ActionRecoveryResult.completed(
                NodeResult.executed(
                    {"recovered": True, "relevance_checkpoint": state},
                    action=action,
                )
            )

    actions = RelevanceRecoveryActions()
    class RelevanceRunner(Runner):
        def run(self, spec, context):
            events.append("model")
            assert [item["product_id"] for item in context["current_products"]] == ["裙子-1"]
            return NodeResult.executed({"detected_product_ids": ["裙子-1"]})

    try:
        workflow = QueryGovernanceWorkflow(
            search=Search(),
            store=store,
            checkpoint=store,
            runners={
                "recall": Runner("recall", events),
                "reorder": Runner("reorder", events),
                "relevance": RelevanceRunner("relevance", events),
            },
            action_gateway=actions,
            model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
            relevance_independent_actions=independent,
        )
        result = workflow.run_batch(
            [spec], batch_id="batch-1", data_date=date(2026, 8, 28)
        )["q1"]

        assert result.nodes["relevance"].status is NodeStatus.EXECUTED
        expected_ids = ["p1", "p2"] if independent else ["p1", "p2", "裙子-1"]
        assert result.nodes["relevance"].result["detected_product_ids"] == expected_ids
        assert events == ([] if independent else ["model"])
        assert actions.recovery_calls == [("q1", "relevance", action)]
    finally:
        engine.dispose()


def test_recovered_relevance_blacklist_fails_when_target_remains_in_mixed():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = SqlAlchemyCheckpointStore(factory)
    spec = QuerySpec("q1", "atlas", "裙子")
    state = {
        "aggregate": {
            "query": "裙子",
            "product_count": 1,
            "reused_product_ids": [],
            "requested_product_ids": ["p1"],
            "detected_product_ids": ["p1"],
            "relevant_product_ids": [],
            "blacklist_candidates": [{"product_id": "p1", "reason": "不相关"}],
            "warnings": [],
            "rounds": [{"round": 1, "blacklisted_product_ids": ["p1"]}],
        },
        "seen_product_ids": ["p1"],
        "round_index": 1,
    }
    action = {
        "type": "product_blacklist",
        "product_ids": ["p1"],
        "_relevance_checkpoint": state,
    }
    checkpoint = store.start_batch("batch-1", [spec], data_date=date(2026, 8, 28))
    for node in ("s0", "recall", "reorder"):
        checkpoint = store.begin_node("batch-1", spec, node, checkpoint.version)
        checkpoint = store.record_node(
            "batch-1", spec, node, NodeResult.executed({"node": node}), checkpoint.version
        )
    checkpoint = store.begin_node("batch-1", spec, "relevance", checkpoint.version)
    store.save_action("batch-1", spec, "relevance", action, checkpoint.version)

    class RelevanceRecoveryActions(Actions):
        def recover_interrupted(self, spec, node, action, context):
            return ActionRecoveryResult.completed(
                NodeResult.executed(
                    {"recovered": True, "relevance_checkpoint": state},
                    action=action,
                )
            )

    class RemainingSearch(Search):
        def mixed(self, brand, query):
            return [{"product_id": "p1", "rank": 1, "ext": {"bucket": "productIdsByCode"}}]

    try:
        workflow = QueryGovernanceWorkflow(
            search=RemainingSearch(),
            store=store,
            checkpoint=store,
            runners={node: Runner(node, []) for node in ("recall", "reorder", "relevance")},
            action_gateway=RelevanceRecoveryActions(),
            model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
        )
        result = workflow.run_batch(
            [spec], batch_id="batch-1", data_date=date(2026, 8, 28)
        )["q1"]

        relevance_result = result.nodes["relevance"]
        assert relevance_result.status is NodeStatus.ERROR
        assert relevance_result.error_code == "RELEVANCE_BLACKLIST_SEARCH_EFFECT_NOT_VERIFIED"
        assert relevance_result.result["search_effect"] == {
            "status": "failed",
            "observed_product_ids": ["p1"],
        }
    finally:
        engine.dispose()


def test_workflow_does_not_retry_an_unresolved_interrupted_action():
    engine, store = build_store()
    events = []
    actions = Actions(
        ActionRecoveryResult.unresolved(
            "ACTION_RESULT_UNKNOWN", "权威回读仍无法确认动作结果"
        )
    )
    try:
        prepare_interrupted_reorder(store)
        workflow = QueryGovernanceWorkflow(
            search=Search(),
            store=store,
            checkpoint=store,
            runners={node: Runner(node, events) for node in ("recall", "reorder", "relevance")},
            action_gateway=actions,
            model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
        )
        workflow.run_batch(specs(), batch_id="batch-1", data_date=date(2026, 8, 28))

        result = store.get_node("batch-1", specs()[0], "reorder")
        assert result.status is NodeStatus.ERROR
        assert result.error_code == "ACTION_RESULT_UNKNOWN"
        assert "reorder:裙子" not in events
    finally:
        engine.dispose()


def test_failed_atomic_node_write_leaves_step_and_version_unchanged():
    engine, store = build_store()
    try:
        checkpoint = store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
        checkpoint = store.begin_node("batch-1", specs()[0], "recall", checkpoint.version)
        with pytest.raises(Exception):
            store.record_node(
                "batch-1",
                specs()[0],
                "recall",
                NodeResult.executed({"invalid": object()}),
                checkpoint.version,
            )
        assert store.get_checkpoint("batch-1").version == checkpoint.version
        assert store.get_node("batch-1", specs()[0], "recall").status is NodeStatus.RUNNING
    finally:
        engine.dispose()


def test_batch_scoped_s0_stores_do_not_leak_between_concurrent_batches():
    engine, store = build_store()
    try:
        store.start_batch("batch-1", specs(), data_date=date(2026, 8, 28))
        first = store.s0_store("batch-1")
        store.start_batch("batch-2", specs(), data_date=date(2026, 8, 29))
        second = store.s0_store("batch-2")

        first.save_s0_if_absent("atlas", "裙子", {"products": [{"rank": 1}]})
        second.save_s0_if_absent("atlas", "裙子", {"products": [{"rank": 2}]})

        assert first.get_s0("atlas", "裙子")["products"][0]["rank"] == 1
        assert second.get_s0("atlas", "裙子")["products"][0]["rank"] == 2
    finally:
        engine.dispose()


def test_workflow_retries_interrupted_node_without_recollecting_s0():
    engine, store = build_store()
    events = []

    class InterruptOnce(Runner):
        interrupted = False

        def run(self, spec, context):
            self.events.append(f"{self.node}:{spec.query}")
            if spec.query_id == "q1" and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt("simulated process stop")
            return NodeResult.executed({"node": self.node})

    class RecordingSearch(Search):
        calls = []

        def mixed(self, brand, query):
            self.calls.append((brand, query))
            return super().mixed(brand, query)

    search = RecordingSearch()
    runners = {
        "recall": InterruptOnce("recall", events),
        "reorder": Runner("reorder", events),
        "relevance": Runner("relevance", events),
    }
    try:
        workflow = QueryGovernanceWorkflow(
            search=search,
            store=store,
            checkpoint=store,
            runners=runners,
            action_gateway=Actions(),
            model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
        )
        with pytest.raises(KeyboardInterrupt):
            workflow.run_batch(
                specs(), batch_id="batch-1", data_date=date(2026, 8, 28)
            )
        # S0 plus the relevance stage's fresh Mixed lookups both run before the
        # recall node under the new mainline order.
        assert search.calls == [
            ("atlas", "裙子"),
            ("atlas", "外套"),
            ("atlas", "裙子"),
            ("atlas", "外套"),
        ]

        workflow.run_batch(
            specs(), batch_id="batch-1", data_date=date(2026, 8, 28)
        )

        # S0 and the completed relevance nodes are not re-executed on resume.
        assert search.calls == [
            ("atlas", "裙子"),
            ("atlas", "外套"),
            ("atlas", "裙子"),
            ("atlas", "外套"),
        ]
        assert events.count("recall:裙子") == 2
        assert store.get_checkpoint("batch-1").status == "completed"
    finally:
        engine.dispose()


def test_workflow_retries_failed_s0_on_resume():
    engine, store = build_store()

    class FailOnceSearch(Search):
        attempts = 0

        def mixed(self, brand, query):
            self.attempts += 1
            if query == "裙子" and self.attempts == 1:
                raise RuntimeError("temporary Mixed failure")
            return super().mixed(brand, query)

    search = FailOnceSearch()
    events = []
    try:
        workflow = QueryGovernanceWorkflow(
            search=search,
            store=store,
            checkpoint=store,
            runners={node: Runner(node, events) for node in ("recall", "reorder", "relevance")},
            action_gateway=Actions(),
            model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
        )
        first = workflow.run_batch(
            specs(), batch_id="batch-1", data_date=date(2026, 8, 28)
        )
        assert first["q1"].nodes["s0"].status is NodeStatus.ERROR
        assert store.get_checkpoint("batch-1").status == "completed"

        second = workflow.run_batch(
            specs(), batch_id="batch-1", data_date=date(2026, 8, 28)
        )
        assert second["q1"].nodes["s0"].status is NodeStatus.EXECUTED
        assert store.get_checkpoint("batch-1").status == "completed"
    finally:
        engine.dispose()
