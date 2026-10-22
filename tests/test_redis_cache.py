from __future__ import annotations

import json

from src.api.redis_cache import RedisCache


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl
        return True

    def scan_iter(self, match):
        prefix = match.removesuffix("*")
        return (key for key in list(self.values) if key.startswith(prefix))

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
        return True


class _BrokenRedis:
    def get(self, _key):
        raise RuntimeError("redis unavailable")

    def setex(self, _key, _ttl, _value):
        raise RuntimeError("redis unavailable")


def test_redis_cache_round_trips_json_values_with_namespaced_keys():
    client = _FakeRedis()
    cache = RedisCache(
        "redis://unused",
        prefix="aiops:test",
        client_factory=lambda _url, **_kwargs: client,
    )

    assert cache.get("query-list", ("atlas", "2026-09-14", 1)) is None
    assert cache.set("query-list", ("atlas", "2026-09-14", 1), {"total": 3}, ttl_seconds=30)
    assert cache.get("query-list", ("atlas", "2026-09-14", 1)) == {"total": 3}
    key = next(iter(client.values))
    assert key.startswith("aiops:test:query-list:")
    assert json.loads(client.values[key]) == {"total": 3}
    assert client.ttls[key] == 30


def test_redis_cache_fails_open_when_redis_is_unavailable():
    cache = RedisCache(
        "redis://unused",
        client_factory=lambda _url, **_kwargs: _BrokenRedis(),
    )

    assert cache.get("query-list", ("atlas",)) is None
    assert cache.set("query-list", ("atlas",), {"total": 0}, ttl_seconds=1) is False


def test_namespace_invalidation_is_visible_to_another_client():
    client = _FakeRedis()
    client.set = lambda key, value: client.values.__setitem__(key, value)
    first = RedisCache('redis://unused', client_factory=lambda *_args, **_kw: client)
    second = RedisCache('redis://unused', client_factory=lambda *_args, **_kw: client)
    first.set('rail', ('atlas',), {'total': 1}, ttl_seconds=30)
    assert second.get('rail', ('atlas',)) == {'total': 1}
    second.invalidate_namespace('rail')
    assert first.get('rail', ('atlas',)) is None
