from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from typing import Any

from src.query_governance.models import NodeResult, NodeStatus, QuerySpec
from src.query_governance.relevance_config import RelevanceRuntimeConfig
from src.query_governance.workflow import QueryGovernanceWorkflow


@dataclass
class _Search:
    responses: list[list[dict[str, Any]]]
    calls: list[tuple[str, str]] = field(default_factory=list)

    def mixed(self, brand: str, query: str):
        self.calls.append((brand, query))
        if self.responses:
            return [
                {
                    **item,
                    "ext": {
                        **(item.get("ext") or {}),
                        "bucket": (item.get("ext") or {}).get("bucket", "productIdsByCode"),
                    },
                }
                for item in self.responses.pop(0)
            ]
        return []


class _Node:
    def __init__(self, result: NodeResult | None = None):
        self.result = result or NodeResult.executed({})
        self.calls: list[str] = []

    def run(self, spec, context):
        self.calls.append(spec.query)
        return self.result


class _RelevanceNode:
    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, spec, context):
        products = list(context["current_products"])
        self.calls.append([str(item["product_id"]) for item in products])
        if len(self.calls) == 1:
            return NodeResult.executed(
                {
                    "requested_product_ids": ["p1", "p2"],
                    "detected_product_ids": ["p1", "p2"],
                    "blacklist_candidates": [
                        {"product_id": "p1", "product_name": "商品一", "reason": "不符合 Query"}
                    ],
                    "relevant_product_ids": ["p2"],
                },
                action={
                    "type": "product_blacklist",
                    "product_ids": ["p1"],
                    "query": spec.query,
                },
            )
        return NodeResult.executed(
            {
                "requested_product_ids": ["p3"],
                "detected_product_ids": ["p3"],
                "blacklist_candidates": [],
                "relevant_product_ids": ["p3"],
            }
        )


class _Actions:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def execute_and_verify(self, spec, action, context):
        self.calls.append(dict(action))
        return NodeResult.executed(
            {"executed": True, "product_ids": action["product_ids"]},
            action=action,
        )


class _DeferredActions(_Actions):
    def __init__(self):
        super().__init__()
        self.deferred: list[dict[str, Any]] = []

    def defer(self, spec, action, context):
        self.deferred.append(dict(action))
        return NodeResult.executed({"status": "pending_approval"}, action=action)


class _Store:
    def __init__(self):
        self.s0: dict[tuple[str, str], dict[str, Any]] = {}
        self.steps: list[tuple[str, str, str, NodeResult]] = []

    def get_s0(self, brand, query):
        return self.s0.get((brand, query))

    def save_s0_if_absent(self, brand, query, snapshot):
        return self.s0.setdefault((brand, query), dict(snapshot))

    def save_s0_failure(self, *args):
        return None

    def save_step(self, brand, query, node, result):
        self.steps.append((brand, query, node, result))


class _AsyncTasks:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def enqueue_for_query(self, **kwargs):
        self.calls.append(kwargs)
        return type("Submission", (), {"status": "pending", "error_code": None, "error_message": None})()


def _workflow(search, relevance, actions, async_tasks=None, **kwargs):
    return QueryGovernanceWorkflow(
        search=search,
        store=_Store(),
        runners={
            "recall": _Node(),
            "reorder": _Node(),
            "relevance": relevance,
        },
        action_gateway=actions,
        async_tasks=async_tasks,
        **kwargs,
    )


def test_relevance_filters_top_n_then_selected_buckets_before_runner():
    class CapturingRelevance(_Node):
        def __init__(self):
            super().__init__(NodeResult.executed({"detected_product_ids": ["p1"]}))
            self.product_ids: list[str] = []

        def run(self, spec, context):
            self.product_ids = [str(item["product_id"]) for item in context["current_products"]]
            return self.result

    search = _Search(
        [
            [{"product_id": "s0"}],
            [
                {"product_id": "p1", "ext": {"bucket": "productIdsByCode"}},
                {"product_id": "p2", "ext": {"bucket": "productIdsByTagsAll"}},
                {"product_id": "p3", "ext": {"bucket": "productIdsByCode"}},
            ],
        ]
    )
    relevance = CapturingRelevance()
    workflow = _workflow(
        search,
        relevance,
        _Actions(),
        relevance_config_provider=lambda _brand: RelevanceRuntimeConfig.from_mapping(
            {
                "top_query_check_product_limit": 2,
                "query_governance_relevance_buckets": ["productIdsByCode"],
            }
        ),
    )

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])

    assert result["q1"].nodes["relevance"].status is NodeStatus.EXECUTED
    assert relevance.product_ids == ["p1"]


