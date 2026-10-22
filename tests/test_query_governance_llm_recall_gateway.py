"""Unit coverage for the real LLM catalog recall gateway (number mapping and validation)."""

from __future__ import annotations

import json

import pytest

from src.query_governance.recall_rate import (
    RECALL_SYSTEM_PROMPT,
    RECALL_PROMPT_VERSION,
    RuntimeLLMRecallGateway,
)


def _catalog(count: int = 3) -> list:
    from src.query_governance.recall_rate import CatalogProduct

    return [
        CatalogProduct(
            brand="atlas",
            spu=f"P{i}",
            recall_product_id=f"P{i}",
            product_name=f"测试商品{i}",
        )
        for i in range(1, count + 1)
    ]


class _FakeSource:
    def __init__(self, products):
        self.products = products

    def list_products(self, brand: str):
        return list(self.products)


class _FakeRuntime:
    def __init__(self, content: str):
        self.content = content
        self.messages: list = []

    def invoke(self, messages, **kwargs):
        self.messages.append(messages)
        return self.content


def test_gateway_maps_product_numbers_and_applies_score_threshold():
    runtime = _FakeRuntime(json.dumps({"results": {"1": {"1": 95, "2": 40}}}))
    gateway = RuntimeLLMRecallGateway(
        product_source=_FakeSource(_catalog(2)),
        runtime=runtime,
        score_threshold=50,
    )

    scored = gateway.recall_queries("atlas", ["连衣裙"])

    assert [item.recall_product_id for item, _score in scored["连衣裙"]] == ["P1"]
    assert scored["连衣裙"][0][1] == 95.0

    permissive = RuntimeLLMRecallGateway(
        product_source=_FakeSource(_catalog(2)),
        runtime=_FakeRuntime(json.dumps({"results": {"1": {"1": 95, "2": 40}}})),
        score_threshold=0,
    )
    scored_all = permissive.recall_queries("atlas", ["连衣裙"])
    assert [item.recall_product_id for item, _score in scored_all["连衣裙"]] == ["P1", "P2"]


def test_gateway_rejects_unknown_product_numbers():
    gateway = RuntimeLLMRecallGateway(
        product_source=_FakeSource(_catalog(2)),
        runtime=_FakeRuntime(json.dumps({"results": {"1": {"99": 90}}})),
    )

    with pytest.raises(ValueError, match="unknown product_no"):
        gateway.recall_queries("atlas", ["连衣裙"])


def test_gateway_rejects_missing_query_coverage_instead_of_silent_empty_recall():
    gateway = RuntimeLLMRecallGateway(
        product_source=_FakeSource(_catalog(2)),
        runtime=_FakeRuntime(json.dumps({"results": {"2": {"1": 90}}})),
    )

    with pytest.raises(ValueError, match="missing results for query_no=1"):
        gateway.recall_queries("atlas", ["连衣裙"])


def test_gateway_rejects_non_finite_scores():
    gateway = RuntimeLLMRecallGateway(
        product_source=_FakeSource(_catalog(2)),
        runtime=_FakeRuntime(json.dumps({"results": {"1": {"1": "not-a-number"}}})),
    )

    with pytest.raises(ValueError):
        gateway.recall_queries("atlas", ["连衣裙"])


def test_gateway_dedupes_duplicate_catalog_product_ids():
    runtime = _FakeRuntime(json.dumps({"results": {"1": {"1": 88}}}))
    gateway = RuntimeLLMRecallGateway(
        product_source=_FakeSource([*_catalog(1), _catalog(1)[0]]),
        runtime=runtime,
    )

    scored = gateway.recall_queries("atlas", ["连衣裙"])

    assert [item.recall_product_id for item, _score in scored["连衣裙"]] == ["P1"]


def test_gateway_keeps_full_v7_prompt_contract_and_puts_queries_last():
    runtime = _FakeRuntime(json.dumps({"results": {"1": {}}}))
    gateway = RuntimeLLMRecallGateway(
        product_source=_FakeSource(_catalog(1)),
        runtime=runtime,
    )
    gateway.recall_queries("atlas", ["连衣裙"])

    for section in (
        "<query_analysis>",
        "<evidence_policy>",
        "<decision_procedure>",
        "<generalization_constraints>",
        "<score_rules>",
        "<output_contract>",
        "<final_validation>",
    ):
        assert section in RECALL_SYSTEM_PROMPT
    assert RECALL_PROMPT_VERSION == "7"

    assert len(runtime.messages) == 1
    system_message, user_message = runtime.messages[0]
    assert system_message["role"] == "system"
    assert system_message["content"] == RECALL_SYSTEM_PROMPT
    assert user_message["role"] == "user"
    final_part = user_message["content"][-1]
    queries_payload = json.loads(final_part["text"])
    assert queries_payload["queries"]["query_no"] == [1]
    assert queries_payload["queries"]["query"] == ["连衣裙"]
