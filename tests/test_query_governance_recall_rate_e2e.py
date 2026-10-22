"""End-to-end workflow test for the 4-stage Query governance mainline with recall-rate detection."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.llm.node_config import QUERY_GOVERNANCE_NODE_KEYS, capture_snapshot
from src.persistence.models import Base
from src.query_governance.async_tasks import (
    QueryGovernanceAsyncTaskRepository,
    QueryGovernanceAsyncTaskService,
)
from src.query_governance.checkpoint import SqlAlchemyCheckpointStore
from src.query_governance.models import (
    NODE_ORDER,
    NodeResult,
    NodeStatus,
    QueryGovernanceActionPlan,
    QueryGovernanceAsyncTask,
    QueryGovernanceRelevanceGuidance,
    QueryGovernanceRelevanceRecord,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
    QuerySpec,
)
from src.query_governance.recall import (
    RecallCheckRunner,
    RecallHistoryBaseline,
    SqlAlchemyRecallHistoryGateway,
)
from src.query_governance.recall_rate import (
    CatalogProduct,
    JevTagSelector,
    RecallRateEvaluator,
)
from src.query_governance.relevance import (
    ImageCacheService,
    InMemoryObjectStorage,
    PillowImageTransformer,
    RelevanceCheckRunner,
    SqlAlchemyRelevanceStore,
)
from src.query_governance.relevance_batch import (
    RelevanceBatchConfig,
    RelevanceBatchRunner,
)
from src.query_governance.workflow import QueryGovernanceWorkflow


class _StubSearch:
    def __init__(self, responses: dict[str, list[dict[str, Any]]], tags_map: dict[str, list[str]] | None = None):
        self.responses = responses
        self.tags_map = tags_map or {}
        self.calls: list[tuple[str, str]] = []

    def mixed(self, brand: str, query: str) -> list[dict[str, Any]]:
        self.calls.append((brand, query))
        return list(self.responses.get(query, []))

    def mixed_with_total(self, brand: str, query: str, *, page_size: int = 16, page_num: int = 1) -> dict[str, Any]:
        self.calls.append((brand, query))
        prods = list(self.responses.get(query, []))
        return {
            "success": True,
            "products": prods,
            "total_count": len(prods),
            "result_complete": True,
            "tags": list(self.tags_map.get(query, [])),
        }

    def search_all_products_with_total(self, brand: str, query: str, page_size: int = 100) -> dict[str, Any]:
        return self.mixed_with_total(brand, query, page_size=page_size)


class _StubActionGateway:
    def __init__(self, search: _StubSearch | None = None):
        self.search = search
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute_and_verify(self, spec: QuerySpec, action: dict[str, Any], context: dict[str, Any]) -> NodeResult:
        self.calls.append((spec.query_id, action))
        if self.search is not None and action.get("type") == "product_blacklist":
            pids = set(action.get("product_ids") or [])
            current = self.search.responses.get(spec.query, [])
            self.search.responses[spec.query] = [p for p in current if p.get("productId") not in pids]
        return NodeResult.executed({"status": "verified", "action": action})


class _RecordingRunner:
    def __init__(self, node: str, events: list[str], return_action: dict[str, Any] | None = None):
        self.node = node
        self.events = events
        self.return_action = return_action

    def run(self, spec: QuerySpec, context: dict[str, Any]) -> NodeResult:
        self.events.append(f"{self.node}:{spec.query}")
        return NodeResult.executed(
            {"node": self.node, "query": spec.query},
            action=self.return_action,
        )


def test_full_workflow_e2e_executes_in_correct_order_and_evaluates_recall_rate():
    """Verify that QueryGovernanceWorkflow runs:
    S0 -> Reorder -> Relevance -> Recall (with LLM recall, tagging, Mixed recheck).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            QueryGovernanceWorkflowRun.__table__,
            QueryGovernanceTask.__table__,
            QueryGovernanceStepStatus.__table__,
            QueryGovernanceActionPlan.__table__,
            QueryGovernanceAsyncTask.__table__,
            QueryGovernanceRelevanceRecord.__table__,
            QueryGovernanceRelevanceGuidance.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    checkpoint_store = SqlAlchemyCheckpointStore(factory)
    relevance_store = SqlAlchemyRelevanceStore(factory)
    async_service = QueryGovernanceAsyncTaskService(
        QueryGovernanceAsyncTaskRepository(factory)
    )

    events: list[str] = []

    # 1. Search setup:
    # S0 & stage 3 mixed returns P1 (dress) and P_BAD (shoes, irrelevant)
    # Stage 4 initial mixed returns P1
    # After tagging P2, post-recheck mixed returns P1 and P2 (100% recall)
    search_responses = {
        "短袖 女士": [
            {"productId": "P1", "productName": "纯棉白色短袖T恤", "rank": 1, "ext": {"bucket": "productIdsByTagsAll"}},
            {"productId": "P_BAD", "productName": "真皮高跟凉鞋", "rank": 2, "ext": {"bucket": "productIdsByTagsAll"}},
        ]
    }
    search = _StubSearch(search_responses, tags_map={"短袖 女士": ["短袖"]})

    # 2. Relevance module:
    relevance_calls: list[str] = []

    class FakeImageFetcher:
        def fetch(self, url: str):
            raise RuntimeError("unused")

    image_cache = ImageCacheService(
        fetcher=FakeImageFetcher(),
        transformer=PillowImageTransformer(),
        storage=InMemoryObjectStorage(),
    )

    class FakeRelevanceRuntime:
        def invoke(self, messages, **kwargs):
            content_str = json.dumps(messages)
            if "P_BAD" in content_str:
                # Stage 3 relevance: P1 is relevant (1), P_BAD is irrelevant (0) -> triggers blacklist
                return json.dumps(
                    {
                        "results": [
                            {
                                "query": "短袖 女士",
                                "relevant": [1, 0],
                                "image_checked": [0, 0],
                                "reasons": {"2": "商品是高跟鞋，不是短袖"},
                            }
                        ]
                    }
                )
            if "P2" in content_str:
                # Stage 4 recall-rate diff relevance: P2 is LLM-only, verified relevant (1)
                return json.dumps(
                    {
                        "results": [
                            {
                                "query": "短袖 女士",
                                "relevant": [1],
                                "image_checked": [0],
                                "reasons": {},
                            }
                        ]
                    }
                )
            return json.dumps({"results": [{"query": "短袖 女士", "relevant": [], "image_checked": [], "reasons": {}}]})

    relevance_runner = RelevanceCheckRunner(
        runtime=FakeRelevanceRuntime(),
        record_store=relevance_store,
        image_cache=image_cache,
    )
    relevance_batch_runner = RelevanceBatchRunner(
        runtime=FakeRelevanceRuntime(),
        record_store=relevance_store,
        image_cache=image_cache,
        config=RelevanceBatchConfig(concurrency=1),
    )

    # 3. LLM Recall & Ops setup:
    class FakeLLMRecall:
        def recall_for_query(self, brand: str, query: str):
            return [
                CatalogProduct(brand=brand, spu="P1", recall_product_id="P1", product_name="纯棉白色短袖T恤"),
                CatalogProduct(brand=brand, spu="P2", recall_product_id="P2", product_name="印花纯棉短袖T恤"),
            ]

    class FakeOpsService:
        def __init__(self):
            self.whitelist_tags_added: list[tuple[str, str, list[str]]] = []
            self.retags: list[tuple[str, str]] = []
            self.search_count = 0

        def search_all_products_with_total(self, brand: str, query: str, page_size: int = 100):
            self.search_count += 1
            if self.search_count == 1:
                # Stage 4 before tagging: search has P1
                return {
                    "success": True,
                    "products": [{"productId": "P1", "productName": "纯棉白色短袖T恤"}],
                    "total_count": 1,
                    "result_complete": True,
                    "tags": ["短袖"],
                }
            # Stage 4 after tagging P2: search returns P1 and P2 -> 100% full recall
            return {
                "success": True,
                "products": [
                    {"productId": "P1", "productName": "纯棉白色短袖T恤"},
                    {"productId": "P2", "productName": "印花纯棉短袖T恤"},
                ],
                "total_count": 2,
                "result_complete": True,
                "tags": ["短袖"],
            }

        def add_whitelist_tags(self, brand: str, product_ids: str, tags: list[str]) -> bool:
            self.whitelist_tags_added.append((brand, product_ids, list(tags)))
            return True

        def retag_selected_products(self, brand: str, product_ids: str) -> bool:
            self.retags.append((brand, product_ids))
            return True

    fake_ops = FakeOpsService()
    recall_rate_evaluator = RecallRateEvaluator(
        search=fake_ops,
        llm_recall=FakeLLMRecall(),
        relevance_runner=relevance_batch_runner,
        relevance_store=relevance_store,
        operational_service=fake_ops,
        jev_selector=JevTagSelector(),
    )

    recall_runner = RecallCheckRunner(
        low_result_threshold=2,
        result_drop_threshold=0.3,
        recall_rate_evaluator=recall_rate_evaluator,
        config_provider=lambda brand: {"query_governance_recall_rate_enabled": True},
    )

    action_gateway = _StubActionGateway(search=search)

    workflow = QueryGovernanceWorkflow(
        search=search,
        store=checkpoint_store,
        checkpoint=checkpoint_store,
        async_tasks=async_service,
        recall_history=SqlAlchemyRecallHistoryGateway(factory),
        runners={
            "reorder": _RecordingRunner("reorder", events),
            "relevance": relevance_runner,
            "recall": recall_runner,
        },
        reorder_batch_runner=None,
        relevance_batch_runner=None,
        action_gateway=action_gateway,
        reorder_enabled=True,
        relevance_enabled=True,
        recall_enabled=True,
        model_config_snapshot=capture_snapshot(node_keys=QUERY_GOVERNANCE_NODE_KEYS),
    )

    spec = QuerySpec("q1", "atlas", "短袖 女士", batch_id="batch-e2e-1")
    batch_result = workflow.run_batch([spec], batch_id="batch-e2e-1", data_date=date(2026, 8, 29))

    run_result = batch_result["q1"]

    # 1. 验证节点状态均执行成功
    assert run_result.nodes["s0"].status is NodeStatus.EXECUTED
    assert run_result.nodes["reorder"].status is NodeStatus.EXECUTED
    assert run_result.nodes["relevance"].status is NodeStatus.EXECUTED
    assert run_result.nodes["recall"].status is NodeStatus.EXECUTED

    # 2. 验证阶段执行顺序符合：s0 -> reorder -> relevance -> recall
    with Session(engine) as db:
        steps = db.scalars(
            select(QueryGovernanceStepStatus).order_by(QueryGovernanceStepStatus.id)
        ).all()
        node_sequence = [step.node_key for step in steps]
        assert node_sequence == ["s0", "reorder", "relevance", "recall"]

    # 3. 验证 Stage 3 relevance 检测出 P_BAD 为不相关，并生成了拉黑动作
    rel_node = run_result.nodes["relevance"]
    assert any(
        cand.get("product_id") == "P_BAD"
        for cand in rel_node.result.get("blacklist_candidates", [])
    )

    # 4. 验证 Stage 4 recall-rate 评估结论：
    # P1 (交集，自动记录相关)
    # P2 (大模型单独召回，通过相关性检测)
    # P2 自动打上白名单标签 ["短袖"]，并触发指定商品重打标
    # 复检后 Mixed 返回 P1 和 P2，召回率达到 100% (is_full_recall is True)
    recall_node = run_result.nodes["recall"]
    recall_rate_data = recall_node.result["recall_rate"]
    assert recall_rate_data["initial_search_recall_rate"] == 0.5  # 1 / 2
    assert recall_rate_data["llm_recall_rate"] == 1.0  # 2 / 2
    assert recall_rate_data["post_search_recall_rate"] == 1.0  # 2 / 2
    assert recall_rate_data["is_full_recall"] is True
    assert recall_rate_data["unrecalled_product_ids"] == []
    assert fake_ops.whitelist_tags_added == [("atlas", "P2", ["短袖"])]
    assert fake_ops.retags == [("atlas", "P2")]

    # 5. 验证相关性双向缓存：P1 和 P2 均已被持久化到 relevance_store，未来任何模块可直接 0 Token 复用
    relevant_ids_in_store = relevance_store.get_relevant_product_ids("atlas", "短袖 女士", ["P1", "P2", "P_BAD"])
    assert relevant_ids_in_store == {"P1", "P2"}

    engine.dispose()