def test_relevance_rechecks_only_new_products_after_blacklist():
    search = _Search(
        [
            [{"product_id": "s0"}],
            [{"product_id": "p1"}, {"product_id": "p2"}],
            [{"product_id": "p2"}, {"product_id": "p3"}],
            [{"product_id": "p2"}, {"product_id": "p3"}],
        ]
    )
    relevance = _RelevanceNode()
    actions = _Actions()
    workflow = _workflow(search, relevance, actions)

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])

    assert result["q1"].nodes["relevance"].status is NodeStatus.EXECUTED
    assert relevance.calls == [["p1", "p2"], ["p3"]]
    assert actions.calls == [
        {
            "type": "product_blacklist",
            "product_ids": ["p1"],
            "query": "裙子",
            "product_names": {"p1": "商品一"},
            "product_images": {"p1": ""},
            "reasons": {"p1": "不符合 Query"},
            "execution_mode": "auto",
        }
    ]
    assert search.calls == [("atlas", "裙子")] * 3
    telemetry = result["q1"].nodes["relevance"].telemetry
    assert telemetry["actionSeconds"] >= 0
    assert telemetry["recheckSeconds"] >= 0
    assert len(result["q1"].nodes["relevance"].result["rounds"]) == 2
    assert all(
        item["telemetry"]["roundSeconds"] >= 0
        for item in result["q1"].nodes["relevance"].result["rounds"]
    )


def test_relevance_fails_when_blacklisted_product_remains_in_mixed_results():
    search = _Search(
        [
            [{"product_id": "s0"}],
            [{"product_id": "p1"}],
            [{"product_id": "p1"}],
        ]
    )
    relevance = _RelevanceNode()
    workflow = _workflow(
        search,
        relevance,
        _Actions(),
        relevance_max_rounds=2,
    )

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])["q1"]

    relevance_result = result.nodes["relevance"]
    assert relevance_result.status is NodeStatus.ERROR
    assert relevance_result.error_code == "RELEVANCE_BLACKLIST_SEARCH_EFFECT_NOT_VERIFIED"
    assert relevance_result.result["error"]["code"] == relevance_result.error_code
    assert relevance_result.result["rounds"][-1]["search_effect"] == {
        "status": "failed",
        "observed_product_ids": ["p1"],
    }


def test_any_relevance_blacklist_candidate_enqueues_root_cause_task_without_blocking():
    search = _Search(
        [
            [{"product_id": "s0"}],
            [{"product_id": "p1"}, {"product_id": "p2"}],
            [{"product_id": "p2"}],
        ]
    )
    relevance = _RelevanceNode()
    actions = _Actions()
    async_tasks = _AsyncTasks()
    workflow = _workflow(
        search,
        relevance,
        actions,
        async_tasks,
        relevance_blacklist_ratio_threshold=0.9,
    )

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")], batch_id="batch-1")

    assert result["q1"].nodes["relevance"].status is NodeStatus.EXECUTED
    assert len(async_tasks.calls) == 1
    assert async_tasks.calls[0]["trigger_source"] == "relevance_root_cause"
    assert async_tasks.calls[0]["evidence"]["error_product_ids"] == ["p1"]
    assert async_tasks.calls[0]["evidence"]["causeGroups"][0]["rootCause"] == "uncertain"


def test_independent_relevance_blacklists_before_root_cause_analysis():
    search = _Search([[{"product_id": "s0"}], [{"product_id": "p1"}, {"product_id": "p2"}]])
    actions = _Actions()
    async_tasks = _AsyncTasks()
    workflow = _workflow(
        search, _RelevanceNode(), actions, async_tasks,
        relevance_independent_actions=True,
    )

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")], batch_id="batch-agent")

    assert result["q1"].nodes["relevance"].status is NodeStatus.EXECUTED
    assert result["q1"].nodes["relevance"].action["product_ids"] == ["p1"]
    assert actions.calls[0]["product_ids"] == ["p1"]
    assert len(async_tasks.calls) == 1
    assert async_tasks.calls[0]["trigger_source"] == "relevance_root_cause"


