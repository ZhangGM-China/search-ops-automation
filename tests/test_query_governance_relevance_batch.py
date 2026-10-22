from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.query_governance.models import NodeStatus, QuerySpec
from src.query_governance.relevance import ImageAsset, RELEVANCE_SYSTEM_PROMPT
from src.query_governance.relevance_batch import (
    RelevanceBatchConfig,
    RelevanceBatchInput,
    RelevanceBatchRunner,
)


@dataclass
class _Invocation:
    content: str
    audit: Mapping[str, Any] = field(default_factory=lambda: {"modelName": "vl"})
    response_metadata: Mapping[str, Any] | None = None

    @property
    def response(self):
        return type(
            "Response",
            (),
            {
                "content": self.content,
                "response_metadata": self.response_metadata,
            },
        )()


class _Runtime:
    def __init__(self, *, invalid_queries: set[str] | None = None):
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.invalid_queries = invalid_queries or set()

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[1]["content"][0]["text"])
        groups = payload["queries"]
        queries = tuple(item["query"] for item in groups)
        self.calls.append((queries, kwargs))
        if self.invalid_queries.intersection(queries):
            return _Invocation('{"results": []}')
        return _Invocation(
            json.dumps(
                {
                    "results": [
                        {
                            "query": group["query"],
                            "products": [
                                {
                                    "product_id": row[0],
                                    "is_relevant": True,
                                    "reason": None,
                                }
                                for row in group["products"]
                            ],
                        }
                        for group in groups
                    ]
                },
                ensure_ascii=False,
            )
        )


class _Store:
    def __init__(self, relevant: set[tuple[str, str, str]] | None = None):
        self.relevant = relevant or set()
        self.saved: list[tuple[str, str, str]] = []

    def get_relevant_product_ids(self, brand, query, product_ids):
        return {
            product_id
            for product_id in product_ids
            if (brand, query, product_id) in self.relevant
        }

    def save_relevant(self, brand, query, product_id, **kwargs):
        self.relevant.add((brand, query, product_id))
        self.saved.append((brand, query, product_id))

    def get_guidance(self, brand, query):
        return f"规则-{query}"


class _Cache:
    def __init__(self, assets: Mapping[str, ImageAsset] | None = None):
        self.assets = dict(assets or {})
        self.calls: list[list[str]] = []

    def prepare_many(self, urls):
        urls = list(urls)
        self.calls.append(urls)
        return {
            url: self.assets.get(
                url,
                ImageAsset(url, available=False, warning_code="IMAGE_URL_MISSING"),
            )
            for url in urls
        }


def _item(query: str, count: int = 1, *, image: str | None = None):
    return RelevanceBatchInput(
        spec=QuerySpec(query, "atlas", query),
        products=[
            {
                "productId": f"{query}-p{index}",
                "productName": "裙子",
                "imageUrl": image,
            }
            for index in range(1, count + 1)
        ],
    )


def test_token_audits_keep_initial_failed_output_and_all_retries():
    class TokenRuntime(_Runtime):
        def invoke(self, messages, **kwargs):
            invocation = super().invoke(messages, **kwargs)
            invocation.audit = {"tokenUsage": 20}
            return invocation

    runner = RelevanceBatchRunner(
        runtime=TokenRuntime(invalid_queries={"q1"}), record_store=_Store(), image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=10, concurrency=1, max_query_retries=2),
    )
    results = runner.run([_item("q1"), _item("q2")])
    failed_calls = results["q1"].result["modelCallAudits"]
    success_calls = results["q2"].result["modelCallAudits"]
    assert len(failed_calls) == 3
    assert len({call["requestId"] for call in failed_calls}) == 3
    assert failed_calls[0]["requestId"] == success_calls[0]["requestId"]
    assert failed_calls[0]["batchQueryCount"] == 2
    assert sum(call["tokenUsage"] for call in failed_calls) == 60


