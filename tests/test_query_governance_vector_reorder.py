from __future__ import annotations

import threading
import time

import pytest
import requests

from src.query_governance.models import NodeStatus, QuerySpec
from src.query_governance.vector_reorder import (
    DEFAULT_VECTOR_REORDER_ENDPOINT,
    VectorReorderBatchRunner,
    VectorReorderClient,
    VectorReorderConfig,
    VectorReorderError,
    VectorReorderInput,
    VectorReorderNodeRunner,
    split_query_gender,
)


def test_default_vector_reorder_endpoint_uses_hologres_domain():
    assert DEFAULT_VECTOR_REORDER_ENDPOINT == (
        "http://vector.example.test/api/hologres/recall/search"
    )


@pytest.mark.parametrize(
    ("brand", "query", "expected_query", "expected_gender"),
    [
        ("atlas", "连衣裙 女士", "连衣裙", "女士"),
        ("harbor", "连衣裙 女士", "连衣裙 女士", None),
        ("riviera", "连衣裙 女士", "连衣裙 女士", None),
    ],
)
def test_only_atlas_splits_trailing_gender_suffix(
    brand, query, expected_query, expected_gender
):
    assert split_query_gender(query, brand=brand) == (
        expected_query,
        expected_gender,
    )


class _Response:
    def __init__(self, payload, *, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _Post:
    def __init__(self, payloads, *, delay=0.0, errors=()):
        self.payloads = payloads
        self.delay = delay
        self.errors = set(errors)
        self.calls = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, url, *, json, headers, timeout):
        query = json["query"]
        with self._lock:
            self.calls.append((url, json, headers, timeout))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if query in self.errors:
                raise RuntimeError(f"vector unavailable: {query}")
            return _Response(self.payloads.get(query, {"result": []}))
        finally:
            with self._lock:
                self.active -= 1


def _payload(*ids):
    return {
        "total": len(ids),
        "pageNum": 1,
        "pageSize": 30,
        "result": [
            {"taggingUid": product_id, "productName": f"商品-{product_id}"}
            for product_id in ids
        ],
    }


def _input(query_id: str, query: str, *, should_reorder=True, reason=None):
    return VectorReorderInput(
        spec=QuerySpec(query_id, "atlas", query),
        should_reorder=should_reorder,
        preference_description=(f"偏好-{query_id}" if should_reorder else None),
        reason=reason,
    )


def test_client_sends_contract_parses_result_and_preserves_duplicate_order():
    post = _Post({"连衣裙": _payload("p1", "p1", "p2")})
    client = VectorReorderClient(
        endpoint_url="http://vector.test/reorder",
        request_func=post,
    )

    response = client.rerank(
        brand="atlas",
        query="连衣裙",
        user_style="偏好轻薄",
        gender="女士",
        page_size=2,
    )

    assert post.calls[0][0] == "http://vector.test/reorder"
    assert post.calls[0][1] == {
        "brand": "atlas",
        "query": "连衣裙",
        "userStyle": "偏好轻薄",
        "gender": "女士",
        "pageSize": 2,
        "pageNum": 1,
    }
    assert [item["product_id"] for item in response.items] == ["p1", "p1"]
    assert response.selected_count == 2


def test_vector_config_uses_brand_api_alias_for_external_service():
    post = _Post({"衬衫": _payload("p1")})
    config = VectorReorderConfig.from_mapping(
        {
            "api_alias": "alias_atlas",
            "query_governance_vector_reorder_concurrency": 1,
            "product_limit": 1,
        }
    )
    runner = VectorReorderNodeRunner(
        client=VectorReorderClient(request_func=post), config=config
    )

    result = runner.run(
        QuerySpec("q1", "atlas", "衬衫"),
        {"preference_result": {"should_reorder": True, "preference_description": "偏好"}},
    )

    assert result.status is NodeStatus.EXECUTED
    assert post.calls[0][1]["brand"] == "alias_atlas"


def test_brand_business_config_preserves_api_alias_for_composition():
    from src.config import get_brand_business_config

    assert get_brand_business_config("atlas")["api_alias"] == "alias_atlas"


def test_client_omits_gender_when_query_has_no_explicit_gender():
    post = _Post({"外套": _payload("p1")})
    client = VectorReorderClient(request_func=post)

    client.rerank(
        brand="atlas",
        query="外套",
        user_style="偏好通勤",
        page_size=16,
    )

    assert "gender" not in post.calls[0][1]


