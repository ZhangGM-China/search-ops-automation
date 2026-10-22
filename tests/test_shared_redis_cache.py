from __future__ import annotations

from src.api import shared


def test_shared_cache_reads_and_writes_through_redis(monkeypatch):
    calls = []

    class _Remote:
        enabled = True

        def invalidate_namespace(self, namespace):
            return True

        def get(self, namespace, key):
            calls.append(("get", namespace, key))
            return {"source": "redis"}

        def set(self, namespace, key, value, *, ttl_seconds):
            calls.append(("set", namespace, key, value, ttl_seconds))
            return True

    monkeypatch.setattr(shared, "_redis_cache", _Remote())
    shared._clear_top_query_endpoint_caches()
    assert shared._top_query_cache_get("queries", ("atlas",)) == {"source": "redis"}
    assert shared._top_query_cache_set("queries", ("atlas",), {"source": "api"}, ttl_seconds=30) is None
    assert [item[0] for item in calls] == ["get", "set"]
