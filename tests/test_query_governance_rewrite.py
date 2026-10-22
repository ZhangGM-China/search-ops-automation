from dataclasses import dataclass, field

import pytest

from src.query_governance.models import (
    NODE_ORDER,
    NodeStatus,
    QuerySpec,
    NodeResult,
)
from src.query_governance.ports import (
    ActionGateway,
    NodeRunner,
    SearchGateway,
    StateStore,
)
from src.query_governance.workflow import QueryGovernanceWorkflow
from src.query_governance.collection import S0CollectionError, S0Collector


@dataclass
class FakeSearch(SearchGateway):
    calls: list[tuple[str, str]] = field(default_factory=list)
    products: list[dict] | None = None

    def mixed(self, brand: str, query: str) -> list[dict]:
        self.calls.append((brand, query))
        return self.products or [{"product_id": f"{query}-1", "rank": 1}]


@dataclass
class TotalSearch(FakeSearch):
    total: int = 0

    def mixed_with_total(self, brand: str, query: str, *, page_size: int = 16, page_num: int = 1) -> dict:
        self.calls.append((brand, query))
        return {
            "success": True,
            "products": self.products or [{"product_id": f"{query}-1", "rank": 1}],
            "total_count": self.total,
            "result_complete": False,
        }


@dataclass
class FakeStore(StateStore):
    snapshots: dict[tuple[str, str], dict] = field(default_factory=dict)
    s0_failures: list[tuple[str, str, str, str]] = field(default_factory=list)
    writes: list[tuple[str, str, str, NodeResult]] = field(
        default_factory=list
    )

    def get_s0(self, brand: str, query: str):
        return self.snapshots.get((brand, query))

    def save_s0_if_absent(self, brand: str, query: str, snapshot: dict):
        return self.snapshots.setdefault((brand, query), snapshot)

    def save_step(self, brand: str, query: str, node: str, result: NodeResult):
        self.writes.append((brand, query, node, result))

    def save_s0_failure(
        self,
        brand: str,
        query: str,
        error_code: str,
        error_message: str,
    ):
        self.s0_failures.append((brand, query, error_code, error_message))


class RecordingNode(NodeRunner):
    def __init__(self, node: str, events: list[str], results=None):
        self.node = node
        self.events = events
        self.results = results or {}

    def run(self, spec, context):
        self.events.append(f"{self.node}:{spec.query}")
        return self.results.get(
            spec.query, NodeResult.executed({"node": self.node})
        )


class NoopActionGateway(ActionGateway):
    def __init__(self):
        self.calls = []

    def execute_and_verify(self, spec, action, context):
        self.calls.append((spec.query_id, action))
        return NodeResult.executed({"action": action})


def build_workflow(events=None, store=None, search=None, runners=None):
    events = events if events is not None else []
    store = store or FakeStore()
    search = search or FakeSearch()
    runners = runners or {
        "recall": RecordingNode("recall", events),
        "reorder": RecordingNode("reorder", events),
        "relevance": RecordingNode("relevance", events),
    }
    return QueryGovernanceWorkflow(
        search=search,
        store=store,
        runners=runners,
        action_gateway=NoopActionGateway(),
    ), store, search, events


def test_new_workflow_exposes_fixed_mainline_order_without_legacy_loop():
    assert NODE_ORDER == ("s0", "reorder", "relevance", "recall")
    assert (
        QueryGovernanceWorkflow.__module__
        == "src.query_governance.workflow"
    )


def test_batch_runs_reorder_barrier_before_any_relevance_node():
    workflow, _, _, events = build_workflow()
    workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子"), QuerySpec("q2", "atlas", "外套")]
    )

    reorder_positions = [
        i for i, event in enumerate(events) if event.startswith("reorder:")
    ]
    relevance_positions = [
        i for i, event in enumerate(events) if event.startswith("relevance:")
    ]
    assert reorder_positions and relevance_positions
    assert max(reorder_positions) < min(relevance_positions)


def test_disabled_recall_records_skip_without_running_recall_node():
    workflow, _, _, events = build_workflow()
    workflow.recall_enabled = False

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])

    assert not any(event.startswith("recall:") for event in events)
    assert result["q1"].nodes["recall"].status is NodeStatus.SKIPPED