@pytest.mark.parametrize("independent", [False, True])
def test_human_approval_mode_persists_pending_blacklist_without_external_write(independent):
    search = _Search(
        [
            [{"product_id": "s0"}],
            [{"product_id": "p1"}, {"product_id": "p2"}],
        ]
    )
    actions = _DeferredActions()
    async_tasks = _AsyncTasks()
    workflow = _workflow(
        search,
        _RelevanceNode(),
        actions,
        async_tasks,
        relevance_independent_actions=independent,
        relevance_blacklist_execution_mode="human_approval",
    )

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")], batch_id="batch-human")

    assert result["q1"].nodes["relevance"].status is NodeStatus.EXECUTED
    assert actions.calls == []
    assert actions.deferred[0]["execution_mode"] == "human_approval"
    assert result["q1"].nodes["relevance"].result["blacklist_execution_mode"] == "human_approval"
    assert len(async_tasks.calls) == 1


@pytest.mark.parametrize("independent", [False, True])
def test_disabled_mode_only_records_candidates_without_action_write(independent):
    search = _Search(
        [
            [{"product_id": "s0"}],
            [{"product_id": "p1"}, {"product_id": "p2"}],
        ]
    )
    actions = _DeferredActions()
    async_tasks = _AsyncTasks()
    workflow = _workflow(
        search,
        _RelevanceNode(),
        actions,
        async_tasks,
        relevance_independent_actions=independent,
        relevance_blacklist_execution_mode="disabled",
    )

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")], batch_id="batch-disabled")

    assert result["q1"].nodes["relevance"].status is NodeStatus.EXECUTED
    assert actions.calls == []
    assert actions.deferred == []
    assert result["q1"].nodes["relevance"].result["blacklist_execution_mode"] == "disabled"
    assert len(async_tasks.calls) == 1


def test_relevance_blacklist_ratio_persists_query_trace_context():
    search = _Search(
        [
            [{"product_id": "s0"}],
            [{"product_id": "p1"}, {"product_id": "p2"}],
            [{"product_id": "p2"}],
        ]
    )
    relevance = _RelevanceNode()
    actions = _Actions()
    async_tasks = _AsyncTasks()
    observability, _, _ = __import__(
        "tests.test_query_governance_langfuse", fromlist=["_observability"]
    )._observability()
    workflow = _workflow(
        search,
        relevance,
        actions,
        async_tasks,
        relevance_blacklist_ratio_threshold=0.5,
        observability=observability,
    )

    workflow.run_batch([QuerySpec("q1", "atlas", "裙子")], batch_id="batch-1")

    assert async_tasks.calls[0]["langfuse_context"]["batch_trace_id"] == "trace-1"
    assert async_tasks.calls[0]["langfuse_context"]["query_trace_id"] == "trace-2"


def test_relevance_config_exposes_incremental_controls():
    config = RelevanceRuntimeConfig.from_mapping(
        {
            "query_governance_relevance_blacklist_ratio_threshold": 0.6,
            "query_governance_relevance_max_rounds": 4,
            "top_query_check_product_limit": 2,
            "query_governance_relevance_buckets": ["productIdsByCode"],
        }
    )

    assert config.blacklist_ratio_threshold == 0.6
    assert config.max_rounds == 4
    assert config.product_limit == 2
    assert config.buckets == ("productIdsByCode",)


def test_relevance_failure_after_blacklist_keeps_aggregate_evidence():
    class FailingSecondRound(_RelevanceNode):
        def run(self, spec, context):
            if self.calls:
                self.calls.append([str(item["product_id"]) for item in context["current_products"]])
                return NodeResult.error("MODEL_FAILED", "模型不可用")
            return super().run(spec, context)

    search = _Search(
        [
            [{"product_id": "s0"}],
            [{"product_id": "p1"}, {"product_id": "p2"}],
            [{"product_id": "p3"}],
        ]
    )
    relevance = FailingSecondRound()
    workflow = _workflow(search, relevance, _Actions())

    result = workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])["q1"]

    relevance_result = result.nodes["relevance"]
    assert relevance_result.status is NodeStatus.ERROR
    assert relevance_result.result["detected_product_ids"] == ["p1", "p2"]
    assert relevance_result.result["blacklist_ratio"] == 0.5


