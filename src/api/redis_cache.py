"""Small fail-open Redis JSON cache used by read-heavy API endpoints."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)


class RedisCache:
    def __init__(
        self,
        url: str | None = None,
        *,
        prefix: str | None = None,
        connect_timeout: float | None = None,
        socket_timeout: float | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url or os.getenv("AIOPS_REDIS_URL")
        self.prefix = prefix or os.getenv("AIOPS_REDIS_KEY_PREFIX", "aiops")
        self.connect_timeout = (
            connect_timeout
            if connect_timeout is not None
            else float(os.getenv("AIOPS_REDIS_CONNECT_TIMEOUT_SECONDS", "1.0"))
        )
        self.socket_timeout = (
            socket_timeout
            if socket_timeout is not None
            else float(os.getenv("AIOPS_REDIS_SOCKET_TIMEOUT_SECONDS", "1.0"))
        )
        self._client_factory = client_factory
        self._client: Any | None = None
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _client_or_none(self) -> Any | None:
        if not self.enabled:
            return None
        if self._client is not None:
            return self._client
        try:
            if self._client_factory is None:
                import redis
                self._client_factory = redis.Redis.from_url
            self._client = self._client_factory(
                self.url,
                decode_responses=True,
                socket_connect_timeout=self.connect_timeout,
                socket_timeout=self.socket_timeout,
                health_check_interval=30,
            )
            return self._client
        except Exception as exc:
            self._warn_once(exc)
            return None

    def _key(self, namespace: str, key: tuple[Any, ...]) -> str:
        payload = json.dumps(
            key,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"{self.prefix}:{namespace}:v1:{digest}"

    def get(self, namespace: str, key: tuple[Any, ...]) -> Any | None:
        client = self._client_or_none()
        if client is None:
            return None
        try:
            value = client.get(self._key(namespace, key))
            return json.loads(value) if value else None
        except Exception as exc:
            self._warn_once(exc)
            return None

    def set(
        self,
        namespace: str,
        key: tuple[Any, ...],
        value: Any,
        *,
        ttl_seconds: int,
    ) -> bool:
        client = self._client_or_none()
        if client is None:
            return False
        try:
            client.setex(
                self._key(namespace, key),
                max(1, int(ttl_seconds)),
                json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str),
            )
            return True
        except Exception as exc:
            self._warn_once(exc)
            return False

    def delete(self, namespace: str, key: tuple[Any, ...]) -> bool:
        client = self._client_or_none()
        if client is None:
            return False
        try:
            client.delete(self._key(namespace, key))
            return True
        except Exception as exc:
            self._warn_once(exc)
            return False

    def invalidate_namespace(self, namespace: str) -> bool:
        client = self._client_or_none()
        if client is None:
            return False
        try:
            pattern = f"{self.prefix}:{namespace}:v1:*"
            keys = list(client.scan_iter(match=pattern))
            if keys:
                client.delete(*keys)
            return True
        except Exception as exc:
            self._warn_once(exc)
            return False

    def _warn_once(self, exc: Exception) -> None:
        if not self._warned:
            logger.warning(
                "Redis cache unavailable; falling back to local cache: %s",
                exc.__class__.__name__,
            )
            self._warned = True
