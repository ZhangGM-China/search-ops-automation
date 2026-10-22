"""Offline coverage for the shared-image guard.

One Harbor photo was written onto thousands of product documents by the search
index (incident 2026-09-29~10-12), and the pipeline fed that image to the
relevance model, which then described a vest as denim shorts.  These tests pin
the fail-safe: an image URL reused by two or more distinct products in the same
run is never downloaded, never reused from the asset cache, and never sent to
the model.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.query_governance.models import QuerySpec
from src.query_governance.product_adapter import ProductAdapterRegistry
from src.query_governance.relevance import (
    IMAGE_URL_SHARED,
    CachedImageRecord,
    ImageCacheAssetStore,
    ImageCacheService,
    ProcessedImage,
    shared_image_urls,
)
from src.query_governance.relevance_batch import (
    RelevanceBatchInput,
    _PreparedQuery,
    build_relevance_batch_messages,
)

SHARED_URL = "https://asset.harbor.example.test/media/catalog/product/f/a/fae7537d5904011483cc1bd867f13b0432a5a1c1_xxl-1.jpg"
VEST_URL = "https://asset.harbor.example.test/media/catalog/product/1/c/1cbb6a1c5abca0cd17e85f5cff7bbbe581afcae0.jpg"
OTHER_URL = "https://asset.harbor.example.test/media/catalog/product/9/2/923c2684ff00112233445566778899aabbccddee.jpg"


@dataclass
class _Fetcher:
    calls: list[str]

    def fetch(self, url: str) -> tuple[bytes, str]:
        self.calls.append(url)
        return b"source", "image/png"


class _Transformer:
    def transform(self, payload: bytes, content_type: str) -> ProcessedImage:
        return ProcessedImage(b"jpeg", "image/jpeg", 20, 20)


class _Storage:
    def put(self, key: str, payload: bytes, content_type: str) -> str:
        return f"https://cos.example/{key}"


class _Store(ImageCacheAssetStore):
    def __init__(self, record: CachedImageRecord | None = None):
        self.record = record
        self.gets: list[tuple[str, str, str | None]] = []
        self.saved: list[dict[str, object]] = []

    def get(self, brand, product_id, spu_id):
        self.gets.append((brand, product_id, spu_id))
        return self.record

    def save_success(self, **kwargs):
        self.saved.append(dict(kwargs))
        self.record = None


def _product(product_id: str, image_url: str):
    return ProductAdapterRegistry().adapt(
        "harbor",
        {
            "productId": product_id,
            "spuId": product_id,
            "mainImage": image_url,
            "productName": f"商品-{product_id}",
        },
        1,
    )


def _service(store: ImageCacheAssetStore | None, fetcher: _Fetcher) -> ImageCacheService:
    return ImageCacheService(
        fetcher=fetcher,
        transformer=_Transformer(),
        storage=_Storage(),
        asset_store=store,
    )


def _ready_record(product_id: str, source_url: str) -> CachedImageRecord:
    return CachedImageRecord(
        brand="harbor",
        product_id=product_id,
        spu_id=product_id,
        source_url=source_url,
        cached_url="https://cos.example/prod/v1/fcfcce9d.jpg",
        storage_key="query-governance/images/prod/v1/fcfcce9d.jpg",
        content_type="image/jpeg",
        width=512,
        height=768,
    )


def test_shared_image_urls_groups_only_reused_images():
    products = [
        _product("1328942002", SHARED_URL),
        _product("1323994001", SHARED_URL),
        _product("1347226001", VEST_URL),
    ]

    assert shared_image_urls(products) == {SHARED_URL: ["1328942002", "1323994001"]}


def test_shared_image_is_skipped_and_never_reused_from_asset_cache():
    store = _Store(record=_ready_record("1328942002", SHARED_URL))
    fetcher = _Fetcher([])

    assets = _service(store, fetcher).prepare_products(
        [_product("1328942002", SHARED_URL), _product("1323994001", SHARED_URL)],
        brand="harbor",
    )

    asset = assets[SHARED_URL]
    assert asset.available is False
    assert asset.url is None
    assert asset.warning_code == IMAGE_URL_SHARED
    assert "2" in (asset.warning_message or "")
    assert store.gets == []
    assert store.saved == []
    assert fetcher.calls == []


def test_shared_image_is_not_downloaded_without_asset_store():
    fetcher = _Fetcher([])

    assets = _service(None, fetcher).prepare_products(
        [
            _product("1328942002", SHARED_URL),
            _product("1323994001", SHARED_URL),
            _product("1347226001", VEST_URL),
        ],
        brand="harbor",
    )

    assert assets[SHARED_URL].available is False
    assert assets[SHARED_URL].warning_code == IMAGE_URL_SHARED
    assert assets[VEST_URL].available is True
    assert fetcher.calls == [VEST_URL]


def test_distinct_images_are_prepared_normally():
    fetcher = _Fetcher([])

    assets = _service(None, fetcher).prepare_products(
        [_product("1328942002", VEST_URL), _product("1347226001", OTHER_URL)],
        brand="harbor",
    )

    assert assets[VEST_URL].available is True
    assert assets[OTHER_URL].available is True
    assert sorted(fetcher.calls) == sorted([VEST_URL, OTHER_URL])


def _prepared(products, assets) -> _PreparedQuery:
    spec = QuerySpec(query_id="q-1", brand="harbor", query="马甲")
    return _PreparedQuery(
        item=RelevanceBatchInput(spec=spec, products=products),
        products=list(products),
        pending=list(products),
        product_ids=[str(product.product_id) for product in products],
        reused_product_ids=[],
        assets=assets,
        guidance="",
        result={},
    )


def test_shared_image_is_absent_from_model_messages():
    bad = _product("1328942002", SHARED_URL)
    peer = _product("1323994001", SHARED_URL)
    good = _product("1347226001", VEST_URL)
    assets = {
        SHARED_URL: _service(None, _Fetcher([])).prepare_products([bad, peer])[SHARED_URL],
        VEST_URL: _service(None, _Fetcher([])).prepare_products([good])[VEST_URL],
    }

    messages = build_relevance_batch_messages([_prepared([bad, good], assets)])

    content = messages[-1]["content"]
    payload = content[0]["text"]
    assert '"image_available":[false,true]' in payload.replace(" ", "")
    assert "fae7537d5904011483cc1bd867f13b0432a5a1c1" not in payload

    image_parts = [part for part in content if part.get("type") == "image_url"]
    assert len(image_parts) == 1
    assert SHARED_URL not in image_parts[0]["image_url"]["url"]

    labels = [part["text"] for part in content if part.get("type") == "text" and part["text"].startswith("query=")]
    assert labels == ["query=马甲 product_id=1347226001"]

@dataclass
class _Runtime:
    response: object

    def invoke(self, messages, **kwargs):
        return self.response


class _Records:
    def get_relevant_product_ids(self, brand, query, product_ids):
        return set()

    def save_relevant(self, brand, query, product_id, **kwargs):
        return None

    def get_guidance(self, brand, query):
        return ""


def _invocation(payload):
    import json

    return type(
        "Invocation",
        (),
        {
            "response": type("Response", (), {"content": json.dumps(payload, ensure_ascii=False)})(),
            "audit": {"modelName": "qwen-vl-max", "tokenUsage": 12},
        },
    )()


def test_shared_image_candidate_carries_warning_and_blocks_auto_blacklist():
    from src.query_governance.relevance import RelevanceCheckRunner
    from src.query_governance.relevance_remediation import decide_blacklist_handling

    runtime = _Runtime(
        _invocation(
            {
                "results": [
                    {
                        "query": "马甲",
                        "products": [
                            {"product_id": "1328942002", "is_relevant": False, "reason": "图片显示的是牛仔短裤，不是马甲"},
                            {"product_id": "1323994001", "is_relevant": False, "reason": "图片显示的是牛仔短裤，不是马甲"},
                        ],
                    }
                ]
            }
        )
    )
    runner = RelevanceCheckRunner(
        runtime=runtime,
        record_store=_Records(),
        image_cache=_service(None, _Fetcher([])),
    )

    outcome = runner.run(
        QuerySpec(query_id="q-1", brand="harbor", query="马甲"),
        {
            "current_products": [
                {"productId": "1328942002", "spuId": "1328942002", "productName": "西装马甲", "mainImage": SHARED_URL},
                {"productId": "1323994001", "spuId": "1323994001", "productName": "汗布马甲", "mainImage": SHARED_URL},
            ]
        },
    )

    assert outcome.status.name == "EXECUTED"
    candidates = outcome.result["blacklist_candidates"]
    assert [item["image_warning"] for item in candidates] == [IMAGE_URL_SHARED, IMAGE_URL_SHARED]
    assert [item["image_checked"] for item in candidates] == [False, False]

    action = outcome.action
    assert action["image_warnings"] == {"1328942002": IMAGE_URL_SHARED, "1323994001": IMAGE_URL_SHARED}

    decision = decide_blacklist_handling(candidates, execution_mode="auto")
    assert decision["executionMode"] == "human_approval"
    assert decision["shouldExecute"] is False
    assert decision["imageAutoDowngraded"] is True


def test_action_omits_warning_maps_for_products_without_any_image():
    from src.query_governance.relevance import RelevanceCheckRunner

    runtime = _Runtime(
        _invocation({"results": [{"query": "鞋", "products": [{"product_id": "p1", "is_relevant": False, "reason": "这是上衣"}]}]})
    )
    runner = RelevanceCheckRunner(runtime=runtime, record_store=_Records(), image_cache=_service(None, _Fetcher([])))

    outcome = runner.run(
        QuerySpec(query_id="q-1", brand="harbor", query="鞋"),
        {"current_products": [{"productId": "p1", "productName": "上衣"}]},
    )

    assert outcome.status.name == "EXECUTED"
    assert outcome.action["product_images"] == {"p1": ""}
    assert "image_warnings" not in outcome.action
    assert "image_warning_messages" not in outcome.action
