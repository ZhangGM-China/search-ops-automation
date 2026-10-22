from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.persistence.models import Base
from src.query_governance.models import QueryGovernanceImageAsset
from src.query_governance.product_adapter import ProductAdapterRegistry
from src.query_governance.relevance import (
    CachedImageRecord,
    ImageCacheService,
    ImageCacheAssetStore,
    InMemoryObjectStorage,
    ProcessedImage,
    SqlAlchemyImageAssetStore,
    UnavailableObjectStorage,
)


@dataclass
class _Fetcher:
    calls: list[str]

    def fetch(self, url: str) -> tuple[bytes, str]:
        self.calls.append(url)
        return b"source", "image/png"


class _Transformer:
    def transform(self, payload: bytes, content_type: str) -> ProcessedImage:
        assert payload == b"source"
        assert content_type == "image/png"
        return ProcessedImage(b"jpeg", "image/jpeg", 20, 20)


class _Storage:
    def put(self, key: str, payload: bytes, content_type: str) -> str:
        assert payload == b"jpeg"
        assert content_type == "image/jpeg"
        return f"https://cos.example/{key}"


class _Store(ImageCacheAssetStore):
    def __init__(self, record: CachedImageRecord | None = None):
        self.record = record
        self.saved: list[tuple[object, ...]] = []

    def get(self, brand, product_id, spu_id):
        del brand, product_id, spu_id
        return self.record

    def save_success(self, **kwargs):
        self.saved.append(tuple(kwargs.values()))
        self.record = CachedImageRecord(
            brand=kwargs["brand"],
            product_id=kwargs["product_id"],
            spu_id=kwargs["spu_id"],
            source_url=kwargs["source_url"],
            cached_url=kwargs["cached_url"],
            storage_key=kwargs["storage_key"],
            content_type=kwargs["content_type"],
            width=kwargs["width"],
            height=kwargs["height"],
        )


def _product():
    return ProductAdapterRegistry().adapt(
        "atlas",
        {
            "productId": "SKU-1",
            "spuId": "SPU-1",
            "mainImage": "https://static.atlas.example.test/product.jpg?ts=1&w=375",
            "productName": "连衣裙",
        },
        1,
    )


def test_image_cache_reuses_product_mapping_without_fetching_source():
    store = _Store(
        CachedImageRecord(
            brand="atlas",
            product_id="SKU-1",
            spu_id="SPU-1",
            source_url="https://static.atlas.example.test/product.jpg?ts=1&w=375",
            cached_url="https://cos.example/atlas/SPU-1.jpg",
            storage_key="query-governance/images/prod/atlas/SPU-1.jpg",
            content_type="image/jpeg",
            width=20,
            height=20,
        )
    )
    fetcher = _Fetcher([])
    cache = ImageCacheService(
        fetcher=fetcher,
        transformer=_Transformer(),
        storage=_Storage(),
        asset_store=store,
    )

    assets = cache.prepare_products([_product()])

    asset = assets["https://static.atlas.example.test/product.jpg?ts=1&w=375"]
    assert asset.available is True
    assert asset.url == "https://cos.example/atlas/SPU-1.jpg"
    assert fetcher.calls == []
    assert store.saved == []


def test_image_cache_reuses_ready_mapping_when_cos_sdk_is_unconfigured(monkeypatch):
    source_url = "https://static.atlas.example.test/product.jpg?ts=1&w=375"
    store = _Store(
        CachedImageRecord(
            brand="atlas",
            product_id="SKU-1",
            spu_id="SPU-1",
            source_url=source_url,
            cached_url="https://cos.example/atlas/SPU-1.jpg",
            storage_key="query-governance/images/test/v1/SPU-1.jpg",
            content_type="image/jpeg",
            width=20,
            height=20,
        )
    )
    fetcher = _Fetcher([])
    checked_urls = []

    class _Response:
        status_code = 200

        def close(self):
            pass

    def check_cached_url(url, **kwargs):
        checked_urls.append(url)
        assert kwargs["timeout"] > 0
        return _Response()

    monkeypatch.setattr("src.query_governance.relevance.requests.head", check_cached_url)
    cache = ImageCacheService(
        fetcher=fetcher,
        transformer=_Transformer(),
        storage=UnavailableObjectStorage(),
        asset_store=store,
    )

    asset = cache.prepare_products([_product()])[source_url]

    assert asset.available is True
    assert asset.url == "https://cos.example/atlas/SPU-1.jpg"
    assert checked_urls == ["https://cos.example/atlas/SPU-1.jpg"]
    assert fetcher.calls == []
    assert store.saved == []


def test_image_cache_fetches_and_persists_mapping_when_product_is_missing():
    store = _Store()
    fetcher = _Fetcher([])
    cache = ImageCacheService(
        fetcher=fetcher,
        transformer=_Transformer(),
        storage=_Storage(),
        asset_store=store,
    )

    assets = cache.prepare_products([_product()])

    assert assets["https://static.atlas.example.test/product.jpg?ts=1&w=375"].available is True
    assert fetcher.calls == ["https://static.atlas.example.test/product.jpg?ts=1&w=375"]
    assert len(store.saved) == 1
    assert store.record is not None
    assert store.record.product_id == "SKU-1"
    assert store.record.spu_id == "SPU-1"


def test_image_cache_reuses_existing_cos_object_and_creates_missing_mapping():
    source_url = "https://static.atlas.example.test/product.jpg?ts=1&w=375"
    storage = InMemoryObjectStorage("https://cos.example")
    key = f"query-governance/images/v1/{sha256(source_url.encode()).hexdigest()}.jpg"
    storage.objects[key] = (b"already-cached", "image/jpeg")
    store = _Store()
    fetcher = _Fetcher([])
    cache = ImageCacheService(
        fetcher=fetcher,
        transformer=_Transformer(),
        storage=storage,
        asset_store=store,
    )

    assets = cache.prepare_products([_product()])

    asset = assets[source_url]
    assert asset.available is True
    assert asset.url == f"https://cos.example/{key}"
    assert fetcher.calls == []
    assert store.record is not None
    assert store.record.storage_key == key


def test_sqlalchemy_image_asset_store_persists_one_current_mapping_per_product(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'image-assets.db'}")
    Base.metadata.create_all(engine)
    store = SqlAlchemyImageAssetStore(lambda: Session(engine))

    store.save_success(
        brand="atlas",
        product_id="SKU-1",
        spu_id="SPU-1",
        source_url="https://static.atlas.example.test/product.jpg?ts=1",
        cached_url="https://cos.example/SPU-1-v1.jpg",
        storage_key="query-governance/images/prod/atlas/SPU-1-v1.jpg",
        content_type="image/jpeg",
        width=20,
        height=20,
    )
    store.save_success(
        brand="atlas",
        product_id="SKU-1",
        spu_id="SPU-1",
        source_url="https://static.atlas.example.test/product.jpg?ts=2",
        cached_url="https://cos.example/SPU-1-v2.jpg",
        storage_key="query-governance/images/prod/atlas/SPU-1-v2.jpg",
        content_type="image/jpeg",
        width=30,
        height=30,
    )

    current = store.get("atlas", "SKU-1", "SPU-1")

    assert current is not None
    assert current.cached_url == "https://cos.example/SPU-1-v2.jpg"
    with Session(engine) as session:
        assert session.query(QueryGovernanceImageAsset).count() == 1
