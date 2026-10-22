"""Runtime configuration and concurrency control for relevance checks."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Mapping

from src.config import get_brand_business_config
from .relevance_buckets import DEFAULT_RELEVANCE_BUCKETS, normalize_relevance_buckets


RELEVANCE_CONFIG_KEYS = (
    "query_governance_relevance_multimodal_concurrency",
    "query_governance_relevance_batch_size",
    "query_governance_relevance_request_max_bytes",
    "query_governance_relevance_model_timeout_seconds",
    "query_governance_relevance_image_download_timeout_seconds",
    "query_governance_relevance_blacklist_ratio_threshold",
    "query_governance_relevance_max_rounds",
    "top_query_check_product_limit",
    "query_governance_relevance_buckets",
)

DEFAULT_RELEVANCE_RUNTIME_CONFIG = {
    "query_governance_relevance_multimodal_concurrency": 5,
    "query_governance_relevance_batch_size": 240,
    "query_governance_relevance_request_max_bytes": 20 * 1024 * 1024,
    "query_governance_relevance_model_timeout_seconds": 1200,
    "query_governance_relevance_image_download_timeout_seconds": 30,
    "query_governance_relevance_blacklist_ratio_threshold": 0.5,
    "query_governance_relevance_max_rounds": 3,
    "top_query_check_product_limit": 16,
    "query_governance_relevance_buckets": DEFAULT_RELEVANCE_BUCKETS,
}


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _ratio(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number between 0 and 1")
    normalized = float(value)
    if normalized < 0 or normalized > 1:
        raise ValueError(f"{field} must be a number between 0 and 1")
    return normalized


@dataclass(frozen=True)
class RelevanceRuntimeConfig:
    """Effective settings used when a relevance batch starts."""

    multimodal_concurrency: int = DEFAULT_RELEVANCE_RUNTIME_CONFIG[
        "query_governance_relevance_multimodal_concurrency"
    ]
    batch_size: int = DEFAULT_RELEVANCE_RUNTIME_CONFIG[
        "query_governance_relevance_batch_size"
    ]
    request_max_bytes: int = DEFAULT_RELEVANCE_RUNTIME_CONFIG[
        "query_governance_relevance_request_max_bytes"
    ]
    model_timeout_seconds: int = DEFAULT_RELEVANCE_RUNTIME_CONFIG[
        "query_governance_relevance_model_timeout_seconds"
    ]
    image_download_timeout_seconds: int = DEFAULT_RELEVANCE_RUNTIME_CONFIG[
        "query_governance_relevance_image_download_timeout_seconds"
    ]
    max_completion_tokens: int | None = None
    blacklist_ratio_threshold: float = DEFAULT_RELEVANCE_RUNTIME_CONFIG[
        "query_governance_relevance_blacklist_ratio_threshold"
    ]
    max_rounds: int = DEFAULT_RELEVANCE_RUNTIME_CONFIG[
        "query_governance_relevance_max_rounds"
    ]
    product_limit: int = DEFAULT_RELEVANCE_RUNTIME_CONFIG["top_query_check_product_limit"]
    buckets: tuple[str, ...] = DEFAULT_RELEVANCE_BUCKETS

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "RelevanceRuntimeConfig":
        values = dict(DEFAULT_RELEVANCE_RUNTIME_CONFIG)
        if mapping is not None:
            if not isinstance(mapping, Mapping):
                raise ValueError("relevance runtime config must be a mapping")
            values.update(
                {
                    key: mapping[key]
                    for key in RELEVANCE_CONFIG_KEYS
                    if key in mapping
                }
            )
        normalized = {
            key: (
                _ratio(values[key], key)
                if key == "query_governance_relevance_blacklist_ratio_threshold"
                else normalize_relevance_buckets(values[key])
                if key == "query_governance_relevance_buckets"
                else _positive_integer(values[key], key)
            )
            for key in RELEVANCE_CONFIG_KEYS
        }
        return cls(
            multimodal_concurrency=normalized[
                "query_governance_relevance_multimodal_concurrency"
            ],
            batch_size=normalized["query_governance_relevance_batch_size"],
            request_max_bytes=normalized[
                "query_governance_relevance_request_max_bytes"
            ],
            model_timeout_seconds=normalized[
                "query_governance_relevance_model_timeout_seconds"
            ],
            image_download_timeout_seconds=normalized[
                "query_governance_relevance_image_download_timeout_seconds"
            ],
            max_completion_tokens=None,
            blacklist_ratio_threshold=normalized[
                "query_governance_relevance_blacklist_ratio_threshold"
            ],
            max_rounds=normalized["query_governance_relevance_max_rounds"],
            product_limit=normalized["top_query_check_product_limit"],
            buckets=normalized["query_governance_relevance_buckets"],
        )

    @classmethod
    def for_brand(cls, brand: str) -> "RelevanceRuntimeConfig":
        return cls.from_mapping(get_brand_business_config(brand))


def get_relevance_runtime_config(brand: str) -> RelevanceRuntimeConfig:
    """Read the current brand settings at the next relevance batch boundary."""

    return RelevanceRuntimeConfig.for_brand(brand)


class RelevanceConcurrencyController:
    """Thread-safe limit for in-flight relevance model calls."""

    def __init__(self, concurrency: int) -> None:
        self.concurrency = _positive_integer(concurrency, "multimodal_concurrency")
        self._semaphore = threading.BoundedSemaphore(self.concurrency)

    def __enter__(self) -> "RelevanceConcurrencyController":
        self._semaphore.acquire()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self._semaphore.release()


__all__ = [
    "DEFAULT_RELEVANCE_RUNTIME_CONFIG",
    "RELEVANCE_CONFIG_KEYS",
    "RelevanceConcurrencyController",
    "RelevanceRuntimeConfig",
    "get_relevance_runtime_config",
]