def test_token_audits_distinguish_transport_failure_and_cache_reuse():
    class TimeoutRuntime:
        def invoke(self, *args, **kwargs):
            raise TimeoutError("test timeout")

    runner = RelevanceBatchRunner(
        runtime=TimeoutRuntime(), record_store=_Store({("atlas", "cached", "cached-p1")}),
        image_cache=_Cache(), config=RelevanceBatchConfig(max_query_retries=0),
    )
    results = runner.run([_item("q1"), _item("cached")])
    assert results["cached"].result["modelCallAudits"] == []
    assert len(results["q1"].result["modelCallAudits"]) == 1
    assert results["q1"].result["modelCallAudits"][0]["tokenUsage"] is None


def test_batch_runner_packs_complete_queries_by_product_count():
    runtime = _Runtime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=3, concurrency=1),
    )

    results = runner.run([_item("q1", 2), _item("q2", 2), _item("q3", 1)])

    assert [call[0] for call in runtime.calls] == [("q1",), ("q2",), ("q3",)]
    assert all(result.status is NodeStatus.EXECUTED for result in results.values())


def test_batch_runner_records_zero_model_telemetry_for_reused_products():
    runner = RelevanceBatchRunner(
        runtime=_Runtime(),
        record_store=_Store({("atlas", "q1", "q1-p1")}),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=3, concurrency=1),
    )
    result = runner.run([_item("q1")])["q1"]
    assert result.result["relevanceSkipReason"] == "all_products_reused"
    assert result.telemetry["modelCallCount"] == 0
    assert result.telemetry["executionSeconds"] == 0.0
    assert result.telemetry["batchQueryCount"] == 0
    assert runner.runtime.calls == []


def test_batch_runner_records_zero_model_telemetry_for_empty_products():
    runner = RelevanceBatchRunner(
        runtime=_Runtime(), record_store=_Store(), image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=3, concurrency=1),
    )
    result = runner.run([RelevanceBatchInput(QuerySpec("q1", "atlas", "q1"), [])])["q1"]
    assert result.result["relevanceSkipReason"] == "no_products_to_check"
    assert result.telemetry["modelCallCount"] == 0
    assert result.telemetry["executionSeconds"] == 0.0


def test_batch_runner_retries_each_complete_query_after_invalid_batch_output():
    class _RetryRuntime(_Runtime):
        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[1]["content"][0]["text"])
            queries = tuple(item["query"] for item in payload["queries"])
            self.calls.append((queries, kwargs))
            if len(queries) > 1:
                return _Invocation('{"results": []}')
            group = payload["queries"][0]
            return _Invocation(
                json.dumps(
                    {
                        "results": [
                            {
                                "query": group["query"],
                                "products": [
                                    {
                                        "product_id": row[0],
                                        "is_relevant": True,
                                        "reason": None,
                                    }
                                    for row in group["products"]
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            )

    runtime = _RetryRuntime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=10, concurrency=1),
    )

    results = runner.run([_item("q1"), _item("q2")])

    assert [call[0] for call in runtime.calls] == [("q1", "q2"), ("q1",), ("q2",)]
    assert all(result.status is NodeStatus.EXECUTED for result in results.values())


def test_batch_runner_retries_transient_invalid_query_output_more_than_once():
    class _TransientInvalidRuntime(_Runtime):
        def __init__(self):
            super().__init__()
            self.query_attempts = 0

        def invoke(self, messages, **kwargs):
            self.query_attempts += 1
            if self.query_attempts <= 3:
                return _Invocation('{"results": []}')
            return super().invoke(messages, **kwargs)

    runtime = _TransientInvalidRuntime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=10, concurrency=1),
    )

    result = runner.run([_item("q-transient")])["q-transient"]

    assert result.status is NodeStatus.EXECUTED
    assert runtime.query_attempts == 4


def test_batch_runner_isolates_failed_query_after_batch_retry():
    class _PartialRuntime(_Runtime):
        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[1]["content"][0]["text"])
            queries = tuple(item["query"] for item in payload["queries"])
            self.calls.append((queries, kwargs))
            if queries == ("q1", "q2") or queries == ("q2",):
                return _Invocation('{"results": []}')
            return super().invoke(messages, **kwargs)

    runtime = _PartialRuntime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=10, concurrency=1),
    )

    results = runner.run([_item("q1"), _item("q2")])

    assert results["q1"].status is NodeStatus.EXECUTED
    assert results["q2"].status is NodeStatus.ERROR
    assert results["q2"].error_code == "RELEVANCE_OUTPUT_INVALID"