def test_short_gender_tokens_are_kept_in_query():
    post = _Post({"连衣裙 女": _payload("p1")})
    runner = VectorReorderNodeRunner(
        client=VectorReorderClient(request_func=post),
        config=VectorReorderConfig(product_limit=1, concurrency=1),
    )

    runner.run(
        QuerySpec("q1", "atlas", "连衣裙 女"),
        {"preference_result": {"should_reorder": True, "preference_description": "偏好"}},
    )

    assert post.calls[0][1]["query"] == "连衣裙 女"
    assert "gender" not in post.calls[0][1]


def test_client_rejects_invalid_response_and_missing_product_id():
    invalid = VectorReorderClient(
        request_func=_Post({"裙子": {"result": {}}}),
    )
    with pytest.raises(VectorReorderError) as invalid_exc:
        invalid.rerank("atlas", "裙子", "偏好", page_size=16)
    assert invalid_exc.value.error_code == "VECTOR_REORDER_RESPONSE_INVALID"

    missing_id = VectorReorderClient(
        request_func=_Post({"裙子": {"result": [{"productName": "无 ID"}]}}),
    )
    with pytest.raises(VectorReorderError) as missing_exc:
        missing_id.rerank("atlas", "裙子", "偏好", page_size=16)
    assert missing_exc.value.error_code == "VECTOR_REORDER_PRODUCT_ID_MISSING"


def test_client_rejects_empty_result_and_http_failure():
    empty = VectorReorderClient(request_func=_Post({"裙子": {"result": []}}))
    with pytest.raises(VectorReorderError) as empty_exc:
        empty.rerank("atlas", "裙子", "偏好", page_size=16)
    assert empty_exc.value.error_code == "VECTOR_REORDER_NO_PRODUCTS"

    failed = VectorReorderClient(
        request_func=lambda *args, **kwargs: _Response({"message": "bad"}, status_code=500),
    )
    with pytest.raises(VectorReorderError) as failed_exc:
        failed.rerank("atlas", "裙子", "偏好", page_size=16)
    assert failed_exc.value.error_code == "VECTOR_REORDER_HTTP_FAILED"


def test_client_retries_transient_empty_result_before_returning_products():
    class _TransientEmptyPost:
        def __init__(self):
            self.calls = 0

        def __call__(self, url, *, json, headers, timeout):
            self.calls += 1
            if self.calls == 1:
                return _Response({"result": []})
            return _Response(_payload("p1"))

    post = _TransientEmptyPost()
    client = VectorReorderClient(
        request_func=post,
        retry_attempts=2,
        retry_backoff_seconds=0,
        sleep_func=lambda _: None,
    )

    response = client.rerank("atlas", "裙子", "偏好", page_size=1)

    assert response.selected_count == 1
    assert post.calls == 2
    assert response.attempts == 2


