"""Bucket selection used by the relevance detection boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_RELEVANCE_BUCKETS = (
    "productIdsByCode",
    "productIdsByProductNameTag",
    "productIdsByCodeSpellCorrections",
    "productIdsByTagsAll",
    "productIdsByWhiteList",
    "productIdsBySubTag",
)

# These are the leaf bucket identifiers currently defined by Mixed/top-query.
# The UI maps them to Chinese labels while the runtime stores these stable IDs.
KNOWN_RELEVANCE_BUCKETS = (
    "top",
    "productIdsByAgent",
    *DEFAULT_RELEVANCE_BUCKETS,
    "productIdsByHighLowGroup",
    "productIdsByMultiCategory",
    "productIdsByTagNameDowngrade",
    "productIdsByOpsLowWeight",
    "productIdsByLowWeight",
    "productIdsByFuzzy",
    "productIdsByProductNameFuzzy",
    "productIdsByTagFuzzy",
    "productIdsByFuzzySpellCorrections",
    "productIdsByNoStock",
    "Dynamic",
)


def normalize_relevance_buckets(value: Any) -> tuple[str, ...]:
    """Normalize configured bucket IDs, falling back safely to L1/L2 defaults."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return DEFAULT_RELEVANCE_BUCKETS
    normalized = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in value
            if isinstance(item, str) and str(item).strip() in KNOWN_RELEVANCE_BUCKETS
        )
    )
    return normalized or DEFAULT_RELEVANCE_BUCKETS


def product_bucket(product: Mapping[str, Any] | Any) -> str:
    raw = (
        product.raw
        if hasattr(product, "raw") and isinstance(getattr(product, "raw"), Mapping)
        else product
    )
    if not isinstance(raw, Mapping):
        return ""
    ext = raw.get("ext")
    if isinstance(ext, Mapping):
        bucket = ext.get("bucket")
        if isinstance(bucket, str) and bucket.strip():
            return bucket.strip()
    bucket = raw.get("bucket")
    return bucket.strip() if isinstance(bucket, str) else ""


def filter_relevance_products(
    products: Sequence[Mapping[str, Any]] | None,
    *,
    product_limit: int,
    configured_buckets: Any,
) -> list[Mapping[str, Any]]:
    """Take the existing top-N result set, then retain selected buckets only."""

    limit = max(0, int(product_limit))
    selected = set(normalize_relevance_buckets(configured_buckets))
    # Some brands, including Summit, do not populate Mixed bucket
    # metadata.  Selecting every known bucket is the explicit opt-out from
    # bucket filtering, so those products must still reach relevance checks.
    include_all_buckets = selected == set(KNOWN_RELEVANCE_BUCKETS)
    return [
        product
        for product in list(products or [])[:limit]
        if isinstance(product, Mapping)
        and (include_all_buckets or product_bucket(product) in selected)
    ]


__all__ = [
    "DEFAULT_RELEVANCE_BUCKETS",
    "KNOWN_RELEVANCE_BUCKETS",
    "filter_relevance_products",
    "normalize_relevance_buckets",
    "product_bucket",
]
