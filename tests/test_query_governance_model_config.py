from dataclasses import dataclass, field
from unittest.mock import Mock, patch

import pytest

from src.llm.node_config import (
    QUERY_GOVERNANCE_NODE_KEYS,
    capture_snapshot,
    model_capabilities,
)
from src.llm.config_store import capture_active_snapshot, create_draft, publish_draft
from src.persistence import Base, SessionLocal, reset_engine_for_testing
from src.query_governance.models import (
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
)
from src.query_governance.ports import ActionGateway, NodeRunner, SearchGateway, StateStore
from src.query_governance.models import NodeResult, QuerySpec
from src.query_governance.workflow import QueryGovernanceWorkflow
from src.query_governance.model_runtime import QueryGovernanceModelRuntime
from src.query_governance.state import (
    NodeStatePersistenceError,
    SqlAlchemyNodeStateStore,
)
from src.query_governance.checkpoint import CheckpointError, SqlAlchemyCheckpointStore
from src.llm.rd_client import FallbackChatModel


def test_new_query_governance_nodes_are_registered_with_relevance_capability():
    assert QUERY_GOVERNANCE_NODE_KEYS == (
        "query_governance.kg_agent",
        "query_governance.preference_generation",
        "query_governance.relevance_multimodal",
        "query_governance.relevance_rules",
    )

    snapshot = capture_snapshot(node_keys=QUERY_GOVERNANCE_NODE_KEYS)
    assert snapshot.nodes["query_governance.kg_agent"].channel == "rd_llm"
    assert snapshot.nodes["query_governance.relevance_multimodal"].primary_model


def test_relevance_multimodal_accepts_custom_model_names():
    snapshot = capture_snapshot(
        node_keys=("query_governance.relevance_multimodal",),
        published={
            "query_governance.relevance_multimodal": {
                "primaryModel": "deepseek-v4.1-flash-1017",
                "fallbackModel": "custom-vision-fallback",
            }
        },
    )

    config = snapshot.nodes["query_governance.relevance_multimodal"]
    assert config.primary_model == "deepseek-v4.1-flash-1017"
    assert config.fallback_model == "custom-vision-fallback"


def test_qwen35_plus_is_registered_as_multimodal_model():
    assert "image" in model_capabilities("qwen3.5-plus")


def test_current_rd_flash_models_are_registered_as_multimodal_models():
    assert "image" in model_capabilities("deepseek-v4.1-flash")
    assert "image" in model_capabilities("qwen3.8-flash")


def test_relevance_defaults_use_models_verified_by_the_live_benchmark():
    config = capture_snapshot(
        node_keys=("query_governance.relevance_multimodal",)
    ).nodes["query_governance.relevance_multimodal"]

    assert config.primary_model == "qwen3.7-plus"
    assert config.fallback_model == "qwen3.6-plus"


def _entries():
    from src.llm.node_config import NODE_REGISTRY

    return {
        node_key: {
            "channel": spec.channel,
            "primaryModel": spec.default_primary,
            "fallbackModel": spec.default_fallback,
        }
        for node_key, spec in NODE_REGISTRY.items()
    }


def test_active_snapshot_carries_published_version():
    reset_engine_for_testing()
    Base.metadata.create_all(bind=SessionLocal.kw["bind"])
    db = SessionLocal()
    try:
        draft = create_draft(db, _entries())
        publish_draft(db, draft.version, expected_version=None)
        snapshot = capture_active_snapshot(db, node_keys=QUERY_GOVERNANCE_NODE_KEYS)
    finally:
        db.close()

    assert snapshot.version == draft.version


@dataclass
class _Search(SearchGateway):
    def mixed(self, brand: str, query: str):
        return [{"product_id": "p1", "rank": 1}]


@dataclass
class _Store(StateStore):
    versions: list[tuple[str, int]] = field(default_factory=list)
    steps: list[tuple[str, str, NodeResult]] = field(default_factory=list)

    def get_s0(self, brand: str, query: str):
        return None

    def save_s0_if_absent(self, brand: str, query: str, snapshot):
        return snapshot

    def save_step(self, brand: str, query: str, node: str, result: NodeResult):
        self.steps.append((query, node, result))

    def save_model_config_version(self, batch_id: str, version: int):
        self.versions.append((batch_id, version))