def test_batch_runner_enforces_image_and_request_limits_without_truncating():
    runtime = _Runtime()
    cache = _Cache(
        {
            "https://cdn/p1.jpg": ImageAsset(
                "https://cdn/p1.jpg", "https://cos/p1.jpg", True
            ),
            "https://cdn/p2.jpg": ImageAsset(
                "https://cdn/p2.jpg", "https://cos/p2.jpg", True
            ),
        }
    )
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=cache,
        config=RelevanceBatchConfig(batch_size=10, request_max_bytes=100_000, concurrency=1),
    )

    results = runner.run(
        [_item("q1", image="https://cdn/p1.jpg"), _item("q2", image="https://cdn/p2.jpg")]
    )

    assert [call[0] for call in runtime.calls] == [("q1", "q2")]
    assert all(result.status is NodeStatus.EXECUTED for result in results.values())

    limited = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=cache,
        config=RelevanceBatchConfig(
            batch_size=10, request_max_bytes=1, concurrency=1
        ),
    )
    oversized = limited.run([_item("q3", image="https://cdn/p1.jpg")])
    assert oversized["q3"].status is NodeStatus.ERROR
    assert oversized["q3"].error_code == "RELEVANCE_REQUEST_TOO_LARGE"


def test_batch_runner_passes_model_timeout_and_warns_on_image_failure():
    runtime = _Runtime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(model_timeout_seconds=17, concurrency=1),
    )

    result = runner.run([_item("q1")])["q1"]

    assert result.status is NodeStatus.EXECUTED
    assert runtime.calls[0][1]["timeout"] == 17
    assert result.result["warnings"][0]["code"] == "IMAGE_URL_MISSING"
    assert result.result["blacklist_candidates"] == []


def test_query_retry_explains_validation_error_and_requests_json_object():
    class RetryRuntime(_Runtime):
        def __init__(self):
            super().__init__(invalid_queries={"q1"})
            self.retry_system_prompts: list[str] = []

        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[1]["content"][0]["text"])
            if tuple(item["query"] for item in payload["queries"]) == ("q1",):
                self.retry_system_prompts.append(messages[0]["content"])
            return super().invoke(messages, **kwargs)

    runtime = RetryRuntime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=10, concurrency=1, max_query_retries=1),
    )

    result = runner.run([_item("q1")])["q1"]

    assert result.status is NodeStatus.ERROR
    retry_prompt = next(
        prompt for prompt in runtime.retry_system_prompts if "输出校验失败" in prompt
    )
    assert retry_prompt.startswith(RELEVANCE_SYSTEM_PROMPT)
    assert "不分别判定文本、图片是否通过" in retry_prompt
    assert "Query result is missing" in retry_prompt
    assert "不得重复 Query" in retry_prompt
    assert runtime.calls[-1][1]["response_format"] == {"type": "json_object"}


def test_query_retry_continues_after_model_invocation_raises():
    class TransientRetryRuntime(_Runtime):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def invoke(self, messages, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                return _Invocation('{"results": []}')
            if self.attempts == 2:
                raise TimeoutError("temporary model timeout")
            return super().invoke(messages, **kwargs)

    runtime = TransientRetryRuntime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=10, concurrency=1, max_query_retries=2),
    )

    result = runner.run([_item("q1")])["q1"]

    assert result.status is NodeStatus.EXECUTED
    assert runtime.attempts == 3


def test_long_invalid_query_is_repaired_with_strict_product_chunks():
    class SplitRuntime(_Runtime):
        def __init__(self):
            super().__init__()
            self.product_counts: list[int] = []

        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[1]["content"][0]["text"])
            product_count = len(payload["queries"][0]["products"])
            self.product_counts.append(product_count)
            self.calls.append((tuple(item["query"] for item in payload["queries"]), kwargs))
            if product_count > 8:
                return _Invocation('{"results": []}')
            return _Invocation(
                json.dumps(
                    {
                        "results": [
                            {
                                "query": payload["queries"][0]["query"],
                                "products": [
                                    {
                                        "product_id": row[0],
                                        "is_relevant": True,
                                        "reason": None,
                                    }
                                    for row in payload["queries"][0]["products"]
                                ],
                            }
                        ]
                    }
                )
            )

    runtime = SplitRuntime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=20, concurrency=1, max_query_retries=1),
    )

    result = runner.run([_item("q1", count=10)])["q1"]

    assert result.status is NodeStatus.EXECUTED
    assert len(result.result["detected_product_ids"]) == 10
    assert runtime.product_counts == [10, 8, 2]


