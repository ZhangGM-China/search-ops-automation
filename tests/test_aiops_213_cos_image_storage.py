from __future__ import annotations

from dataclasses import dataclass

from src.query_governance.composition import (
    _cos_key_prefix,
    _image_proxy_url,
    _image_storage,
)
from src.query_governance.relevance import ImageCacheService, ProcessedImage


@dataclass
class _Fetcher:
    def fetch(self, url: str) -> tuple[bytes, str]:
        assert url == "https://cdn.example/product.jpg"
        return b"source", "image/jpeg"


class _Transformer:
    def transform(self, payload: bytes, content_type: str) -> ProcessedImage:
        assert payload == b"source"
        assert content_type == "image/jpeg"
        return ProcessedImage(b"compressed", "image/jpeg", 10, 10)


def test_missing_cos_configuration_never_emits_cos_local(monkeypatch):
    for name in (
        "QUERY_GOVERNANCE_COS_REGION",
        "QUERY_GOVERNANCE_COS_BUCKET",
        "QUERY_GOVERNANCE_COS_PUBLIC_BASE_URL",
        "TENCENT_COS_SECRET_ID",
        "TENCENT_COS_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    asset = ImageCacheService(
        fetcher=_Fetcher(),
        transformer=_Transformer(),
        storage=_image_storage(),
    ).prepare("https://cdn.example/product.jpg")

    assert asset.available is False
    assert asset.url is None
    assert asset.warning_code == "IMAGE_STORAGE_UNAVAILABLE"
    assert "cos.local" not in (asset.warning_message or "")


def test_cos_key_prefix_defaults_to_environment_and_allows_explicit_override(monkeypatch):
    monkeypatch.setenv("ENV", "test")
    monkeypatch.delenv("QUERY_GOVERNANCE_COS_KEY_PREFIX", raising=False)
    assert _cos_key_prefix() == "query-governance/images/test/v1"

    monkeypatch.setenv("QUERY_GOVERNANCE_COS_KEY_PREFIX", "query-governance/images/test/custom")
    assert _cos_key_prefix() == "query-governance/images/test/custom"


def test_image_proxy_url_is_read_from_runtime_environment(monkeypatch):
    monkeypatch.setenv(
        "QUERY_GOVERNANCE_IMAGE_PROXY_URL",
        "http://proxy.example:8080",
    )

    assert _image_proxy_url() == "http://proxy.example:8080"


def test_image_proxy_url_is_optional(monkeypatch):
    monkeypatch.delenv("QUERY_GOVERNANCE_IMAGE_PROXY_URL", raising=False)

    assert _image_proxy_url() is None