def test_reorder_skips_query_without_ctr_or_cvr_historical_decline():
    events = []
    workflow, _, _, _ = build_workflow(events=events)

    result = workflow.run_batch(
        [
            QuerySpec(
                "q-normal",
                "atlas",
                "裙子",
                reorder_eligible=False,
                reorder_skip_reason="CTR/CVR 正常",
            ),
            QuerySpec(
                "q-decline",
                "atlas",
                "外套",
                reorder_eligible=True,
            ),
        ]
    )

    assert result["q-normal"].nodes["reorder"].status is NodeStatus.SKIPPED
    assert result["q-normal"].nodes["reorder"].result == {
        "reason": "CTR/CVR 正常"
    }
    assert result["q-decline"].nodes["reorder"].status is NodeStatus.SKIPPED
    assert result["q-decline"].nodes["reorder"].result == {"reason": "S0 召回商品数≤4"}
    assert "reorder:裙子" not in events


def test_reorder_without_explicit_eligibility_keeps_direct_workflow_compatibility():
    workflow, _, _, events = build_workflow()

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])

    assert result["q1"].nodes["reorder"].status is NodeStatus.EXECUTED
    assert "reorder:裙子" in events


def test_brand_reorder_disabled_skips_node_at_workflow_boundary():
    workflow, _, _, events = build_workflow()
    workflow.reorder_enabled = False

    result = workflow.run_batch([QuerySpec("q1", "summit", "帐篷", reorder_eligible=True)])

    assert result["q1"].nodes["reorder"].status is NodeStatus.SKIPPED
    assert result["q1"].nodes["reorder"].result == {"reason": "品牌未启用重排序"}
    assert not any(event.startswith("reorder:") for event in events)


def test_single_query_error_is_terminal_and_does_not_block_other_queries():
    events = []
    runners = {
        "recall": RecordingNode("recall", events),
        "reorder": RecordingNode(
            "reorder",
            events,
            {"裙子": NodeResult.error("REORDER_FAILED", "排序接口失败")},
        ),
        "relevance": RecordingNode("relevance", events),
    }
    workflow, store, _, _ = build_workflow(events=events, runners=runners)
    result = workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子"), QuerySpec("q2", "atlas", "外套")]
    )

    assert result["q1"].nodes["reorder"].status is NodeStatus.ERROR
    assert result["q2"].nodes["relevance"].status is NodeStatus.EXECUTED
    assert any(
        query == "外套" and node == "relevance"
        for _, query, node, _ in store.writes
    )


def test_s0_is_idempotent_and_never_overwritten():
    workflow, store, search, _ = build_workflow()
    spec = QuerySpec("q1", "atlas", "裙子")

    first = workflow.run_batch([spec])["q1"].s0
    first["products"][0]["rank"] = 99
    workflow.run_batch([spec])

    # The first call creates S0 once; each workflow run still performs the
    # required fresh Mixed lookup for relevance.
    assert len(search.calls) == 3
    assert store.snapshots[("atlas", "裙子")]["products"][0]["rank"] == 99


def test_node_action_uses_one_execution_and_verification_gateway():
    events = []
    action_gateway = NoopActionGateway()
    runners = {
        "recall": RecordingNode("recall", events),
        "reorder": RecordingNode(
            "reorder",
            events,
            {
                "裙子": NodeResult.executed(
                    {"proposal": "sort"}, action={"type": "sort"}
                )
            },
        ),
        "relevance": RecordingNode("relevance", events),
    }
    workflow, _, _, _ = build_workflow(events=events, runners=runners)
    workflow.action_gateway = action_gateway

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])

    assert action_gateway.calls == [("q1", {"type": "sort"})]
    assert result["q1"].nodes["reorder"].result == {"action": {"type": "sort"}}