def test_batch_runner_preserves_irrelevant_product_name():
    class Runtime(_Runtime):
        def invoke(self, messages, **kwargs):
            return _Invocation(json.dumps({"results": [{"query": "q1", "products": [{"product_id": "q1-p1", "is_relevant": False, "reason": "不相关"}]}]}))
    result = RelevanceBatchRunner(runtime=Runtime(), record_store=_Store(), image_cache=_Cache(), config=RelevanceBatchConfig(concurrency=1)).run([_item("q1")])["q1"]
    assert result.status is NodeStatus.EXECUTED
    assert result.result["blacklist_candidates"][0]["product_name"] == "裙子"
    assert result.action["product_names"] == {"q1-p1": "裙子"}


def test_batch_runner_uses_title_for_irrelevant_product_name():
    class Runtime(_Runtime):
        def invoke(self, messages, **kwargs):
            return _Invocation(
                json.dumps(
                    {
                        "results": [
                            {
                                "query": "q-title",
                                "products": [
                                    {
                                        "product_id": "q-title-p1",
                                        "is_relevant": False,
                                        "reason": "不相关",
                                    }
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            )

    item = RelevanceBatchInput(
        spec=QuerySpec("q-title", "atlas", "q-title"),
        products=[{"productId": "q-title-p1", "title": "标题商品"}],
    )
    result = RelevanceBatchRunner(
        runtime=Runtime(),
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(concurrency=1),
    ).run([item])["q-title"]

    assert result.status is NodeStatus.EXECUTED
    assert result.result["blacklist_candidates"][0]["product_name"] == "标题商品"
    assert result.action["product_names"] == {"q-title-p1": "标题商品"}


def test_batch_runner_executes_multiple_batches_concurrently():
    import threading
    import time

    class _SlowRuntime(_Runtime):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.peak = 0
            self.lock = threading.Lock()

        def invoke(self, messages, **kwargs):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(0.03)
                return super().invoke(messages, **kwargs)
            finally:
                with self.lock:
                    self.active -= 1

    runtime = _SlowRuntime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=1, concurrency=2),
    )
    results = runner.run([_item("q1"), _item("q2")])
    assert all(result.status is NodeStatus.EXECUTED for result in results.values())
    assert runtime.peak == 2


def test_batch_runner_does_not_apply_an_independent_image_limit():
    runtime = _Runtime()
    assets = {
        f"https://cdn/{index}.jpg": ImageAsset(
            f"https://cdn/{index}.jpg", f"https://cos/{index}.jpg", True
        )
        for index in range(3)
    }
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(assets),
        config=RelevanceBatchConfig(batch_size=3, concurrency=1),
    )
    results = runner.run(
        [_item("q1", image="https://cdn/0.jpg"), _item("q2", image="https://cdn/1.jpg"), _item("q3", image="https://cdn/2.jpg")]
    )
    assert [call[0] for call in runtime.calls] == [("q1", "q2", "q3")]
    assert all(result.status is NodeStatus.EXECUTED for result in results.values())


def test_batch_runner_marks_model_time_as_shared_and_keeps_query_independent_time_non_shared():
    runtime = _Runtime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=10, concurrency=1),
    )

    results = runner.run([_item("q1"), _item("q2")])

    for result in results.values():
        telemetry = result.telemetry
        assert telemetry["batchQueryCount"] == 2
        assert telemetry["batchModelSeconds"] >= 0
        assert telemetry["sharedBatchSeconds"] == telemetry["batchModelSeconds"]
        assert telemetry["independentExecutionSeconds"] == telemetry["imagePreparationSeconds"]
        assert telemetry["roundSeconds"] >= telemetry["imagePreparationSeconds"]


