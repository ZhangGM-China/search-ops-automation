"""Client for the isolated Atlas browser image bridge."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import requests


class BrowserBridgeError(RuntimeError):
    """A typed browser bridge failure that is safe to persist in job results."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BrowserBridgeImageFetcher:
    """Fetch a Atlas image through a browser worker without embedding Chromium."""

    def __init__(
        self,
        bridge_url: str,
        *,
        token: str | None = None,
        timeout: float = 60.0,
        max_bytes: int = 8 * 1024 * 1024,
        request_post: Callable[..., Any] = requests.post,
    ) -> None:
        normalized = str(bridge_url or "").strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("browser bridge URL must be an http(s) URL")
        if timeout <= 0 or max_bytes <= 0:
            raise ValueError("browser bridge limits must be positive")
        self.bridge_url = normalized
        self.token = str(token or "").strip() or None
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes)
        self.request_post = request_post

    @staticmethod
    def _validate_url(url: str) -> str:
        value = str(url or "").strip()
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "static.atlas.example.test":
            raise BrowserBridgeError("IMAGE_URL_INVALID", "图片 URL 必须是 Atlas static HTTPS 地址")
        return value

    def fetch(self, url: str) -> tuple[bytes, str]:
        normalized = self._validate_url(url)
        headers = {"Accept": "image/*"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.request_post(
                f"{self.bridge_url}/fetch",
                json={"url": normalized},
                headers=headers,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise BrowserBridgeError("IMAGE_DOWNLOAD_TIMEOUT", "浏览器图片获取超时") from exc
        except requests.RequestException as exc:
            raise BrowserBridgeError("IMAGE_DOWNLOAD_FAILED", "浏览器图片桥接服务不可用") from exc

        try:
            status = int(getattr(response, "status_code", 0) or 0)
            if status == 401:
                raise BrowserBridgeError("IMAGE_BRIDGE_UNAUTHORIZED", "浏览器图片桥接服务认证失败")
            if status == 403:
                raise BrowserBridgeError("IMAGE_BROWSER_FORBIDDEN", "浏览器图片桥接服务返回来源 403")
            if status >= 500:
                raise BrowserBridgeError("IMAGE_HTTP_RETRYABLE", f"浏览器图片桥接服务返回 HTTP {status}")
            if status < 200 or status >= 300:
                raise BrowserBridgeError("IMAGE_BROWSER_FAILED", f"浏览器图片桥接服务返回 HTTP {status}")
            content_type = str(getattr(response, "headers", {}).get("content-type") or "").split(";", 1)[0].strip().lower()
            if content_type and not content_type.startswith("image/") and content_type != "application/octet-stream":
                raise BrowserBridgeError("IMAGE_UNSUPPORTED_MEDIA", "浏览器桥接响应不是图片媒体类型")
            payload = bytes(getattr(response, "content", b"") or b"")
            if len(payload) > self.max_bytes:
                raise BrowserBridgeError("IMAGE_TOO_LARGE", "浏览器返回图片超过下载大小限制")
            if not payload:
                raise BrowserBridgeError("IMAGE_EMPTY", "浏览器返回空图片")
            return payload, content_type or "application/octet-stream"
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()


__all__ = ["BrowserBridgeError", "BrowserBridgeImageFetcher"]