def test_s0_collection_finishes_for_every_query_before_any_governance_node():
    events = []

    @dataclass
    class OrderedSearch(SearchGateway):
        def mixed(self, brand: str, query: str) -> list[dict]:
            events.append(f"mixed:{query}")
            return [{"product_id": f"{query}-1", "rank": 1}]

    class OrderedNode(RecordingNode):
        def run(self, spec, context):
            events.append(f"{self.node}:{spec.query}")
            return super().run(spec, context)

    workflow, _, _, _ = build_workflow(
        events=events,
        search=OrderedSearch(),
        runners={
            "recall": OrderedNode("recall", events),
            "reorder": OrderedNode("reorder", events),
            "relevance": OrderedNode("relevance", events),
        },
    )

    workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子"), QuerySpec("q2", "atlas", "外套")]
    )

    assert events[:2] == ["mixed:裙子", "mixed:外套"]
    assert events[2].startswith("reorder:")


def test_s0_collection_only_uses_mixed_and_s0_store():
    search = FakeSearch()
    store = FakeStore()
    collector = S0Collector(search=search, store=store)

    snapshot = collector.collect(QuerySpec("q1", "atlas", "裙子"))

    assert snapshot["products"] == [{
        "product_id": "裙子-1",
        "rank": 1,
        "product_name": None,
        "image_url": None,
        "original_price": None,
        "discount_price": None,
    }]
    assert search.calls == [("atlas", "裙子")]
    assert list(store.snapshots) == [("atlas", "裙子")]
    assert store.writes == []


def test_s0_collection_persists_authoritative_mixed_total_separately_from_top_n_products():
    search = TotalSearch(products=[{"product_id": "p1", "rank": 1}], total=102)
    snapshot = S0Collector(search=search, store=FakeStore()).collect(QuerySpec("q1", "atlas", "裙子"))

    assert len(snapshot["products"]) == 1
    assert snapshot["result_count"] == 102
    assert snapshot["result_count_source"] == "mixed_total"
    assert snapshot["result_complete"] is False


def test_recall_accepts_a_trusted_total_for_a_top_n_s0_page():
    from src.query_governance.recall import RecallCheckRunner

    result = RecallCheckRunner(low_result_threshold=5).run(
        QuerySpec("q1", "atlas", "裙子"),
        {"s0": {"result_count": 102, "result_count_known": True, "result_complete": False, "products": [{"product_id": "p1"}]}},
    )

    assert result.status.value == "executed"
    assert result.result["evidence"]["result_count"] == 102


def test_s0_snapshot_only_keeps_normalized_display_fields():
    search = FakeSearch(products=[
        {
            "productId": "PRODUCT-1",
            "skuId": "PRODUCT-1",
            "spuId": "SPU-1",
            "productName": "黑色连衣裙",
            "mainImage": "https://img.example/dress.jpg",
            "originalPrice": "399.00",
            "discountPrice": 299,
            "tagName": "不应进入 S0",
            "description": "不应进入 S0",
        },
        {
            "product_id": "PRODUCT-2",
            "name": "白色衬衫",
            "imageUrl": "https://img.example/shirt.jpg",
            "price": 199,
        },
    ])
    collector = S0Collector(search=search, store=FakeStore())

    snapshot = collector.collect(QuerySpec("q1", "atlas", "裙子"))

    assert snapshot["products"] == [
        {
            "product_id": "PRODUCT-1",
            "spu_id": "SPU-1",
            "rank": 1,
            "product_name": "黑色连衣裙",
            "image_url": "https://img.example/dress.jpg",
            "original_price": 399,
            "discount_price": 299,
        },
        {
            "product_id": "PRODUCT-2",
            "rank": 2,
            "product_name": "白色衬衫",
            "image_url": "https://img.example/shirt.jpg",
            "original_price": 199,
            "discount_price": 199,
        },
    ]
    assert set(snapshot) == {
        "captured_at",
        "products",
        "result_count",
        "result_count_known",
        "result_complete",
        "result_count_source",
        "request_status",
        "recall_product_ids",
        "recall_identity_complete",
    }