def test_batch_runner_records_model_output_truncation_in_telemetry():
    class _TruncatedRuntime(_Runtime):
        def invoke(self, messages, **kwargs):
            invocation = super().invoke(messages, **kwargs)
            return _Invocation(
                invocation.content,
                audit=invocation.audit,
                response_metadata={"finish_reason": "length"},
            )

    runtime = _TruncatedRuntime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=10, concurrency=1),
    )

    result = runner.run([_item("q1")])["q1"]

    assert result.status is NodeStatus.EXECUTED
    assert result.telemetry["truncationCount"] == 1


def test_batch_runner_preserves_shared_truncation_across_query_retries():
    class _TruncatedBatchRuntime(_Runtime):
        def __init__(self):
            super().__init__()
            self.first_call = True

        def invoke(self, messages, **kwargs):
            if self.first_call:
                self.first_call = False
                payload = json.loads(messages[1]["content"][0]["text"])
                queries = tuple(item["query"] for item in payload["queries"])
                self.calls.append((queries, kwargs))
                return _Invocation(
                    '{"results": []}',
                    response_metadata={"finish_reason": "length"},
                )
            return super().invoke(messages, **kwargs)

    runtime = _TruncatedBatchRuntime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=10, concurrency=1),
    )

    results = runner.run([_item("q1"), _item("q2")])

    assert all(result.status is NodeStatus.EXECUTED for result in results.values())
    assert all(result.telemetry["truncationCount"] == 1 for result in results.values())
    assert all(result.telemetry["truncationKeys"] == ["model:1"] for result in results.values())


def test_batch_runner_does_not_retry_permanent_model_unavailable_errors():
    class _UnavailableRuntime(_Runtime):
        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[1]["content"][0]["text"])
            queries = tuple(item["query"] for item in payload["queries"])
            self.calls.append((queries, kwargs))
            raise RuntimeError(
                "Error code: 503 - {'error': {'code': 'model_not_found', "
                "'message': 'No available channel'}}"
            )

    runtime = _UnavailableRuntime()
    runner = RelevanceBatchRunner(
        runtime=runtime,
        record_store=_Store(),
        image_cache=_Cache(),
        config=RelevanceBatchConfig(batch_size=10, concurrency=2, max_query_retries=2),
    )

    results = runner.run([_item("q1"), _item("q2")])

    assert len(runtime.calls) == 1
    assert all(result.error_code == "RELEVANCE_MODEL_UNAVAILABLE" for result in results.values())
    for result in results.values():
        telemetry = result.telemetry
        assert telemetry["modelSeconds"] == telemetry["batchModelSeconds"]
        assert telemetry["executionSeconds"] == round(
            telemetry["imagePreparationSeconds"] + telemetry["modelSeconds"], 6
        )
        assert telemetry["independentExecutionSeconds"] == telemetry["imagePreparationSeconds"]


def test_single_and_batch_requests_use_the_evaluated_integrated_prompt():
    import hashlib
    from src.query_governance.relevance import build_relevance_messages

    # 固定已实测候选的指纹，避免接入或后续合并时悄然换回旧稿。
    assert hashlib.sha256((RELEVANCE_SYSTEM_PROMPT.rstrip() + "\n").encode()).hexdigest() == (
        "d8874934183bb6a123f7e790da24193bec2f4edfd78cc69d31b91ec7913edb91"
    )
    single = build_relevance_messages(QuerySpec("q1", "atlas", "q1"), [{"productId": "p1"}], {}, guidance="规则-q1")
    assert single[0]["content"].startswith(RELEVANCE_SYSTEM_PROMPT)
    assert "规则-q1" in single[0]["content"]

    class CaptureRuntime(_Runtime):
        def invoke(self, messages, **kwargs):
            self.messages = messages
            return super().invoke(messages, **kwargs)

    runtime = CaptureRuntime()
    runner = RelevanceBatchRunner(runtime=runtime, record_store=_Store(), image_cache=_Cache(), config=RelevanceBatchConfig(batch_size=10, concurrency=1))
    runner.run([_item("q1"), _item("q2")])
    system = runtime.messages[0]["content"]
    assert system.startswith(RELEVANCE_SYSTEM_PROMPT)
    assert "规则-q1" in system and "规则-q2" in system
    assert "每个 Query 分组必须完整返回" in system
    payload = json.loads(runtime.messages[1]["content"][0]["text"])
    assert [group["query"] for group in payload["queries"]] == ["q1", "q2"]
