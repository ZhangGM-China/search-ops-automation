from types import SimpleNamespace

import pytest

from src.query_governance.relevance import RemoteImageFetcher, RelevanceError


class Bridge:
    def __init__(self):
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return b'browser-image', 'image/jpeg'


def test_atlas_uses_browser_without_direct_download():
    bridge = Bridge()
    fetcher = RemoteImageFetcher(browser_fetcher=bridge, request_get=lambda *a, **k: pytest.fail('must not download Atlas directly'))
    assert fetcher.fetch('https://static.atlas.example.test/assets/a.jpg') == (b'browser-image', 'image/jpeg')
    assert len(bridge.calls) == 1


def test_other_brands_keep_direct_fetch():
    bridge = Bridge()
    response = SimpleNamespace(status_code=200, headers={'content-type':'image/jpeg'}, content=b'harbor-image')
    fetcher = RemoteImageFetcher(browser_fetcher=bridge, request_get=lambda *a, **k: response)
    assert fetcher.fetch('https://image.harbor.com/a.jpg') == (b'harbor-image', 'image/jpeg')
    assert bridge.calls == []


def test_browser_failure_is_typed_and_never_falls_back_to_atlas_direct():
    from scripts.image_sync.browser_bridge_client import BrowserBridgeError
    bridge = Bridge()
    bridge.fetch = lambda url: (_ for _ in ()).throw(BrowserBridgeError('IMAGE_BROWSER_FORBIDDEN', 'source denied'))
    fetcher = RemoteImageFetcher(browser_fetcher=bridge, request_get=lambda *a, **k: pytest.fail('no direct retry storm'))
    with pytest.raises(RelevanceError) as exc:
        fetcher.fetch('https://static.atlas.example.test/assets/a.jpg')
    assert exc.value.error_code == 'IMAGE_BROWSER_FORBIDDEN'


def test_composition_builds_configured_browser_fetcher(monkeypatch):
    from src.query_governance import composition
    monkeypatch.setenv('QG_BROWSER_BRIDGE_URL', 'http://qg-atlas-browser-worker:8790')
    monkeypatch.setenv('QG_BROWSER_BRIDGE_TOKEN', 'test-only-placeholder')
    fetcher = composition._image_fetcher(30)
    assert fetcher.browser_fetcher.bridge_url == 'http://qg-atlas-browser-worker:8790'
    assert fetcher.browser_fetcher.timeout >= 90


def test_configured_bridge_requires_token(monkeypatch):
    from src.query_governance import composition
    monkeypatch.setenv('QG_BROWSER_BRIDGE_URL', 'http://qg-atlas-browser-worker:8790')
    monkeypatch.delenv('QG_BROWSER_BRIDGE_TOKEN', raising=False)
    with pytest.raises(ValueError, match='Token'):
        composition._image_fetcher(30)


def test_no_configuration_keeps_existing_downloader(monkeypatch):
    from src.query_governance import composition
    monkeypatch.delenv('QG_BROWSER_BRIDGE_URL', raising=False)
    fetcher = composition._image_fetcher(30)
    assert fetcher.browser_fetcher is None
