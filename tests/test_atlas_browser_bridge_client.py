from __future__ import annotations

from dataclasses import dataclass

import pytest

from scripts.image_sync.browser_bridge_client import BrowserBridgeImageFetcher, BrowserBridgeError


@dataclass
class _Response:
    status_code: int
    content: bytes = b""
    headers: dict[str, str] | None = None


def test_fetcher_posts_atlas_url_with_bearer_token() -> None:
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(200, content=b"image", headers={"content-type": "image/jpeg"})

    fetcher = BrowserBridgeImageFetcher("http://worker:8790", token="secret", request_post=post)

    payload, content_type = fetcher.fetch("https://static.atlas.example.test/assets/a.jpg?ts=1")

    assert payload == b"image"
    assert content_type == "image/jpeg"
    assert calls[0][0] == "http://worker:8790/fetch"
    assert calls[0][1]["json"] == {"url": "https://static.atlas.example.test/assets/a.jpg?ts=1"}
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret"


def test_fetcher_rejects_non_atlas_url_before_network_call() -> None:
    calls = []
    fetcher = BrowserBridgeImageFetcher(
        "http://worker:8790",
        request_post=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(BrowserBridgeError, match="Atlas") as error:
        fetcher.fetch("https://example.com/a.jpg")

    assert error.value.code == "IMAGE_URL_INVALID"
    assert calls == []


def test_fetcher_maps_bridge_http_errors_and_payload_validation() -> None:
    fetcher = BrowserBridgeImageFetcher(
        "http://worker:8790",
        request_post=lambda *args, **kwargs: _Response(403, content=b"forbidden"),
    )
    with pytest.raises(BrowserBridgeError) as error:
        fetcher.fetch("https://static.atlas.example.test/assets/a.jpg")
    assert error.value.code == "IMAGE_BROWSER_FORBIDDEN"

    fetcher = BrowserBridgeImageFetcher(
        "http://worker:8790",
        request_post=lambda *args, **kwargs: _Response(200, content=b"html", headers={"content-type": "text/html"}),
    )
    with pytest.raises(BrowserBridgeError) as error:
        fetcher.fetch("https://static.atlas.example.test/assets/a.jpg")
    assert error.value.code == "IMAGE_UNSUPPORTED_MEDIA"


def test_fetcher_rejects_invalid_configuration_and_oversized_payload() -> None:
    with pytest.raises(ValueError, match="bridge URL"):
        BrowserBridgeImageFetcher("worker:8790")

    fetcher = BrowserBridgeImageFetcher(
        "http://worker:8790",
        max_bytes=3,
        request_post=lambda *args, **kwargs: _Response(200, content=b"1234", headers={"content-type": "image/jpeg"}),
    )
    with pytest.raises(BrowserBridgeError) as error:
        fetcher.fetch("https://static.atlas.example.test/assets/a.jpg")
    assert error.value.code == "IMAGE_TOO_LARGE"
