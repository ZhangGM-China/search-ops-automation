"""Manual relevance check service contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.manual_relevance import (
    ManualCheckStatus,
    ManualProductStatus,
    ManualQueryStatus,
    ManualRelevanceCheckRepository,
    ManualRelevanceCheckService,
    ProductDetailBatchGateway,
    ProductDetailResult,
)
from src.query_governance.models import QueryGovernanceManualRelevanceCheck
from src.query_governance.relevance import ImageAsset


@dataclass
class _Invocation:
    content: str
    audit: Mapping[str, Any] = field(
        default_factory=lambda: {"modelName": "vl", "tokenUsage": 12}
    )

    @property
    def response(self) -> Any:
        return type("Response", (), {"content": self.content})()


class _Runtime:
    def __init__(self, *, reject_product_ids: set[str] | None = None) -> None:
        self.calls: list[list[tuple[str, int]]] = []
        self.reject_product_ids = reject_product_ids or set()

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[1]["content"][0]["text"])
        groups = payload["queries"]
        self.calls.append(
            [(group["query"], len(group["products"])) for group in groups]
        )
        results = []
        for group in groups:
            relevant = []
            image_checked = []
            reasons: dict[str, str] = {}
            for index, row in enumerate(group["products"], start=1):
                product_id = str(row[0])
                rejected = product_id in self.reject_product_ids
                relevant.append(0 if rejected else 1)
                image_checked.append(1)
                if rejected:
                    reasons[str(index)] = "不符合检索意图"
            results.append({
                "query": group["query"],
                "relevant": relevant,
                "image_checked": image_checked,
                "reasons": reasons,
            })
        return _Invocation(json.dumps({"results": results}, ensure_ascii=False))


class _Cache:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.calls: list[list[str | None]] = []

    def prepare_many(self, urls):
        urls = list(urls)
        self.calls.append(urls)
        return {
            url: ImageAsset(
                source_url=str(url or ""),
                url=f"https://cache/{index}.jpg" if (url and self.available) else None,
                available=bool(url) and self.available,
            )
            for index, url in enumerate(urls)
        }


class _OperationalStub:
    def __init__(
        self,
        rows: Mapping[str, list[dict[str, Any]]],
        *,
        failing: set[str] | None = None,
        search_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rows = dict(rows)
        self.failing = failing or set()
        self.search_rows = list(search_rows or [])
        self.calls: list[tuple[str, str]] = []
        self.search_calls: list[tuple[str, str]] = []

    def get_raw_product_details(
        self, brand: str, product_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append((brand, product_id))
        if product_id in self.failing:
            raise RuntimeError("detail backend unavailable")
        return [dict(row) for row in self.rows.get(product_id, [])]

    def search_products_with_total(self, brand: str, query: str, *, page_size: int):
        self.search_calls.append((brand, query))
        return {"success": True, "products": self.search_rows[:page_size]}


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine, tables=[QueryGovernanceManualRelevanceCheck.__table__]
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def _detail_row(
    sku: str,
    *,
    name: str = "白色连衣裙",
    image: str | None = "https://img/c.jpg",
) -> dict[str, Any]:
    return {
        "productId": sku,
        "productName": name,
        "imageUrl": image,
        "description": "蕾丝连衣裙",
        "category": "连衣裙",
        "gender": "WOMAN",
    }


def _service(
    session_factory,
    *,
    rows: Mapping[str, list[dict[str, Any]]] | None = None,
    failing: set[str] | None = None,
    runtime: Any | None = None,
    cache: Any | None = None,
    batch_size: int = 240,
) -> tuple[ManualRelevanceCheckService, _OperationalStub]:
    operational = _OperationalStub(rows or {}, failing=failing)
    service = ManualRelevanceCheckService(
        repository=ManualRelevanceCheckRepository(session_factory),
        gateway=ProductDetailBatchGateway(operational, search_client=operational),
        runtime_factory=lambda: (runtime or _Runtime()),
        image_cache=cache or _Cache(),
        batch_size=batch_size,
        guidance_reader=lambda brand, query: "",
    )
    return service, operational


def test_manual_check_resolves_detail_then_returns_per_sku_verdict(
    session_factory,
):
    service, operational = _service(
        session_factory,
        rows={
            "s1": [_detail_row("s1")],
            "s2": [_detail_row("s2", name="白色衬衫")],
        },
        runtime=_Runtime(reject_product_ids={"s2"}),
    )

    submission = service.submit(
        brand="atlas",
        queries=[{"query": "白色连衣裙 女士", "skus": ["s1", "s2"]}],
        idempotency_key="manual-1",
    )
    assert submission.created is True
    service.run(submission.check_id)

    body = service.get(submission.check_id)
    assert body["checkId"] == submission.check_id
    assert body["brand"] == "atlas"
    assert body["status"] == "completed"
    assert set(body["queries"][0]) == {"query", "status", "products"}
    assert body["queries"][0]["products"] == [
        {"sku": "s1", "status": "completed", "isRelevant": True, "reason": None},
        {"sku": "s2", "status": "completed", "isRelevant": False, "reason": "不符合检索意图"},
    ]
    assert operational.calls == [("atlas", "s1"), ("atlas", "s2")]


def test_manual_check_falls_back_to_query_recall_when_detail_is_unavailable(
    session_factory,
):
    recall_row = _detail_row("s1", name="召回结果中的连衣裙")
    service, operational = _service(session_factory, runtime=_Runtime())
    operational.search_rows = [recall_row]
    submission = service.submit(
        brand="atlas",
        queries=[{"query": "连衣裙", "skus": ["s1"]}],
        idempotency_key="manual-recall-fallback",
    )

    service.run(submission.check_id)

    body = service.get(submission.check_id)
    assert body["status"] == "completed"
    assert body["queries"][0]["products"] == [
        {"sku": "s1", "status": "completed", "isRelevant": True, "reason": None},
    ]
    assert operational.calls == [("atlas", "s1")]
    assert operational.search_calls == [("atlas", "连衣裙")]


def test_manual_check_never_returns_detection_types_or_governance_actions(
    session_factory,
):
    service, _ = _service(
        session_factory, rows={"s1": [_detail_row("s1")]}
    )
    submission = service.submit(
        brand="atlas",
        queries=[{"query": "连衣裙", "skus": ["s1"]}],
        idempotency_key="manual-2",
    )
    service.run(submission.check_id)

    serialized = json.dumps(service.get(submission.check_id), ensure_ascii=False)
    assert "detectionTypes" not in serialized
    assert "blacklist" not in serialized.lower()
    assert "actionPlan" not in serialized


def test_duplicate_query_text_is_checked_independently(session_factory):
    service, operational = _service(
        session_factory,
        rows={"s1": [_detail_row("s1")], "s2": [_detail_row("s2")]},
    )
    submission = service.submit(
        brand="atlas",
        queries=[
            {"query": "连衣裙", "skus": ["s1"]},
            {"query": "连衣裙", "skus": ["s2"]},
        ],
        idempotency_key="manual-3",
    )
    service.run(submission.check_id)

    body = service.get(submission.check_id)
    assert [item["query"] for item in body["queries"]] == ["连衣裙", "连衣裙"]
    assert [item["products"][0]["sku"] for item in body["queries"]] == ["s1", "s2"]
    assert operational.calls == [("atlas", "s1"), ("atlas", "s2")]


def test_latest_check_filters_a_multi_query_record_to_requested_query(session_factory):
    service, _ = _service(
        session_factory,
        rows={"s1": [_detail_row("s1")], "s2": [_detail_row("s2")]},
    )
    submission = service.submit(
        brand="atlas",
        queries=[
            {"query": "连衣裙", "skus": ["s1"]},
            {"query": "外套", "skus": ["s2"]},
        ],
        idempotency_key="latest-query",
    )

    latest = service.get_latest(brand="atlas", query="外套")

    assert latest is not None
    assert latest["checkId"] == submission.check_id
    assert [item["query"] for item in latest["queries"]] == ["外套"]


def test_query_with_more_products_than_batch_size_is_chunked(session_factory):
    skus = [f"p{index}" for index in range(1, 6)]
    runtime = _Runtime()
    service, _ = _service(
        session_factory,
        rows={sku: [_detail_row(sku)] for sku in skus},
        runtime=runtime,
        batch_size=2,
    )
    submission = service.submit(
        brand="atlas",
        queries=[{"query": "连衣裙", "skus": skus}],
        idempotency_key="manual-4",
    )
    service.run(submission.check_id)

    assert len(runtime.calls) == 3
    assert [call[0][1] for call in runtime.calls] == [2, 2, 1]
    body = service.get(submission.check_id)
    assert [item["sku"] for item in body["queries"][0]["products"]] == skus


def test_missing_detail_is_reported_per_sku_without_failing_other_products(
    session_factory,
):
    service, _ = _service(
        session_factory, rows={"s1": [_detail_row("s1")]}
    )
    submission = service.submit(
        brand="atlas",
        queries=[{"query": "连衣裙", "skus": ["s1", "missing"]}],
        idempotency_key="manual-5",
    )
    service.run(submission.check_id)

    body = service.get(submission.check_id)
    query = body["queries"][0]
    assert query["status"] == "completed_with_errors"
    assert body["status"] == "completed_with_errors"
    assert query["products"][1] == {
        "sku": "missing",
        "status": "not_found",
        "isRelevant": None,
        "reason": "未查询到商品详情",
    }


def test_detail_backend_failure_is_reported_as_detail_error(session_factory):
    service, _ = _service(
        session_factory,
        rows={"s1": [_detail_row("s1")]},
        failing={"s1"},
    )
    submission = service.submit(
        brand="atlas",
        queries=[{"query": "连衣裙", "skus": ["s1"]}],
        idempotency_key="manual-6",
    )
    service.run(submission.check_id)

    product = service.get(submission.check_id)["queries"][0]["products"][0]
    assert product["status"] == "detail_error"
    assert product["isRelevant"] is None


def test_one_failing_query_does_not_block_other_queries(session_factory):
    class BrokenRuntime(_Runtime):
        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[1]["content"][0]["text"])
            if any(group["query"] == "broken" for group in payload["queries"]):
                raise TimeoutError("model timeout")
            return super().invoke(messages, **kwargs)

    service, _ = _service(
        session_factory,
        rows={"s1": [_detail_row("s1")], "s2": [_detail_row("s2")]},
        runtime=BrokenRuntime(),
    )
    submission = service.submit(
        brand="atlas",
        queries=[
            {"query": "broken", "skus": ["s1"]},
            {"query": "连衣裙", "skus": ["s2"]},
        ],
        idempotency_key="manual-7",
    )
    service.run(submission.check_id)

    queries = service.get(submission.check_id)["queries"]
    assert queries[0]["status"] == "failed"
    assert queries[0]["products"][0]["status"] == "model_error"
    assert queries[1]["status"] == "completed"
    assert queries[1]["products"][0]["isRelevant"] is True


def test_submit_is_idempotent_and_rejects_payload_collisions(session_factory):
    service, _ = _service(
        session_factory, rows={"s1": [_detail_row("s1")]}
    )
    payload = [{"query": "连衣裙", "skus": ["s1"]}]

    first = service.submit(
        brand="atlas", queries=payload, idempotency_key="same-key"
    )
    second = service.submit(
        brand="atlas", queries=payload, idempotency_key="same-key"
    )

    assert first.check_id == second.check_id
    assert first.created is True
    assert second.created is False
    with pytest.raises(ValueError, match="idempotency key collision"):
        service.submit(
            brand="atlas",
            queries=[{"query": "连衣裙", "skus": ["s1", "s2"]}],
            idempotency_key="same-key",
        )


def test_gateway_matches_identifier_aliases_and_deduplicates_repeated_skus():
    operational = _OperationalStub({
        "C123": [{"id": "alias_atlas_C123", "spu": "C123", "productName": "裙子"}],
        "M9": [{"modelCode": "M9", "webLabel": "卫衣"}],
        "C02157225800": [{"spu": "C02157225800,2157226800", "productName": "连衣裙"}],
        "C02157225800,2157226800": [{"spu": "C02157225800,2157226800", "productName": "连衣裙"}],
    })
    gateway = ProductDetailBatchGateway(operational)

    result = gateway.resolve(
        "atlas",
        ["C123", "M9", "C02157225800", "C02157225800,2157226800", "C123", "absent"],
    )

    assert result["C123"].status == "found"
    assert result["M9"].status == "found"
    assert result["C02157225800"].status == "found"
    assert result["C02157225800,2157226800"].status == "found"
    assert result["absent"].status == "not_found"
    assert isinstance(result["absent"], ProductDetailResult)
    assert operational.calls == [
        ("atlas", "C123"),
        ("atlas", "M9"),
        ("atlas", "C02157225800"),
        ("atlas", "C02157225800,2157226800"),
        ("atlas", "absent"),
    ]


def test_gateway_reports_invalid_detail_payload_without_aborting():
    class InvalidDetailClient:
        def get_raw_product_details(self, brand, product_id):
            return None

    result = ProductDetailBatchGateway(InvalidDetailClient()).resolve("atlas", ["s1"])

    assert result["s1"].status == "detail_error"
    assert result["s1"].reason == "商品详情接口返回格式无效"


def test_terminal_job_failure_is_visible_in_query_and_product_status(session_factory):
    service, _ = _service(
        session_factory,
        rows={"s1": [_detail_row("s1")]},
    )
    service.runtime_factory = lambda: (_ for _ in ()).throw(RuntimeError("model config missing"))
    submission = service.submit(
        brand="atlas",
        queries=[{"query": "连衣裙", "skus": ["s1"]}],
        idempotency_key="manual-terminal-failure",
    )

    service.run(submission.check_id)

    body = service.get(submission.check_id)
    assert body["status"] == "failed"
    assert body["queries"][0]["status"] == "failed"
    assert body["queries"][0]["products"][0] == {
        "sku": "s1",
        "status": "model_error",
        "isRelevant": None,
        "reason": "model config missing",
    }


def test_guidance_failure_is_isolated_to_its_query(session_factory):
    operational = _OperationalStub({
        "s1": [_detail_row("s1")],
        "s2": [_detail_row("s2")],
    })
    service = ManualRelevanceCheckService(
        repository=ManualRelevanceCheckRepository(session_factory),
        gateway=ProductDetailBatchGateway(operational),
        runtime_factory=lambda: _Runtime(),
        image_cache=_Cache(),
        batch_size=240,
        guidance_reader=lambda brand, query: (
            (_ for _ in ()).throw(RuntimeError("guidance unavailable"))
            if query == "broken"
            else ""
        ),
    )
    submission = service.submit(
        brand="atlas",
        queries=[
            {"query": "broken", "skus": ["s1"]},
            {"query": "连衣裙", "skus": ["s2"]},
        ],
        idempotency_key="manual-guidance-failure",
    )

    service.run(submission.check_id)

    queries = service.get(submission.check_id)["queries"]
    assert queries[0]["status"] == "failed"
    assert queries[0]["products"][0]["reason"] == "guidance unavailable"
    assert queries[1]["status"] == "completed"


def test_status_enums_are_pinned():
    assert {item.value for item in ManualCheckStatus} == {
        "queued", "running", "completed", "completed_with_errors", "failed",
    }
    assert {item.value for item in ManualQueryStatus} == {
        "pending", "running", "completed", "completed_with_errors", "failed",
    }
    assert {item.value for item in ManualProductStatus} == {
        "pending", "fetching_detail", "checking", "completed",
        "not_found", "detail_error", "model_error",
    }
