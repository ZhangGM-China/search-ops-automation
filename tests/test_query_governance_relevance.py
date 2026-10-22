import json
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.governance.query_identity import normalize_governance_query_key
from src.query_governance.models import NodeStatus, QuerySpec
from src.query_governance.product_adapter import ProductAdapterRegistry
from src.query_governance.relevance import (
    ImageAsset,
    ImageCacheService,
    PillowImageTransformer,
    ProcessedImage,
    RelevanceCheckRunner,
    RelevanceError,
    RelevanceOutputError,
    RemoteImageFetcher,
    SqlAlchemyRelevanceStore,
    TencentCosObjectStorage,
    build_relevance_messages,
    normalize_image_url,
)
from src.persistence.models import Base
from src.query_governance.models import QueryGovernanceRelevanceGuidance, QueryGovernanceRelevanceRecord


@dataclass
class _Fetcher:
    calls: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> tuple[bytes, str]:
        self.calls.append(url)
        return b"raw-image", "image/png"


class _Transformer:
    def transform(self, payload: bytes, content_type: str):
        assert payload == b"raw-image"
        return b"jpeg-image", "image/jpeg"


@dataclass
class _Storage:
    uploads: list[tuple[str, bytes, str]] = field(default_factory=list)

    def put(self, key: str, payload: bytes, content_type: str) -> str:
        self.uploads.append((key, payload, content_type))
        return f"https://cos.example/{key}"


def test_image_cache_normalizes_deduplicates_and_uploads_once():
    fetcher = _Fetcher()
    storage = _Storage()
    cache = ImageCacheService(
        fetcher=fetcher,
        transformer=_Transformer(),
        storage=storage,
        max_workers=2,
    )

    assets = cache.prepare_many(["//cdn.example/a.png", "https://cdn.example/a.png", ""])

    assert normalize_image_url("//cdn.example/a.png") == "https://cdn.example/a.png"
    assert fetcher.calls == ["https://cdn.example/a.png"]
    assert assets["https://cdn.example/a.png"].available is True
    assert assets["https://cdn.example/a.png"].url.startswith("https://cos.example/")
    assert assets[""].available is False
    assert assets[""].warning_code == "IMAGE_URL_MISSING"
    assert len(storage.uploads) == 1


class _Response:
    def __init__(self, status_code=200, payload=b"image-bytes", content_type="image/jpeg", content_length=None):
        self.status_code = status_code
        self.content = payload
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self.closed = False

    def iter_content(self, chunk_size):
        del chunk_size
        yield self.content

    def close(self):
        self.closed = True


def test_remote_image_fetcher_retries_server_failure_and_returns_bytes():
    responses = [_Response(status_code=503), _Response(payload=b"ok")]
    calls = []
    fetcher = RemoteImageFetcher(
        max_attempts=2,
        request_get=lambda *args, **kwargs: (calls.append((args, kwargs)) or responses.pop(0)),
        sleep=lambda value: calls.append(("sleep", value)),
    )

    payload, content_type = fetcher.fetch("https://cdn.example/a.jpg")

    assert payload == b"ok"
    assert content_type == "image/jpeg"
    assert len([call for call in calls if isinstance(call[0], tuple)]) == 2
    assert any(call[0] == "sleep" for call in calls)


