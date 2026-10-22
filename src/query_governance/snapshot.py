"""Immutable S0 snapshot normalization."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any


class S0SnapshotError(ValueError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _first_value(product: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = product.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _price(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not decimal_value.is_finite() or decimal_value < 0:
        return None
    float_value = float(decimal_value)
    if not math.isfinite(float_value):
        return None
    if decimal_value == decimal_value.to_integral():
        return int(decimal_value)
    return float_value


def _rank(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return fallback
    return normalized if normalized > 0 else fallback


def normalize_s0_product(
    product: Mapping[str, Any], position: int
) -> dict[str, Any]:
    product_id = _text(
        _first_value(
            product,
            "product_id",
            "productId",
            "taggingUid",
            "id",
            "spu_id",
            "spuId",
            "spu",
            "sku_id",
            "skuId",
        )
    )
    if product_id is None:
        raise S0SnapshotError(
            "S0_PRODUCT_ID_MISSING",
            f"Mixed result at rank {position} has no product identifier",
        )

    sku_id = _text(_first_value(product, "sku_id", "skuId", "sku"))
    spu_id = _text(_first_value(product, "spu_id", "spuId", "spu"))
    original_price = _price(
        _first_value(
            product,
            "original_price",
            "originalPrice",
            "origin_price",
            "originPrice",
            "market_price",
            "marketPrice",
            "price",
        )
    )
    discount_price = _price(
        _first_value(
            product,
            "discount_price",
            "discountPrice",
            "sale_price",
            "salePrice",
        )
    )
    if original_price is None:
        original_price = discount_price
    if discount_price is None:
        discount_price = original_price

    normalized: dict[str, Any] = {
        "product_id": product_id,
        "rank": _rank(product.get("rank"), position),
        "product_name": _text(
            _first_value(
                product,
                "product_name",
                "productName",
                "name",
                "title",
                "webLabel",
            )
        ),
        "image_url": _text(
            _first_value(
                product,
                "image_url",
                "imageUrl",
                "main_image",
                "mainImage",
            )
        ),
        "original_price": original_price,
        "discount_price": discount_price,
    }
    if sku_id is not None and sku_id != product_id:
        normalized["sku_id"] = sku_id
    if spu_id is not None and spu_id != product_id:
        normalized["spu_id"] = spu_id
    ext = product.get("ext")
    bucket = ext.get("bucket") if isinstance(ext, Mapping) else product.get("bucket")
    if isinstance(bucket, str) and bucket.strip():
        normalized["bucket"] = bucket.strip()
    return normalized


def normalize_s0_products(
    products: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        normalize_s0_product(product, position)
        for position, product in enumerate(products, start=1)
    ]


__all__ = [
    "S0SnapshotError",
    "normalize_s0_product",
    "normalize_s0_products",
]