def test_client_does_not_retry_permanent_client_errors():
    calls = 0

    def post(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _Response({"message": "bad request"}, status_code=400)

    client = VectorReorderClient(
        request_func=post,
        retry_attempts=3,
        retry_backoff_seconds=0,
        sleep_func=lambda _: None,
    )

    with pytest.raises(VectorReorderError) as exc_info:
        client.rerank("atlas", "裙子", "偏好", page_size=1)

    assert exc_info.value.error_code == "VECTOR_REORDER_HTTP_FAILED"
    assert calls == 1


def test_runner_records_exhausted_empty_response_attempts_without_action():
    post = _Post({"裙子": {"result": []}})
    runner = VectorReorderBatchRunner(
        client=VectorReorderClient(request_func=post, sleep_func=lambda _: None),
        config=VectorReorderConfig(product_limit=1, concurrency=1, retry_attempts=3),
    )

    result = runner.run_batch([_input("q1", "裙子")])["q1"]

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "VECTOR_REORDER_NO_PRODUCTS"
    assert result.error_message == "向量接口没有返回可用商品"
    assert result.action is None
    assert result.telemetry["vectorRetryAttempts"] == 3
    assert len(post.calls) == 3


def test_client_retries_timeout_and_service_unavailable():
    calls = 0
    waits = []

    def post(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.Timeout("temporary timeout")
        if calls == 2:
            return _Response({"message": "busy"}, status_code=503)
        return _Response(_payload("p1"))

    client = VectorReorderClient(
        request_func=post,
        retry_attempts=3,
        retry_backoff_seconds=0.5,
        sleep_func=waits.append,
    )

    response = client.rerank("atlas", "裙子", "偏好", page_size=1)

    assert response.attempts == 3
    assert calls == 3
    assert len(waits) == 2
    assert 0.5 <= waits[0] < 0.75
    assert 1.0 <= waits[1] < 1.5


def test_retry_wait_is_staggered_by_query():
    waits = []
    client = VectorReorderClient(
        request_func=_Post({"裙子": {"result": []}, "外套": {"result": []}}),
        retry_attempts=2,
        retry_backoff_seconds=0.5,
        sleep_func=waits.append,
    )

    for query in ("裙子", "外套"):
        with pytest.raises(VectorReorderError):
            client.rerank("atlas", query, "偏好", page_size=1)

    assert len(waits) == 2
    assert waits[0] != waits[1]


def test_batch_runner_skips_non_eligible_and_isolates_query_errors():
    post = _Post(
        {"q1": _payload("p1"), "q3": _payload("p3")},
        errors={"q3"},
    )
    runner = VectorReorderBatchRunner(
        client=VectorReorderClient(request_func=post),
        config=VectorReorderConfig(product_limit=2, concurrency=2),
    )

    results = runner.run_batch(
        [_input("q1", "q1"), _input("q2", "q2", should_reorder=False, reason="无需重排"), _input("q3", "q3")]
    )

    assert results["q1"].status is NodeStatus.EXECUTED
    assert results["q1"].result["product_ids"] == ["p1"]
    assert results["q2"].status is NodeStatus.SKIPPED
    assert results["q2"].result["reason"] == "无需重排"
    assert results["q3"].status is NodeStatus.ERROR
    assert results["q3"].error_code == "VECTOR_REORDER_REQUEST_FAILED"


def test_batch_runner_enforces_configured_concurrency():
    post = _Post({query: _payload(f"p-{query}") for query in ("q1", "q2", "q3", "q4")}, delay=0.02)
    runner = VectorReorderBatchRunner(
        client=VectorReorderClient(request_func=post),
        config=VectorReorderConfig(product_limit=1, concurrency=2),
    )

    runner.run_batch([_input(f"q{i}", f"q{i}") for i in range(1, 5)])

    assert post.max_active == 2


def test_batch_runner_records_query_duration_and_configured_concurrency():
    post = _Post({"q1": _payload("p-q1")}, delay=0.01)
    runner = VectorReorderBatchRunner(
        client=VectorReorderClient(request_func=post),
        config=VectorReorderConfig(product_limit=1, concurrency=2),
    )

    result = runner.run_batch([_input("q1", "q1")])["q1"]

    assert result.telemetry["executionSeconds"] >= 0
    assert result.telemetry["maxConcurrency"] == 2


def test_batch_runner_enforces_each_brand_concurrency_limit():
    post = _Post(
        {query: _payload(f"p-{query}") for query in ("z1", "z2", "h1", "h2")},
        delay=0.02,
    )
    inputs = [
        VectorReorderInput(QuerySpec("z1", "atlas", "z1"), True, "偏好"),
        VectorReorderInput(QuerySpec("z2", "atlas", "z2"), True, "偏好"),
        VectorReorderInput(QuerySpec("h1", "harbor", "h1"), True, "偏好"),
        VectorReorderInput(QuerySpec("h2", "harbor", "h2"), True, "偏好"),
    ]
    active_by_brand: dict[str, int] = {"atlas": 0, "harbor": 0}
    max_by_brand: dict[str, int] = {"atlas": 0, "harbor": 0}
    original = post

    def limited_post(url, *, json, headers, timeout):
        brand = json["brand"]
        with original._lock:
            active_by_brand[brand] += 1
            max_by_brand[brand] = max(max_by_brand[brand], active_by_brand[brand])
        try:
            return original(url, json=json, headers=headers, timeout=timeout)
        finally:
            with original._lock:
                active_by_brand[brand] -= 1

    runner = VectorReorderBatchRunner(
        client=VectorReorderClient(request_func=limited_post),
        config=VectorReorderConfig(product_limit=1, concurrency=1),
        config_provider=lambda brand: {"concurrency": 2 if brand == "harbor" else 1},
    )

    results = runner.run_batch(inputs)

    assert all(result.status is NodeStatus.EXECUTED for result in results.values())
    assert max_by_brand["atlas"] == 1
    assert max_by_brand["harbor"] <= 2


def test_node_runner_consumes_preference_result_and_splits_gender_suffix():
    post = _Post({"连衣裙": _payload("p1")})
    runner = VectorReorderNodeRunner(
        client=VectorReorderClient(request_func=post),
        config=VectorReorderConfig(product_limit=1, concurrency=1),
    )

    result = runner.run(
        QuerySpec("q1", "atlas", "连衣裙 女士"),
        {"preference_result": {"should_reorder": True, "preference_description": "偏好"}},
    )

    assert result.status is NodeStatus.EXECUTED
    assert result.result["product_ids"] == ["p1"]
    assert post.calls[0][1]["query"] == "连衣裙"
    assert post.calls[0][1]["gender"] == "女士"


def test_node_runner_keeps_gender_suffix_for_harbor():
    post = _Post({"连衣裙 女士": _payload("p1")})
    runner = VectorReorderNodeRunner(
        client=VectorReorderClient(request_func=post),
        config=VectorReorderConfig(product_limit=1, concurrency=1),
    )

    result = runner.run(
        QuerySpec("q1", "harbor", "连衣裙 女士"),
        {"preference_result": {"should_reorder": True, "preference_description": "偏好"}},
    )

    assert result.status is NodeStatus.EXECUTED
    assert post.calls[0][1]["query"] == "连衣裙 女士"
    assert "gender" not in post.calls[0][1]


def test_brand_config_overrides_vector_endpoint_timeout_and_limit():
    post = _Post({"裙子": _payload("p1", "p2")})
    runner = VectorReorderBatchRunner(
        client=VectorReorderClient(
            endpoint_url="http://default.test/reorder",
            timeout_seconds=15,
            request_func=post,
        ),
        config=VectorReorderConfig(product_limit=1, concurrency=1),
        config_provider=lambda brand: {
            "query_governance_vector_reorder_url": "http://brand.test/reorder",
            "query_governance_vector_reorder_timeout_seconds": 3,
            "query_governance_check_product_limit": 2,
        },
    )

    result = runner.run_batch([_input("q1", "裙子")])["q1"]

    assert result.status is NodeStatus.EXECUTED
    assert result.result["product_ids"] == ["p1", "p2"]
    assert post.calls[0][0] == "http://brand.test/reorder"
    assert post.calls[0][3] == 10


def test_atlas_filters_low_stock_candidates_and_pages_until_limit_is_filled():
    calls = []

    class _PagedPost:
        def __call__(self, url, *, json, headers, timeout):
            calls.append(dict(json))
            if json["pageNum"] == 1:
                return _Response(_payload_with_stock(("low", 3), ("good-1", 20)))
            return _Response(_payload_with_stock(("good-2", 30), ("good-3", 40)))

    runner = VectorReorderBatchRunner(
        client=VectorReorderClient(request_func=_PagedPost()),
        config=VectorReorderConfig(product_limit=2, concurrency=1),
    )

    result = runner.run_batch([_input("q1", "粉色上衣")])["q1"]

    assert result.status is NodeStatus.EXECUTED
    assert result.result["product_ids"] == ["good-1", "good-2"]
    assert [call["pageNum"] for call in calls] == [1, 2]
    assert result.result["filtered_low_stock_product_ids"] == ["low"]


def test_non_atlas_only_zero_stock_is_filtered():
    class _HmPost:
        def __call__(self, url, *, json, headers, timeout):
            if json["pageNum"] == 1:
                return _Response(_payload_with_stock(("zero", 0),))
            return _Response(_payload_with_stock(("one", 1),))

    item = VectorReorderInput(QuerySpec("q1", "harbor", "上衣"), True, "偏好")
    runner = VectorReorderBatchRunner(
        client=VectorReorderClient(request_func=_HmPost()),
        config=VectorReorderConfig(product_limit=1, concurrency=1),
    )

    result = runner.run_batch([item])["q1"]

    assert result.result["product_ids"] == ["one"]


def _payload_with_stock(*items):
    return {
        "total": len(items),
        "pageNum": 1,
        "pageSize": len(items),
        "result": [
            {"taggingUid": product_id, "stock": stock}
            for product_id, stock in items
        ],
    }