def test_remote_image_fetcher_uses_configured_proxy_for_image_requests():
    calls = []

    def request_get(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response(payload=b"proxied-image")

    payload, content_type = RemoteImageFetcher(
        proxy_url="http://image-proxy.example:8080",
        request_get=request_get,
    ).fetch("https://cdn.example/a.jpg")

    assert payload == b"proxied-image"
    assert content_type == "image/jpeg"
    assert calls[0][1]["proxies"] == {
        "http": "http://image-proxy.example:8080",
        "https": "http://image-proxy.example:8080",
    }


def test_remote_image_fetcher_does_not_add_proxy_without_configuration():
    calls = []

    def request_get(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response(payload=b"direct-image")

    payload, content_type = RemoteImageFetcher(request_get=request_get).fetch(
        "https://cdn.example/a.jpg"
    )

    assert payload == b"direct-image"
    assert content_type == "image/jpeg"
    assert "proxies" not in calls[0][1]


@pytest.mark.parametrize(
    "proxy_url",
    ["ftp://proxy.example:8080", "proxy.example:8080", "http://"],
)
def test_remote_image_fetcher_rejects_invalid_proxy_url(proxy_url):
    with pytest.raises(ValueError, match=r"http\(s\)"):
        RemoteImageFetcher(proxy_url=proxy_url)


def test_remote_image_fetcher_rejects_invalid_media_and_download_size():
    fetcher = RemoteImageFetcher(request_get=lambda *args, **kwargs: _Response(content_type="text/html"))
    with pytest.raises(RelevanceError) as media_error:
        fetcher.fetch("https://cdn.example/video.m3u8")
    assert media_error.value.error_code == "IMAGE_UNSUPPORTED_MEDIA"

    large = RemoteImageFetcher(
        max_bytes=3,
        request_get=lambda *args, **kwargs: _Response(payload=b"1234"),
    )
    with pytest.raises(RelevanceError) as size_error:
        large.fetch("https://cdn.example/a.jpg")
    assert size_error.value.error_code == "IMAGE_TOO_LARGE"


def test_remote_image_fetcher_rejects_invalid_url_status_content_and_empty_body():
    with pytest.raises(RelevanceError) as url_error:
        RemoteImageFetcher().fetch("file:///tmp/a.jpg")
    assert url_error.value.error_code == "IMAGE_URL_INVALID"

    unavailable = RemoteImageFetcher(request_get=lambda *args, **kwargs: _Response(status_code=404))
    with pytest.raises(RelevanceError) as status_error:
        unavailable.fetch("https://cdn.example/a.jpg")
    assert status_error.value.error_code == "IMAGE_HTTP_UNAVAILABLE"

    unsupported = RemoteImageFetcher(request_get=lambda *args, **kwargs: _Response(content_type="text/html"))
    with pytest.raises(RelevanceError) as content_error:
        unsupported.fetch("https://cdn.example/a.jpg")
    assert content_error.value.error_code == "IMAGE_UNSUPPORTED_MEDIA"

    advertised = RemoteImageFetcher(
        max_bytes=3,
        request_get=lambda *args, **kwargs: _Response(payload=b"123", content_length=4),
    )
    with pytest.raises(RelevanceError) as advertised_error:
        advertised.fetch("https://cdn.example/a.jpg")
    assert advertised_error.value.error_code == "IMAGE_TOO_LARGE"

    empty = RemoteImageFetcher(request_get=lambda *args, **kwargs: _Response(payload=b""))
    with pytest.raises(RelevanceError) as empty_error:
        empty.fetch("https://cdn.example/a.jpg")
    assert empty_error.value.error_code == "IMAGE_EMPTY"


def test_remote_image_fetcher_maps_timeout_and_request_failures():
    timeout_calls = []
    timeout_fetcher = RemoteImageFetcher(
        max_attempts=1,
        request_get=lambda *args, **kwargs: (_ for _ in ()).throw(__import__("requests").exceptions.Timeout()),
        sleep=lambda value: timeout_calls.append(value),
    )
    with pytest.raises(RelevanceError) as timeout_error:
        timeout_fetcher.fetch("https://cdn.example/a.jpg")
    assert timeout_error.value.error_code == "IMAGE_DOWNLOAD_TIMEOUT"

    request_fetcher = RemoteImageFetcher(
        max_attempts=1,
        request_get=lambda *args, **kwargs: (_ for _ in ()).throw(__import__("requests").exceptions.RequestException()),
    )
    with pytest.raises(RelevanceError) as request_error:
        request_fetcher.fetch("https://cdn.example/a.jpg")
    assert request_error.value.error_code == "IMAGE_DOWNLOAD_FAILED"


def test_remote_image_fetcher_rejects_invalid_constructor_limits():
    with pytest.raises(ValueError):
        RemoteImageFetcher(timeout=0)
    with pytest.raises(ValueError):
        ImageCacheService(fetcher=_Fetcher(), transformer=_Transformer(), storage=_Storage(), max_workers=0)
    with pytest.raises(ValueError):
        PillowImageTransformer(max_edge=0)


def test_pillow_transformer_resizes_and_compresses_image():
    from PIL import Image
    source = __import__("io").BytesIO()
    Image.new("RGBA", (1600, 800), (255, 0, 0, 128)).save(source, format="PNG")

    result = PillowImageTransformer(max_edge=400).transform(source.getvalue(), "image/png")

    assert isinstance(result, ProcessedImage)
    assert result.content_type == "image/jpeg"
    assert result.width == 400
    assert result.height == 200
    with Image.open(__import__("io").BytesIO(result.payload)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (400, 200)


def test_pillow_transformer_handles_rgb_and_reduces_large_output():
    from PIL import Image
    source = __import__("io").BytesIO()
    Image.new("RGB", (20, 20), "blue").save(source, format="JPEG", quality=95)
    result = PillowImageTransformer(max_edge=20, max_output_bytes=200, quality=95).transform(source.getvalue(), "image/jpeg")
    assert result.width == 20
    assert result.height == 20
    assert len(result.payload) <= 200 or len(result.payload) > 0


def test_tencent_cos_storage_uses_client_and_encodes_public_url():
    class Client:
        def __init__(self):
            self.calls = []

        def put_object(self, **kwargs):
            self.calls.append(kwargs)

    client = Client()
    storage = TencentCosObjectStorage(
        region="ap-shanghai",
        bucket="query-governance-123",
        public_base_url="https://img.example.com",
        client=client,
    )

    url = storage.put("query-governance/a b.jpg", b"data", "image/jpeg")

    assert url == "https://img.example.com/query-governance/a%20b.jpg"
    assert client.calls[0]["Bucket"] == "query-governance-123"
    assert client.calls[0]["Key"] == "query-governance/a b.jpg"


def test_relevance_messages_include_guidance_text_and_cached_image_url():
    spec = QuerySpec("q1", "atlas", "连衣裙")
    products = [{
        "productId": "p1",
        "productName": "红色连衣裙",
        "description": "女装",
        "gender": "WOMAN",
        "category": "连衣裙",
        "imageUrl": "https://origin.example/p1.jpg",
    }]
    assets = {"https://origin.example/p1.jpg": ImageAsset(
        source_url="https://origin.example/p1.jpg",
        url="https://cos.example/p1.jpg",
        available=True,
    )}

    messages = build_relevance_messages(
        spec,
        products,
        assets,
        guidance="图片中不能明确证明材质时不要臆测。",
    )

    assert messages[0]["role"] == "system"
    assert "图片中不能明确证明材质时不要臆测" in messages[0]["content"]
    payload = json.loads(messages[1]["content"][0]["text"])
    assert payload["columns"][:3] == ["productId", "productName", "description"]
    assert payload["queries"][0]["products"][0][0] == "p1"
    assert payload["queries"][0]["image_available"] == [True]
    assert messages[1]["content"][2]["image_url"]["url"] == "https://cos.example/p1.jpg"


def test_relevance_prompt_requires_compact_output_without_product_ids():
    messages = build_relevance_messages(
        QuerySpec("q1", "atlas", "连衣裙"),
        [{"productId": "p1"}, {"productId": "p2"}],
        {},
    )

    prompt = messages[0]["content"]
    assert '"relevant"' in prompt
    assert '"image_checked"' in prompt
    assert "不要在输出中返回商品 ID" in prompt
    assert '"product_id"' not in prompt


def test_relevance_prompt_uses_actual_image_markers_and_variant_evidence():
    product = ProductAdapterRegistry().adapt(
        "summit",
        {"dsmCode": "305469", "models": [{"modelCode": "8561855"}]},
        1,
    )
    messages = build_relevance_messages(
        QuerySpec("q1", "summit", "卡扣"),
        [product],
        {},
    )

    prompt = messages[0]["content"]
    assert "图片前的 product_id 标记指定所属商品" in prompt
    assert "至少一个款式完整满足关键条件" in prompt
    assert "image_for=" not in prompt
    assert "你是电商搜索结果相关性检测 Agent。" in prompt


@dataclass
class _Runtime:
    response: Any
    calls: list[tuple[list[Mapping[str, Any]], dict[str, Any]]] = field(default_factory=list)

    def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response


@dataclass
class _RecordStore:
    relevant: set[tuple[str, str, str]] = field(default_factory=set)
    saved: list[dict[str, Any]] = field(default_factory=list)
    guidance: str = ""

    def get_relevant_product_ids(self, brand: str, query: str, product_ids):
        return {
            product_id
            for product_id in product_ids
            if (brand, query, product_id) in self.relevant
        }

    def save_relevant(self, brand: str, query: str, product_id: str, **kwargs):
        self.relevant.add((brand, query, product_id))
        self.saved.append({"brand": brand, "query": query, "product_id": product_id, **kwargs})

    def get_guidance(self, brand: str, query: str) -> str:
        return self.guidance


class _Cache:
    def __init__(self, assets):
        self.assets = assets

    def prepare_many(self, urls):
        return {url: self.assets.get(url, ImageAsset(source_url=url, available=False, warning_code="IMAGE_DOWNLOAD_FAILED")) for url in urls}


class _Gate:
    def __init__(self):
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.exited += 1


def _response(payload: Mapping[str, Any], *, model_name: str = "qwen-vl-max"):
    return type("Invocation", (), {
        "response": type("Response", (), {"content": json.dumps(payload, ensure_ascii=False)})(),
        "audit": {"modelName": model_name, "tokenUsage": 12},
    })()


def test_invalid_single_query_output_preserves_model_usage():
    runner = RelevanceCheckRunner(runtime=_Runtime(_response({"results": []})),
                                  record_store=_RecordStore(), image_cache=_Cache({}))
    outcome = runner.run(QuerySpec("q1", "atlas", "裙子"), {
        "current_products": [{"productId": "p1", "productName": "裙子"}],
    })
    assert outcome.status is NodeStatus.ERROR
    assert outcome.result["modelCallAudits"][0]["tokenUsage"] == 12


def test_runner_reuses_relevant_record_and_persists_only_new_relevant_product():
    runtime = _Runtime(_response({
        "results": [{
            "query": "连衣裙",
            "products": [
                {"product_id": "p2", "is_relevant": True, "reason": None},
            ],
        }],
    }))
    store = _RecordStore(relevant={("atlas", "连衣裙", "p1")}, guidance="规则")
    assets = {
        "https://cdn/p1.jpg": ImageAsset("https://cdn/p1.jpg", "https://cos/p1.jpg", True),
        "https://cdn/p2.jpg": ImageAsset("https://cdn/p2.jpg", "https://cos/p2.jpg", True),
    }
    runner = RelevanceCheckRunner(runtime=runtime, record_store=store, image_cache=_Cache(assets))

    result = runner.run(QuerySpec("q1", "atlas", "连衣裙"), {
        "current_products": [
            {"productId": "p1", "productName": "已通过", "mainImage": "https://cdn/p1.jpg"},
            {"productId": "p2", "productName": "待检测", "mainImage": "https://cdn/p2.jpg"},
        ],
    })

    assert result.status is NodeStatus.EXECUTED
    assert result.result["reused_product_ids"] == ["p1"]
    assert result.result["relevant_product_ids"] == ["p2"]
    assert [item["product_id"] for item in store.saved] == ["p2"]
    assert len(runtime.calls) == 1
    assert "https://cos/p1.jpg" not in json.dumps(runtime.calls[0][0], ensure_ascii=False)
    assert "https://cos/p2.jpg" in json.dumps(runtime.calls[0][0], ensure_ascii=False)


def test_runner_emits_product_blacklist_action_for_irrelevant_products():
    runtime = _Runtime(_response({
        "results": [{
            "query": "鞋",
            "products": [
                {"product_id": "p1", "is_relevant": False, "reason": "这是上衣"},
            ],
        }],
    }))
    runner = RelevanceCheckRunner(
        runtime=runtime,
        record_store=_RecordStore(),
        image_cache=_Cache({}),
    )

    result = runner.run(
        QuerySpec("q1", "atlas", "鞋"),
        {"current_products": [{"productId": "p1", "productName": "上衣"}]},
    )

    assert result.status is NodeStatus.EXECUTED
    assert result.action == {
        "type": "product_blacklist",
        "brand": "atlas",
        "query": "鞋",
        "product_ids": ["p1"],
        "reasons": {"p1": "这是上衣"},
        "product_names": {"p1": "上衣"},
        "product_images": {"p1": ""},
    }


def test_runner_warns_on_image_failure_but_keeps_text_detection():
    runtime = _Runtime(_response({
        "results": [{
            "query": "鞋",
            "products": [
                {"product_id": "p1", "is_relevant": False, "reason": "商品不是鞋"},
            ],
        }],
    }))
    store = _RecordStore()
    runner = RelevanceCheckRunner(
        runtime=runtime,
        record_store=store,
        image_cache=_Cache({"https://cdn/p1.jpg": ImageAsset("https://cdn/p1.jpg", available=False, warning_code="IMAGE_DECODE_FAILED")}),
    )

    result = runner.run(QuerySpec("q1", "atlas", "鞋"), {
        "current_products": [{"productId": "p1", "productName": "上衣", "mainImage": "https://cdn/p1.jpg"}],
    })

    assert result.status is NodeStatus.EXECUTED
    assert result.result["blacklist_candidates"][0]["detection_types"] == ["Text"]
    assert result.result["blacklist_candidates"][0]["image_checked"] is False
    assert result.result["warnings"][0]["code"] == "IMAGE_DECODE_FAILED"
    assert result.result["telemetry"] == {
        "batch_index": 1,
        "batch_count": 1,
        "product_count": 1,
        "image_download_success_count": 0,
        "image_download_failure_count": 1,
    }


def test_runner_returns_error_for_invalid_model_output_without_blacklist_candidates():
    runtime = _Runtime(_response({"results": [{"query": "鞋", "products": []}]}))
    runner = RelevanceCheckRunner(
        runtime=runtime,
        record_store=_RecordStore(),
        image_cache=_Cache({"": ImageAsset("", available=False, warning_code="IMAGE_URL_MISSING")}),
    )

    result = runner.run(QuerySpec("q1", "atlas", "鞋"), {
        "current_products": [{"productId": "p1", "productName": "上衣"}],
    })

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "RELEVANCE_OUTPUT_INVALID"
    assert result.result["blacklist_candidates"] == []
    assert result.result["modelCallAudits"][0]["tokenUsage"] == 12


def test_runner_preserves_stage_telemetry_when_model_output_is_invalid():
    runtime = _Runtime(_response({"results": [{"query": "鞋", "products": []}]}))
    runner = RelevanceCheckRunner(
        runtime=runtime,
        record_store=_RecordStore(),
        image_cache=_Cache({"": ImageAsset("", available=False, warning_code="IMAGE_URL_MISSING")}),
    )

    result = runner.run(QuerySpec("q1", "atlas", "鞋"), {
        "current_products": [{"productId": "p1", "productName": "上衣"}],
    })

    assert result.error_code == "RELEVANCE_OUTPUT_INVALID"
    assert result.telemetry["imagePreparationSeconds"] >= 0
    assert result.telemetry["modelSeconds"] >= 0
    assert result.telemetry["modelCallCount"] == 1
    assert result.telemetry["executionSeconds"] == round(
        result.telemetry["imagePreparationSeconds"] + result.telemetry["modelSeconds"], 6
    )


def test_runner_returns_errors_for_missing_input_and_record_read_failure():
    runner = RelevanceCheckRunner(
        runtime=_Runtime(_response({"results": []})),
        record_store=_RecordStore(),
        image_cache=_Cache({}),
    )
    missing = runner.run(QuerySpec("q1", "atlas", "鞋"), {})
    assert missing.error_code == "RELEVANCE_INPUT_MISSING"

    class BrokenStore(_RecordStore):
        def get_relevant_product_ids(self, brand, query, product_ids):
            raise RuntimeError("db unavailable")

    broken = RelevanceCheckRunner(
        runtime=_Runtime(_response({"results": []})),
        record_store=BrokenStore(),
        image_cache=_Cache({}),
    )
    result = broken.run(QuerySpec("q1", "atlas", "鞋"), {"current_products": [{"productId": "p1"}]})
    assert result.error_code == "RELEVANCE_RECORD_READ_FAILED"


def test_runner_maps_timeout_and_save_failures():
    class TimeoutRuntime(_Runtime):
        def invoke(self, messages, **kwargs):
            raise TimeoutError("model timeout")

    timeout = RelevanceCheckRunner(
        runtime=TimeoutRuntime(None),
        record_store=_RecordStore(),
        image_cache=_Cache({}),
    )
    result = timeout.run(QuerySpec("q1", "atlas", "鞋"), {"current_products": [{"productId": "p1"}]})
    assert result.error_code == "RELEVANCE_MODEL_TIMEOUT"

    class BrokenSaveStore(_RecordStore):
        def save_relevant(self, *args, **kwargs):
            raise RuntimeError("save failed")

    broken = RelevanceCheckRunner(
        runtime=_Runtime(_response({"results": [{"query": "鞋", "products": [{"product_id": "p1", "is_relevant": True}]}]})),
        record_store=BrokenSaveStore(),
        image_cache=_Cache({}),
    )
    result = broken.run(QuerySpec("q1", "atlas", "鞋"), {"current_products": [{"productId": "p1"}]})
    assert result.error_code == "RELEVANCE_RECORD_SAVE_FAILED"


def test_runner_passes_langfuse_context_to_runtime_and_guidance_store():
    runtime = _Runtime(_response({
        "results": [{"query": "鞋", "products": [{"product_id": "p1", "is_relevant": True}]}],
    }))
    store = _RecordStore(guidance="不要把包当鞋")
    runner = RelevanceCheckRunner(
        runtime=runtime,
        record_store=store,
        image_cache=_Cache({"": ImageAsset("", available=False, warning_code="IMAGE_URL_MISSING")}),
    )
    result = runner.run(QuerySpec("q1", "atlas", "鞋"), {
        "current_products": [{"productId": "p1", "productName": "鞋"}],
        "callbacks": ["callback"],
        "langfuse_config": {"metadata": {"query_id": "q1"}, "run_name": "query-governance.relevance"},
    })

    assert result.status is NodeStatus.EXECUTED
    kwargs = runtime.calls[0][1]
    assert kwargs["callbacks"] == ["callback"]
    assert kwargs["metadata"] == {"query_id": "q1"}
    assert kwargs["run_name"] == "query-governance.relevance"
    assert "不要把包当鞋" in runtime.calls[0][0][0]["content"]


def test_runner_uses_configured_multimodal_concurrency_controller_for_model_call():
    runtime = _Runtime(_response({
        "results": [{"query": "鞋", "products": [{"product_id": "p1", "is_relevant": True}]}],
    }))
    gate = _Gate()
    runner = RelevanceCheckRunner(
        runtime=runtime,
        record_store=_RecordStore(),
        image_cache=_Cache({"": ImageAsset("", available=False, warning_code="IMAGE_URL_MISSING")}),
        concurrency_controller=gate,
    )

    result = runner.run(QuerySpec("q1", "atlas", "鞋"), {"current_products": [{"productId": "p1"}]})

    assert result.status is NodeStatus.EXECUTED
    assert gate.entered == 1
    assert gate.exited == 1


def test_sqlalchemy_relevance_store_reads_guidance_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'relevance.db'}")
    Base.metadata.create_all(engine, tables=[QueryGovernanceRelevanceRecord.__table__, QueryGovernanceRelevanceGuidance.__table__])
    factory = lambda: Session(engine)
    store = SqlAlchemyRelevanceStore(factory)
    with factory() as db:
        db.add(QueryGovernanceRelevanceGuidance(brand="atlas", query_text="鞋", rules_text="只认鞋", status="active"))
        db.commit()

    store.save_relevant("atlas", "鞋", "p1", model_name="qwen-vl-max")
    store.save_relevant("atlas", "鞋", "p1", model_name="qwen-vl-max")

    assert store.get_relevant_product_ids("atlas", "鞋", ["p1", "p2"]) == {"p1"}
    assert store.get_guidance("atlas", "鞋") == "只认鞋"
    with factory() as db:
        assert len(db.execute(select(QueryGovernanceRelevanceRecord)).scalars().all()) == 1
    engine.dispose()


def test_sqlalchemy_relevance_store_matches_normalized_query_but_preserves_original_text(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'normalized-relevance.db'}")
    Base.metadata.create_all(engine, tables=[QueryGovernanceRelevanceRecord.__table__])
    store = SqlAlchemyRelevanceStore(lambda: Session(engine))

    original_query = "  女式  鞋  "
    store.save_relevant("atlas", original_query, "p1", model_name="qwen-vl-max")

    assert store.get_relevant_product_ids("atlas", "女式 鞋", ["p1"]) == {"p1"}
    assert store.get_relevant_product_ids("atlas", " 女式鞋 ", ["p1"]) == set()
    with Session(engine) as db:
        row = db.execute(select(QueryGovernanceRelevanceRecord)).scalar_one()
        assert row.query_text == original_query
        assert row.query_key == normalize_governance_query_key("atlas", original_query)
    engine.dispose()


def test_sqlalchemy_relevance_store_does_not_reuse_across_different_queries(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'query-scope.db'}")
    Base.metadata.create_all(engine, tables=[QueryGovernanceRelevanceRecord.__table__])
    store = SqlAlchemyRelevanceStore(lambda: Session(engine))

    store.save_relevant("atlas", "鞋", "p1")

    assert store.get_relevant_product_ids("atlas", "包", ["p1"]) == set()
    with Session(engine) as db:
        assert len(db.execute(select(QueryGovernanceRelevanceRecord)).scalars().all()) == 1
    engine.dispose()


def test_sqlalchemy_relevance_store_keeps_concurrent_duplicate_writes_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent-relevance.db'}")
    Base.metadata.create_all(engine, tables=[QueryGovernanceRelevanceRecord.__table__])
    store = SqlAlchemyRelevanceStore(lambda: Session(engine))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.save_relevant("atlas", "鞋", "p1"), range(8)))

    with Session(engine) as db:
        rows = db.execute(select(QueryGovernanceRelevanceRecord)).scalars().all()
        assert len(rows) == 1
        assert rows[0].query_key == "鞋"
    engine.dispose()


