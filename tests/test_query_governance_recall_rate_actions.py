"""Recall-rate remediation execution-mode tests: switch, gateway, defer, node gating."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.persistence.models import Base
from src.query_governance.models import (
    NodeResult,
    NodeStatus,
    QueryGovernanceActionPlan,
    QueryGovernanceTask,
    QuerySpec,
)
from src.query_governance.recall import RecallCheckRunner
from src.query_governance.recall_rate import (
    CatalogProduct,
    JevTagSelector,
    RecallRateEvaluator,
)
from src.query_governance.recall_rate_actions import (
    RecallRateActionGateway,
    RecallRateActionPlanRepository,
    execute_pending_recall_rate_action,
    recall_rate_action_mode,
)


class _FakeExecutionContext:
    external_request_id = "req-test-1"
    transaction = None


class _FakeFactGateway:
    def __init__(self):
        self.intents: list[Any] = []

    def execute(self, intent, executor, **kwargs):
        self.intents.append(intent)
        return executor(_FakeExecutionContext())


class _FakeOperational:
    def __init__(self):
        self.tag_writes: list[tuple[str, list[str]]] = []
        self.wl_writes: list[tuple[str, list[str]]] = []
        self.retags: list[str] = []
        self.search_count = 0

    def search_all_products_with_total(self, brand: str, query: str, page_size: int = 100):
        self.search_count += 1
        if self.search_count == 1:
            return {
                "success": True,
                "products": [{"productId": "P1"}],
                "total_count": 1,
                "result_complete": True,
                "tags": ["连衣裙"],
            }
        return {
            "success": True,
            "products": [{"productId": "P1"}, {"productId": "P2"}],
            "total_count": 2,
            "result_complete": True,
            "tags": ["连衣裙"],
        }

    def add_whitelist_tags(self, brand, product_ids, tags, external_request_id=None, **kwargs):
        self.tag_writes.append((product_ids, list(tags)))
        return True

    def add_to_whitelist(self, brand, product_ids, whitelist_words, external_request_id=None, **kwargs):
        self.wl_writes.append((product_ids, list(whitelist_words)))
        return True

    def retag_selected_products(self, brand, product_ids, external_request_id=None, **kwargs):
        self.retags.append(product_ids)
        return True


class _FakeLLMRecall:
    def recall_for_query(self, brand: str, query: str):
        return [
            CatalogProduct(brand=brand, spu="P1", recall_product_id="P1", product_name="商品一"),
            CatalogProduct(brand=brand, spu="P2", recall_product_id="P2", product_name="商品二"),
        ]


class _FakeRelevanceRunner:
    record_store = None

    def run(self, spec, context):
        return NodeResult.executed(
            {"reused_product_ids": [], "relevant_product_ids": ["P2"]}
        )


class _StubGateway:
    def __init__(self):
        self.executed: list[dict[str, Any]] = []
        self.deferred: list[dict[str, Any]] = []
        self.plan_seq = 0

    def execute_and_verify(self, spec, action, context, **kwargs):
        self.executed.append(dict(action))
        self.plan_seq += 1
        return NodeResult.executed(
            {"gateway": {"status": "success"}, "plan_id": 100 + self.plan_seq}
        )

    def defer(self, spec, action, context):
        self.deferred.append(dict(action))
        self.plan_seq += 1
        return NodeResult.executed(
            {"status": "pending_approval", "plan_id": 200 + self.plan_seq}
        )


def _spec() -> QuerySpec:
    return QuerySpec("q1", "atlas", "红色连衣裙", batch_id="batch-1")


def _make_evaluator(ops: _FakeOperational, gateway, config) -> RecallRateEvaluator:
    return RecallRateEvaluator(
        search=ops,
        llm_recall=_FakeLLMRecall(),
        relevance_runner=_FakeRelevanceRunner(),
        operational_service=ops,
        jev_selector=JevTagSelector(),
        action_gateway=gateway,
        config_provider=lambda brand: config,
    )


def test_recall_rate_action_mode_resolution():
    assert recall_rate_action_mode({}, "whitelist_tag") == "auto"
    assert (
        recall_rate_action_mode(
            {"top_query_tool_modes": {"whitelist_tag": "human_approval"}},
            "whitelist_tag",
        )
        == "human_approval"
    )
    assert (
        recall_rate_action_mode(
            {"top_query_tool_modes": {"query_whitelist": "disabled"}},
            "query_whitelist",
        )
        == "disabled"
    )
    assert (
        recall_rate_action_mode({"top_query_tool_modes": {"query_whitelist": 7}}, "query_whitelist")
        == "auto"
    )


def test_evaluator_human_approval_defers_and_reports_pending():
    ops = _FakeOperational()
    gateway = _StubGateway()
    evaluator = _make_evaluator(
        ops,
        gateway,
        {
            "query_governance_recall_rate_enabled": True,
            "top_query_tool_modes": {
                "whitelist_tag": "human_approval",
                "query_whitelist": "human_approval",
            },
        },
    )
    report = evaluator.evaluate(_spec(), {})
    assert gateway.deferred and not gateway.executed
    assert ops.tag_writes == [] and ops.wl_writes == []
    entry = report["tagging_actions"][0]
    assert entry["execution_mode"] == "human_approval"
    assert entry["status"] == "pending_approval"
    assert entry["plan_id"] is not None
    assert report["pending_action_count"] == 1
    assert report["is_full_recall"] is False
    assert "待人工审批" in str(report["operator_alert"])
    # Deferred actions wrote nothing yet: no immediate re-fetch of Mixed.
    assert ops.search_count == 1


def test_evaluator_disabled_mode_records_proposal_only():
    ops = _FakeOperational()
    gateway = _StubGateway()
    evaluator = _make_evaluator(
        ops,
        gateway,
        {
            "query_governance_recall_rate_enabled": True,
            "top_query_tool_modes": {
                "whitelist_tag": "disabled",
                "query_whitelist": "disabled",
            },
        },
    )
    report = evaluator.evaluate(_spec(), {})
    assert gateway.deferred == [] and gateway.executed == []
    assert ops.tag_writes == [] and ops.wl_writes == []
    entry = report["tagging_actions"][0]
    assert entry["status"] == "record_only"
    assert entry["execution_mode"] == "disabled"


def test_evaluator_auto_mode_executes_via_gateway_and_rechecks():
    ops = _FakeOperational()
    gateway = _StubGateway()
    evaluator = _make_evaluator(
        ops,
        gateway,
        {
            "query_governance_recall_rate_enabled": True,
            "top_query_tool_modes": {"whitelist_tag": "auto"},
        },
    )
    report = evaluator.evaluate(_spec(), {})
    assert len(gateway.executed) == 1
    assert gateway.executed[0]["type"] == "whitelist_tag"
    entry = report["tagging_actions"][0]
    assert entry["execution_mode"] == "auto"
    assert entry["status"] == "success"
    assert entry["plan_id"] is not None
    # Executed actions trigger the post-tagging Mixed recheck.
    assert ops.search_count == 2
    assert report["is_full_recall"] is True


def test_recall_runner_skips_evaluator_when_brand_disabled():
    calls: list[str] = []

    class _CountingEvaluator:
        def evaluate(self, spec, context):
            calls.append(spec.query)
            return {}

    runner = RecallCheckRunner(
        low_result_threshold=5,
        result_drop_threshold=0.3,
        recall_rate_evaluator=_CountingEvaluator(),
    )
    spec = _spec()
    outcome = runner.run(spec, {"s0": {"request_status": "success", "result_count": 3}})
    assert calls == []
    assert (
        outcome.result["evidence"]["recall_rate_status"] == "品牌未启用AI召回率检测"
    )


def test_recall_runner_notes_unavailable_when_enabled_without_evaluator():
    runner = RecallCheckRunner(
        low_result_threshold=5,
        result_drop_threshold=0.3,
        config_provider=lambda brand: {"query_governance_recall_rate_enabled": True},
    )
    outcome = runner.run(
        _spec(),
        {
            "s0": {"request_status": "success", "result_count": 3},
            "recall_rate_evaluator": None,
        },
    )
    assert (
        outcome.result["evidence"]["recall_rate_status"]
        == "AI召回率检测已启用但目录数据源未配置"
    )


def _plan_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            QueryGovernanceTask.__table__,
            QueryGovernanceActionPlan.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    spec = _spec()
    with factory() as session:
        task = QueryGovernanceTask(
            batch_id=spec.batch_id,
            brand=spec.brand,
            query_text=spec.query,
            data_date=__import__("datetime").date(2026, 10, 14),
        )
        session.add(task)
        session.commit()
    return factory, spec


def test_gateway_defer_persists_pending_plan():
    factory, spec = _plan_factory()
    repository = RecallRateActionPlanRepository(factory)
    gateway = RecallRateActionGateway(
        operational_service=_FakeOperational(),
        fact_gateway=_FakeFactGateway(),
        action_plan_repository=repository,
    )
    outcome = gateway.defer(
        spec,
        {"type": "whitelist_tag", "tag": "连衣裙", "product_ids": ["P2"]},
        {},
    )
    assert outcome.status is not NodeStatus.ERROR
    plan_id = outcome.result["plan_id"]
    with factory() as session:
        row = session.get(QueryGovernanceActionPlan, plan_id)
        assert row is not None
        assert row.action_type == "whitelist_tag"
        assert row.node_key == "recall"
        assert row.status == "pending"
        assert row.executor_type == "human"


def test_gateway_auto_executes_write_and_verifies_plan():
    factory, spec = _plan_factory()
    ops = _FakeOperational()
    repository = RecallRateActionPlanRepository(factory)
    gateway = RecallRateActionGateway(
        operational_service=ops,
        fact_gateway=_FakeFactGateway(),
        action_plan_repository=repository,
    )
    outcome = gateway.execute_and_verify(
        spec,
        {"type": "whitelist_tag", "tag": "连衣裙", "product_ids": ["P2"]},
        {},
    )
    assert outcome.status is not NodeStatus.ERROR
    assert ops.tag_writes == [("P2", ["连衣裙"])]
    assert ops.retags == ["P2"]
    plan_id = outcome.result["plan_id"]
    with factory() as session:
        row = session.get(QueryGovernanceActionPlan, plan_id)
        assert row is not None
        assert row.status == "verified"


def test_execute_pending_recall_rate_action_uses_human_executor():
    factory, spec = _plan_factory()
    repository = RecallRateActionPlanRepository(factory)
    pending_id = repository.create_pending(
        spec, {"type": "query_whitelist", "query": spec.query, "product_ids": ["P3"]}
    )
    assert pending_id is not None

    received: dict[str, Any] = {}

    class _RecordingGateway:
        def execute_and_verify(self, plan_spec, action, context, **kwargs):
            received.update(kwargs)
            received["action"] = dict(action)
            return NodeResult.executed({"gateway": {"status": "success"}})

    result = execute_pending_recall_rate_action(
        pending_id,
        session_factory=factory,
        operational_service=_FakeOperational(),
        operator="alice",
        user_id="42",
        gateway=_RecordingGateway(),
    )
    assert result["status"] == "success"
    assert result["action_type"] == "query_whitelist"
    assert received["plan_id"] == pending_id
    assert received["executor_type"] == "human"
    assert received["executor_user_id"] == "42"
    assert received["action"]["product_ids"] == ["P3"]


def test_execute_pending_recall_rate_action_rejects_wrong_status():
    factory, spec = _plan_factory()
    repository = RecallRateActionPlanRepository(factory)
    pending_id = repository.create_pending(
        spec, {"type": "whitelist_tag", "tag": "连衣裙", "product_ids": ["P2"]}
    )
    repository.record_result(pending_id, status="verified")
    with pytest.raises(ValueError):
        execute_pending_recall_rate_action(
            pending_id,
            session_factory=factory,
            operational_service=_FakeOperational(),
            operator="alice",
            gateway=_StubGateway(),
        )
