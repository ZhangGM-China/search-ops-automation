from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

import pytest

from src.query_governance.models import NodeStatus, QuerySpec
from src.query_governance.preference_generation import (
    PreferenceGenerationConfig,
    PreferenceGenerationInput,
    PreferenceGenerationRunner,
)


@dataclass
class _Response:
    content: str


class _Runtime:
    def __init__(self, responses, *, delay=0.0, error_queries=()):
        self.responses = responses
        self.delay = delay
        self.error_queries = set(error_queries)
        self.calls = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        query_ids = tuple(
            payload["queries"]
            if isinstance(payload["queries"], dict)
            else (item["query_id"] for item in payload["queries"])
        )
        with self._lock:
            self.calls.append((query_ids, messages, kwargs))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.error_queries.intersection(query_ids):
                raise RuntimeError("model unavailable")
            return _Response(
                json.dumps(
                    {
                        "results": [
                            {
                                "query_id": query_id,
                                "should_reorder": True,
                                "preference_description": f"偏好-{query_id}",
                                "reason": None,
                            }
                            for query_id in query_ids
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            with self._lock:
                self.active -= 1


def _item(query_id: str, *, eligible=True, size=1):
    return PreferenceGenerationInput(
        spec=QuerySpec(query_id, "atlas", query_id, reorder_eligible=eligible),
        shared_context={"season": "夏季"},
        brand_context={"social_trends": []},
        products=([{"product_id": f"p-{query_id}", "product_name": "裙子"}] * size),
    )


def test_only_eligible_queries_enter_dynamic_batches_and_query_groups_are_not_split():
    runtime = _Runtime({})
    runner = PreferenceGenerationRunner(
        runtime=runtime,
        config=PreferenceGenerationConfig(batch_query_limit=2, concurrency=1),
    )

    results = runner.run(
        [_item("q1"), _item("q2", eligible=False), _item("q3"), _item("q4")]
    )

    assert [call[0] for call in runtime.calls] == [("q1", "q3"), ("q4",)]
    assert results["q2"].status is NodeStatus.SKIPPED
    assert results["q2"].result["reason"] == "CTR/CVR 正常"
    assert results["q1"].result["preference_description"] == "偏好-q1"


def test_query_with_large_evidence_is_submitted_as_one_over_limit_batch():
    runtime = _Runtime({})
    runner = PreferenceGenerationRunner(
        runtime=runtime,
        config=PreferenceGenerationConfig(
            batch_query_limit=10, token_limit=30, concurrency=1
        ),
    )

    results = runner.run([_item("q1", size=20), _item("q2")])

    assert [call[0] for call in runtime.calls] == [("q1",), ("q2",)]
    assert results["q1"].result["warnings"] == ["preference_batch_token_limit_exceeded"]


def test_model_failure_only_marks_the_affected_query_batch_as_error():
    runtime = _Runtime({}, error_queries={"q2"})
    runner = PreferenceGenerationRunner(
        runtime=runtime,
        config=PreferenceGenerationConfig(batch_query_limit=1, concurrency=2),
    )

    results = runner.run([_item("q1"), _item("q2")])

    assert results["q1"].status is NodeStatus.EXECUTED
    assert results["q2"].status is NodeStatus.ERROR
    assert results["q2"].error_code == "PREFERENCE_MODEL_FAILED"


def test_configured_concurrency_is_enforced():
    runtime = _Runtime({}, delay=0.02)
    runner = PreferenceGenerationRunner(
        runtime=runtime,
        config=PreferenceGenerationConfig(batch_query_limit=1, concurrency=2),
    )

    runner.run([_item("q1"), _item("q2"), _item("q3"), _item("q4")])

    assert runtime.max_active <= 2
    assert runtime.max_active == 2


def test_invalid_or_incomplete_model_output_is_an_error():
    class _InvalidRuntime:
        def invoke(self, messages, **kwargs):
            return _Response('{"results": [{"query_id": "q1"}]}')

    runner = PreferenceGenerationRunner(
        runtime=_InvalidRuntime(),
        config=PreferenceGenerationConfig(batch_query_limit=1, concurrency=1),
    )

    result = runner.run([_item("q1")])["q1"]

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "PREFERENCE_OUTPUT_INVALID"


def test_config_rejects_non_positive_limits():
    with pytest.raises(ValueError):
        PreferenceGenerationConfig(batch_query_limit=0)
    with pytest.raises(ValueError):
        PreferenceGenerationConfig(concurrency=0)
    with pytest.raises(ValueError):
        PreferenceGenerationConfig(token_limit=0)


def test_config_reads_brand_runtime_limits_and_forwards_batch_trace_config():
    config = PreferenceGenerationConfig.from_mapping(
        {
            "query_governance_preference_generation_batch_query_limit": 3,
            "query_governance_preference_generation_concurrency": 2,
            "query_governance_preference_generation_token_limit": 600,
        }
    )
    runtime = _Runtime({})
    runner = PreferenceGenerationRunner(runtime=runtime, config=config)

    runner.run(
        [_item("q1"), _item("q2")],
        batch_config={
            "callbacks": ["batch-handler"],
            "metadata": {"batch_id": "b1"},
            "run_name": "query-governance.preference_generation",
        },
    )

    assert config.batch_query_limit == 3
    assert config.concurrency == 2
    assert config.token_limit == 600
    assert runtime.calls[0][2]["callbacks"] == ["batch-handler"]
    assert runtime.calls[0][2]["metadata"] == {"batch_id": "b1"}


def test_runner_unwraps_model_runtime_invocation_result_and_keeps_audit():
    class _WrappedRuntime:
        def invoke(self, messages, **kwargs):
            return type(
                "Invocation",
                (),
                {
                    "response": _Response(
                        '{"results":[{"query_id":"q1","should_reorder":true,'
                        '"preference_description":"偏好-q1","reason":null}]}'
                    ),
                    "audit": {"modelName": "primary", "tokenUsage": 12},
                },
            )()

    result = PreferenceGenerationRunner(
        runtime=_WrappedRuntime(),
        config=PreferenceGenerationConfig(batch_query_limit=1, concurrency=1),
    ).run([_item("q1")])["q1"]

    assert result.status is NodeStatus.EXECUTED
    assert result.result["audit"]["modelName"] == "primary"
    assert result.result["audit"]["tokenUsage"] == 12
    assert result.result["audit"]["batchQueryCount"] == 1


def test_runner_sends_brand_level_compact_prompt_and_accepts_brand_strategies_output():
    class _NewRuntime:
        def __init__(self):
            self.messages = []

        def invoke(self, messages, **kwargs):
            self.messages.append(messages)
            request = json.loads(messages[-1]["content"])
            assert request["brand"] == "atlas"
            assert list(request["queries"]) == ["q1", "q2"]
            assert isinstance(request["queries"]["q1"], list)
            assert "task" not in request
            return _Response(json.dumps({
                "brand": "atlas",
                "strategies": {"q1": "偏好-q1", "q2": "偏好-q2"},
            }, ensure_ascii=False))

    runtime = _NewRuntime()
    results = PreferenceGenerationRunner(
        runtime=runtime,
        config=PreferenceGenerationConfig(batch_query_limit=10, concurrency=1),
    ).run([_item("q1"), _item("q2")])

    assert results["q1"].result["preference_description"] == "偏好-q1"
    assert results["q2"].result["preference_description"] == "偏好-q2"
    assert "电商搜索 Query 治理" in runtime.messages[0][0]["content"]


def test_runner_keeps_different_brands_in_separate_requests():
    class _Runtime:
        def __init__(self):
            self.brands = []

        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.brands.append(payload["brand"])
            return _Response(json.dumps({
                "brand": payload["brand"],
                "strategies": {query: "ok" for query in payload["queries"]},
            }, ensure_ascii=False))

    runtime = _Runtime()
    items = [_item("z1"), PreferenceGenerationInput(
        spec=QuerySpec("h1", "harbor", "裙子"),
        shared_context={}, brand_context={}, products=[],
    )]
    PreferenceGenerationRunner(
        runtime=runtime,
        config=PreferenceGenerationConfig(batch_query_limit=10, concurrency=1),
    ).run(items)

    assert runtime.brands == ["atlas", "harbor"]