def test_summit_product_input_keeps_variant_identity():
    product = ProductAdapterRegistry().adapt("summit", {
        "productId": "PARENT",
        "productName": "徒步鞋",
        "models": [
            {"modelCode": "M-1", "webLabel": "黑色", "media": {"images": ["https://cdn/m1.jpg"]}},
            {"modelCode": "M-2", "webLabel": "蓝色", "media": {"images": ["https://cdn/m2.jpg"]}},
        ],
    }, rank=1)

    assert product.product_id == "PARENT"
    assert [variant.product_id for variant in product.variants] == ["M-1", "M-2"]


def test_output_parser_rejects_unknown_product_ids():
    with pytest.raises(RelevanceOutputError, match="unknown"):
        RelevanceCheckRunner.parse_output(
            {"results": [{"query": "鞋", "products": [{"product_id": "unknown", "is_relevant": True}]}]},
            query="鞋",
            products=[{"productId": "p1"}],
        )


def test_output_parser_supports_compact_arrays_and_rejects_duplicates_or_mismatches():
    decisions = RelevanceCheckRunner.parse_output(
        {"results": [{"query": "鞋", "relevant": [1, 0], "image_checked": [1, 0], "reasons": {"2": "不是鞋"}}]},
        query="鞋",
        products=[{"productId": "p1"}, {"productId": "p2"}],
    )
    assert [item.is_relevant for item in decisions] == [True, False]
    assert decisions[1].image_checked is False

    with pytest.raises(RelevanceOutputError, match="duplicate"):
        RelevanceCheckRunner.parse_output(
            {"results": [{"query": "鞋", "products": [{"product_id": "p1", "is_relevant": True}, {"product_id": "p1", "is_relevant": True}]}]},
            query="鞋",
            products=[{"productId": "p1"}, {"productId": "p2"}],
        )
    with pytest.raises(RelevanceOutputError, match="does not match"):
        RelevanceCheckRunner.parse_output(
            {"results": [{"query": "包", "products": []}]},
            query="鞋",
            products=[{"productId": "p1"}],
        )


def test_output_parser_requires_image_checked_for_compact_arrays():
    with pytest.raises(RelevanceOutputError, match="image_checked"):
        RelevanceCheckRunner.parse_output(
            {"results": [{"query": "鞋", "relevant": [1], "reasons": {}}]},
            query="鞋",
            products=[{"productId": "p1"}],
        )