def test_relevance_does_not_revisit_products_seen_in_a_previous_round():
    class RelevanceWithReuse(_RelevanceNode):
        def run(self, spec, context):
            products = list(context["current_products"])
            self.calls.append([str(item["product_id"]) for item in products])
            if len(self.calls) == 1:
                return NodeResult.executed(
                    {
                        "requested_product_ids": ["p1", "p2"],
                        "reused_product_ids": ["p2"],
                        "detected_product_ids": ["p1"],
                        "blacklist_candidates": [
                            {"product_id": "p1", "reason": "不符合 Query"}
                        ],
                    },
                    action={"type": "product_blacklist", "product_ids": ["p1"]},
                )
            return NodeResult.executed(
                {
                    "requested_product_ids": ["p3"],
                    "detected_product_ids": ["p3"],
                    "blacklist_candidates": [],
                }
            )

    search = _Search(
        [
            [{"product_id": "s0"}],
            [{"product_id": "p1"}, {"product_id": "p2"}],
            [{"product_id": "p2"}, {"product_id": "p3"}],
        ]
    )
    relevance = RelevanceWithReuse()
    workflow = _workflow(search, relevance, _Actions())

    workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])

    assert relevance.calls == [["p1", "p2"], ["p3"]]


def test_relevance_config_failure_isolated_to_current_query():
    def broken_config(_brand):
        raise RuntimeError("config store unavailable")

    search = _Search(
        [
            [{"product_id": "s0-1"}],
            [{"product_id": "p1"}],
            [{"product_id": "s0-2"}],
            [{"product_id": "p2"}],
        ]
    )
    relevance = _RelevanceNode()
    workflow = _workflow(
        search,
        relevance,
        _Actions(),
        relevance_config_provider=broken_config,
    )

    result = workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子"), QuerySpec("q2", "atlas", "外套")]
    )

    assert result["q1"].nodes["relevance"].status is NodeStatus.ERROR
    assert result["q1"].nodes["relevance"].error_code == "RELEVANCE_CONFIG_FAILED"
    assert result["q2"].nodes["relevance"].status is NodeStatus.ERROR


def test_detection_blacklists_all_errors_independently_of_root_cause_enqueue_failure():
    class BrokenTasks(_AsyncTasks):
        def enqueue_for_query(self, **kwargs):
            self.calls.append(kwargs)
            raise ValueError('KG analysis unavailable')

    actions = _Actions()
    tasks = BrokenTasks()
    workflow = _workflow(
        _Search([[{'product_id': 's0'}], [{'product_id': 'p1'}, {'product_id': 'p2'}]]),
        _RelevanceNode(), actions, tasks, relevance_independent_actions=True,
    )
    run = workflow.run_batch([QuerySpec('q1', 'atlas', '裙子')], batch_id='independent')['q1']
    assert actions.calls[0]['product_ids'] == ['p1']
    assert run.nodes['relevance'].status is NodeStatus.EXECUTED
    assert run.nodes['relevance'].result['warnings'][-1]['code'] == 'KG_TASK_ENQUEUE_FAILED'


def test_root_cause_analysis_is_enqueued_even_when_blacklist_fails():
    class BrokenActions(_Actions):
        def execute_and_verify(self, spec, action, context):
            self.calls.append(action)
            return NodeResult.error('BLACKLIST_READBACK_FAILED', '未确认生效')

    tasks = _AsyncTasks()
    actions = BrokenActions()
    workflow = _workflow(
        _Search([[{'product_id': 's0'}], [{'product_id': 'p1'}, {'product_id': 'p2'}]]),
        _RelevanceNode(), actions, tasks, relevance_independent_actions=True,
    )
    run = workflow.run_batch([QuerySpec('q1', 'atlas', '裙子')], batch_id='failed-blacklist')['q1']
    assert run.nodes['relevance'].error_code == 'BLACKLIST_READBACK_FAILED'
    assert len(tasks.calls) == 1


def test_batched_detection_blacklists_all_candidates_without_root_cause_or_runner_action():
    class Batch:
        def run(self, inputs, context):
            return {item.spec.query: NodeResult.executed({
                'detected_product_ids': ['p1', 'p2'],
                'blacklist_candidates': [
                    {'product_id': 'p1', 'reason': '类目不符'},
                    {'product_id': 'p2', 'reason': '材质不符'},
                ],
            }) for item in inputs}
    actions = _Actions()
    tasks = _AsyncTasks()
    workflow = _workflow(
        _Search([[{'product_id': 's0'}], [{'product_id': 'p1'}, {'product_id': 'p2'}]]),
        _RelevanceNode(), actions, tasks,
        relevance_batch_runner=Batch(), relevance_independent_actions=True,
    )
    run = workflow.run_batch([QuerySpec('q1', 'atlas', '裙子')], batch_id='batch-all')['q1']
    assert run.nodes['relevance'].status is NodeStatus.EXECUTED
    assert actions.calls[0]['product_ids'] == ['p1', 'p2']
    assert len(tasks.calls) == 1
    assert tasks.calls[0]['evidence']['error_product_ids'] == ['p1', 'p2']