def test_relevance_uses_fresh_mixed_results_without_s0_context():
    class SequencedSearch(FakeSearch):
        def mixed(self, brand: str, query: str) -> list[dict]:
            self.calls.append((brand, query))
            product_id = (
                "S0-PRODUCT" if len(self.calls) == 1 else "LIVE-PRODUCT"
            )
            return [{"productId": product_id, "rank": 1, "ext": {"bucket": "productIdsByCode"}}]

    contexts: dict[str, dict] = {}

    class ContextNode(RecordingNode):
        def run(self, spec, context):
            contexts[self.node] = dict(context)
            return super().run(spec, context)

    events: list[str] = []
    search = SequencedSearch()
    workflow, _, _, _ = build_workflow(
        events=events,
        search=search,
        runners={
            "recall": ContextNode("recall", events),
            "reorder": ContextNode("reorder", events),
            "relevance": ContextNode("relevance", events),
        },
    )

    workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])

    recall_product = contexts["recall"]["s0"]["products"][0]
    assert recall_product["product_id"] == "S0-PRODUCT"
    assert "s0" not in contexts["reorder"]
    assert "s0" not in contexts["relevance"]
    assert contexts["relevance"]["current_products"] == [
        {"productId": "LIVE-PRODUCT", "rank": 1, "ext": {"bucket": "productIdsByCode"}}
    ]
    assert search.calls == [
        ("atlas", "裙子"),
        ("atlas", "裙子"),
    ]


def test_s0_collection_rejects_a_product_without_an_identifier():
    search = FakeSearch(products=[{"productName": "缺少 ID"}])
    store = FakeStore()
    collector = S0Collector(search=search, store=store)

    with pytest.raises(S0CollectionError) as raised:
        collector.collect(QuerySpec("q1", "atlas", "裙子"))

    assert raised.value.error_code == "S0_PRODUCT_ID_MISSING"
    assert store.s0_failures == [
        (
            "atlas",
            "裙子",
            "S0_PRODUCT_ID_MISSING",
            "Mixed result at rank 1 has no product identifier",
        )
    ]


def test_relevance_search_failure_skips_the_relevance_runner():
    class RelevanceSearch(FakeSearch):
        def mixed(self, brand: str, query: str) -> list[dict]:
            self.calls.append((brand, query))
            if len(self.calls) == 2:
                raise RuntimeError("live Mixed unavailable")
            return [{"productId": "P-1", "rank": 1}]

    events: list[str] = []
    workflow, store, _, _ = build_workflow(
        events=events,
        search=RelevanceSearch(),
    )

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])["q1"]

    assert result.nodes["relevance"].status is NodeStatus.ERROR
    assert result.nodes["relevance"].error_code == "RELEVANCE_SEARCH_FAILED"
    assert "relevance:裙子" not in events
    writes = [(query, node) for _, query, node, _ in store.writes]
    assert ("裙子", "relevance") in writes
    assert writes == [
        ("裙子", "s0"),
        ("裙子", "reorder"),
        ("裙子", "relevance"),
        ("裙子", "recall"),
    ]


def test_s0_failure_closes_downstream_nodes_without_running_them():
    class FailingSearch(FakeSearch):
        def mixed(self, brand: str, query: str) -> list[dict]:
            self.calls.append((brand, query))
            raise RuntimeError("Mixed unavailable")

    events = []
    workflow, store, _, _ = build_workflow(
        events=events,
        search=FailingSearch(),
    )

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])["q1"]

    assert result.nodes["s0"].status is NodeStatus.ERROR
    assert result.nodes["recall"].status is NodeStatus.SKIPPED
    assert result.nodes["reorder"].status is NodeStatus.SKIPPED
    assert result.nodes["relevance"].status is NodeStatus.SKIPPED
    assert store.snapshots == {}
    assert store.s0_failures == [
        ("atlas", "裙子", "MIXED_FAILED", "Mixed unavailable")
    ]
    assert [(query, node) for _, query, node, _ in store.writes] == [
        ("裙子", "s0"),
        ("裙子", "reorder"),
        ("裙子", "relevance"),
        ("裙子", "recall"),
    ]
    assert events == []


def test_failed_s0_request_remains_retryable():
    class RetryableSearch(FakeSearch):
        attempts = 0

        def mixed(self, brand: str, query: str) -> list[dict]:
            self.calls.append((brand, query))
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("temporary Mixed failure")
            return [{"product_id": "dress-1", "rank": 1}]

    search = RetryableSearch()
    store = FakeStore()
    collector = S0Collector(search=search, store=store)
    spec = QuerySpec("q1", "atlas", "裙子")

    try:
        collector.collect(spec)
    except RuntimeError:
        pass

    snapshot = collector.collect(spec)

    assert snapshot["request_status"] == "success"
    assert len(search.calls) == 2