class _Runner(NodeRunner):
    def run(self, spec, context):
        assert context["model_config_snapshot"].version == 9
        return NodeResult.executed()


class _Actions(ActionGateway):
    def execute_and_verify(self, spec, action, context):
        return NodeResult.executed()


def test_workflow_persists_one_model_config_version_and_passes_snapshot_to_nodes():
    store = _Store()
    snapshot = capture_snapshot(
        node_keys=QUERY_GOVERNANCE_NODE_KEYS,
    )
    snapshot = snapshot.__class__(
        captured_at=snapshot.captured_at,
        nodes=snapshot.nodes,
        version=9,
    )
    workflow = QueryGovernanceWorkflow(
        search=_Search(),
        store=store,
        runners={key: _Runner() for key in ("recall", "reorder", "relevance")},
        action_gateway=_Actions(),
        model_config_snapshot=snapshot,
    )

    workflow.run_batch([QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")])

    assert store.versions == [("batch-1", 9)]


def test_each_new_batch_captures_the_current_published_snapshot_once():
    store = _Store()
    snapshots = [
        capture_snapshot(node_keys=QUERY_GOVERNANCE_NODE_KEYS, version=3),
        capture_snapshot(node_keys=QUERY_GOVERNANCE_NODE_KEYS, version=4),
    ]
    provider = Mock(side_effect=snapshots)
    class _SnapshotRunner(NodeRunner):
        def run(self, spec, context):
            assert context["model_config_snapshot"] is not None
            return NodeResult.executed()

    workflow = QueryGovernanceWorkflow(
        search=_Search(),
        store=store,
        runners={key: _SnapshotRunner() for key in ("recall", "reorder", "relevance")},
        action_gateway=_Actions(),
        model_config_provider=provider,
    )

    workflow.run_batch([QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")])
    workflow.run_batch([QuerySpec("q2", "atlas", "外套", batch_id="batch-2")])

    assert provider.call_count == 2
    assert store.versions == [("batch-1", 3), ("batch-2", 4)]


def test_sqlalchemy_store_pins_and_reads_back_one_model_version_per_batch():
    reset_engine_for_testing()
    Base.metadata.create_all(bind=SessionLocal.kw["bind"])
    store = SqlAlchemyNodeStateStore(
        SessionLocal,
        task_model=QueryGovernanceTask,
        step_model=QueryGovernanceStepStatus,
        workflow_run_model=QueryGovernanceWorkflowRun,
    )

    store.save_model_config_version("batch-model-version", 7)
    store.save_model_config_version("batch-model-version", 7)

    db = SessionLocal()
    try:
        run = db.query(QueryGovernanceWorkflowRun).filter_by(
            batch_id="batch-model-version"
        ).one()
        assert run.model_config_version == 7
    finally:
        db.close()

    with pytest.raises(NodeStatePersistenceError, match="already pinned"):
        store.save_model_config_version("batch-model-version", 8)


def test_checkpoint_store_pins_model_version_after_batch_start():
    reset_engine_for_testing()
    Base.metadata.create_all(bind=SessionLocal.kw["bind"])
    store = SqlAlchemyCheckpointStore(SessionLocal)
    spec = QuerySpec("q1", "atlas", "裙子", batch_id="batch-checkpoint-model-version")

    store.start_batch(spec.batch_id, [spec], data_date=__import__("datetime").date(2026, 9, 3))
    store.save_model_config_version(spec.batch_id, 7)

    db = SessionLocal()
    try:
        run = db.query(QueryGovernanceWorkflowRun).filter_by(
            batch_id=spec.batch_id
        ).one()
        assert run.model_config_version == 7
    finally:
        db.close()

    with pytest.raises(CheckpointError, match="already pinned"):
        store.save_model_config_version(spec.batch_id, 8)


def test_model_runtime_records_fallback_model_version_duration_and_tokens():
    class _Model:
        def __init__(self, *, response=None, error=None):
            self.response = response
            self.error = error

        def invoke(self, _messages, **_kwargs):
            if self.error:
                raise self.error
            return self.response

    response = type(
        "Response",
        (),
        {"usage_metadata": {"input_tokens": 8, "output_tokens": 5}},
    )()
    snapshot = capture_snapshot(
        node_keys=("query_governance.preference_generation",),
        published={
            "query_governance.preference_generation": {
                "primaryModel": "primary-model",
                "fallbackModel": "fallback-model",
            }
        },
        version=12,
    )
    llm = FallbackChatModel(
        primary=_Model(error=RuntimeError("primary unavailable")),
        fallback=_Model(response=response),
    )

    result = QueryGovernanceModelRuntime(
        "query_governance.preference_generation",
        snapshot,
        llm=llm,
    ).invoke([{"role": "user", "content": "test"}])

    assert result.response is response
    assert result.audit["modelName"] == "fallback-model"
    assert result.audit["modelRole"] == "fallback"
    assert result.audit["configVersion"] == 12
    assert result.audit["tokenUsage"] == 13
    assert result.audit["durationSeconds"] >= 0
    assert [call["tokenUsage"] for call in result.audit["modelCallAudits"]] == [None, 13]


def test_model_runtime_keeps_token_usage_missing_when_provider_does_not_report_usage():
    class _Model:
        def invoke(self, _messages, **_kwargs):
            return type("Response", (), {"content": "ok"})()

    snapshot = capture_snapshot(
        node_keys=("query_governance.preference_generation",),
        published={
            "query_governance.preference_generation": {
                "primaryModel": "primary-model",
                "fallbackModel": None,
            }
        },
        version=13,
    )
    result = QueryGovernanceModelRuntime(
        "query_governance.preference_generation",
        snapshot,
        llm=FallbackChatModel(primary=_Model(), fallback=None),
    ).invoke([{"role": "user", "content": "test"}])

    assert result.audit["tokenUsage"] is None


def test_model_runtime_builds_the_exact_models_from_the_pinned_snapshot():
    snapshot = capture_snapshot(
        node_keys=("query_governance.kg_agent",),
        published={
            "query_governance.kg_agent": {
                "primaryModel": "configured-primary",
                "fallbackModel": "configured-fallback",
            }
        },
        version=15,
    )
    primary = object()
    fallback = object()

    with patch(
        "src.query_governance.model_runtime.build_rd_chat_model",
        side_effect=[primary, fallback],
    ) as build:
        runtime = QueryGovernanceModelRuntime.from_snapshot(
            "query_governance.kg_agent",
            snapshot,
            timeout=20,
        )

    assert runtime.llm.primary is primary
    assert runtime.llm.fallback is fallback
    assert build.call_args_list[0].kwargs["default_model"] == "configured-primary"
    assert build.call_args_list[1].kwargs["default_model"] == "configured-fallback"
    assert all(call.kwargs["timeout"] == 20 for call in build.call_args_list)


def test_model_runtime_passes_relevance_output_controls_to_model_clients():
    snapshot = capture_snapshot(
        node_keys=("query_governance.relevance_multimodal",),
        published={
            "query_governance.relevance_multimodal": {
                "primaryModel": "qwen-vl-max",
                "fallbackModel": None,
            }
        },
        version=16,
    )
    primary = object()
    with patch(
        "src.query_governance.model_runtime.build_rd_chat_model",
        return_value=primary,
    ) as build:
        QueryGovernanceModelRuntime.from_snapshot(
            "query_governance.relevance_multimodal",
            snapshot,
            timeout=20,
            max_completion_tokens=131072,
            extra_body={"enable_thinking": False},
        )
    assert build.call_args.kwargs["max_completion_tokens"] == 131072
    assert build.call_args.kwargs["extra_body"] == {"enable_thinking": False}
