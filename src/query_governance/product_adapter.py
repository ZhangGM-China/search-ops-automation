"""Brand-aware product contracts shared by Query governance nodes.

The search service returns brand-specific dictionaries.  This module converts
those dictionaries once at the boundary and exposes three stable projections:
the minimal S0 snapshot, behavior evidence, and the relevance-model input.
The raw payload is never mutated and remains available for diagnostics.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from numbers import Real
from typing import Any, Protocol


RELEVANCE_FIELD_NAMES = (
    "description",
    "gender",
    "series",
    "category",
    "style",
    "color",
    "size",
    "material",
    "design",
    "stock",
    "isNew",
)


class ProductAdapterError(ValueError):
    """Raised when a search row cannot satisfy the product contract."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _first_value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _string_value(payload: Mapping[str, Any], *keys: str) -> str | None:
    value = _first_value(payload, *keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number_value(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Real):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().replace(",", "")
        if not normalized:
            return None
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _price_value(payload: Mapping[str, Any], *keys: str) -> float | None:
    value = _first_value(payload, *keys)
    if isinstance(value, Mapping):
        value = _first_value(value, "activePrice", "salePrice", "price", "amount")
    number = _number_value(value)
    return float(number) if number is not None else None


def _image_value(payload: Mapping[str, Any]) -> str | None:
    direct = _string_value(payload, "mainImage", "imageUrl", "image_url")
    if direct:
        return direct
    media = payload.get("media")
    if isinstance(media, Mapping):
        images = media.get("images")
        if isinstance(images, Sequence) and not isinstance(images, (str, bytes)):
            for image in images:
                if isinstance(image, str) and image.strip():
                    return image.strip()
                if isinstance(image, Mapping):
                    url = _string_value(image, "url", "imageUrl", "src")
                    if url:
                        return url
    return None


def _same_identifier(primary: str | None, other: str | None) -> str | None:
    if other is None or other == primary:
        return None
    return other


def _generic_attributes(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "description": _first_value(payload, "description", "desc"),
        "gender": _first_value(payload, "gender", "sex"),
        "series": _first_value(payload, "series", "collection", "family"),
        "category": _first_value(payload, "category", "mainCategory", "main_category"),
        "style": _first_value(payload, "style", "styleName", "style_name"),
        "color": _first_value(payload, "color", "colorLabel", "colour"),
        "size": _first_value(payload, "size", "sizeLabel"),
        "material": _first_value(payload, "material", "fabric"),
        "design": _first_value(payload, "design", "pattern"),
        "stock": _first_value(payload, "stock", "inventory"),
        "isNew": _first_value(payload, "isNew", "is_new", "newProduct"),
    }


@dataclass(frozen=True)
class ProductVariant:
    """One concrete style/model belonging to a product result."""

    product_id: str
    product_name: str | None
    image_url: str | None
    price: float | None
    discount_price: float | None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_relevance_input(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "productId": self.product_id,
            "productName": self.product_name,
            "imageUrl": self.image_url,
            "price": self.price,
            "discountPrice": self.discount_price,
        }
        result.update({name: self.attributes.get(name) for name in RELEVANCE_FIELD_NAMES})
        return result

    def to_behavior_evidence(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "image_url": self.image_url,
            "price": self.price,
            "discount_price": self.discount_price,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True)
class AdaptedProduct:
    """Stable product representation used by all later governance nodes."""

    brand: str
    rank: int
    product_id: str
    sku_id: str | None
    spu_id: str | None
    product_name: str | None
    image_url: str | None
    original_price: float | None
    discount_price: float | None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    variants: tuple[ProductVariant, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_s0(self) -> dict[str, Any]:
        """Project only the fields allowed in the immutable S0 snapshot."""

        result: dict[str, Any] = {
            "product_id": self.product_id,
            "rank": self.rank,
        }
        optional = {
            "sku_id": self.sku_id,
            "spu_id": self.spu_id,
            "product_name": self.product_name,
            "image_url": self.image_url,
            "original_price": self.original_price,
            "discount_price": self.discount_price,
        }
        result.update({key: value for key, value in optional.items() if value is not None})
        ext = self.raw.get("ext") if isinstance(self.raw, Mapping) else None
        bucket = ext.get("bucket") if isinstance(ext, Mapping) else self.raw.get("bucket")
        if isinstance(bucket, str) and bucket.strip():
            result["bucket"] = bucket.strip()
        return result

    def to_behavior_evidence(
        self,
        *,
        click_rank: int | None = None,
        purchase_rank: int | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "image_url": self.image_url,
            "price": self.original_price,
            "discount_price": self.discount_price,
            "current_rank": self.rank,
            "click_rank": click_rank,
            "purchase_rank": purchase_rank,
            "attributes": dict(self.attributes),
        }
        if self.sku_id is not None:
            result["sku_id"] = self.sku_id
        if self.spu_id is not None:
            result["spu_id"] = self.spu_id
        if self.variants:
            result["variants"] = [variant.to_behavior_evidence() for variant in self.variants]
        return result

    def to_relevance_input(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "productId": self.product_id,
            "productName": self.product_name,
            "imageUrl": self.image_url,
            "price": self.original_price,
            "discountPrice": self.discount_price,
        }
        result.update({name: self.attributes.get(name) for name in RELEVANCE_FIELD_NAMES})
        if self.sku_id is not None:
            result["skuId"] = self.sku_id
        if self.spu_id is not None:
            result["spuId"] = self.spu_id
        if self.variants:
            result["variants"] = [variant.to_relevance_input() for variant in self.variants]
        return result


class ProductAdapter(Protocol):
    def adapt(self, raw: Mapping[str, Any], rank: int) -> AdaptedProduct:
        """Convert one Mixed row without mutating its raw payload."""


class GenericProductAdapter:
    """Adapter for brands whose Mixed product shape uses common aliases."""

    def __init__(self, brand: str) -> None:
        self.brand = brand.strip().lower()

    def adapt(self, raw: Mapping[str, Any], rank: int) -> AdaptedProduct:
        if not isinstance(raw, Mapping):
            raise ProductAdapterError("PRODUCT_ROW_INVALID", "商品结果不是对象")
        product_id = _string_value(
            raw, "product_id", "productId", "spuId", "spu", "skuId", "sku_id"
        )
        if product_id is None:
            raise ProductAdapterError("PRODUCT_ID_MISSING", "商品结果缺少商品 ID")
        sku_id = _same_identifier(product_id, _string_value(raw, "sku_id", "skuId"))
        spu_id = _same_identifier(product_id, _string_value(raw, "spu_id", "spuId", "spu"))
        original_price = _price_value(raw, "original_price", "originalPrice", "price")
        discount_price = _price_value(
            raw, "discount_price", "discountPrice", "sale_price", "salePrice", "price"
        )
        if discount_price is None:
            discount_price = original_price
        attributes = _generic_attributes(raw)
        attributes["stock"] = _number_value(attributes.get("stock"))
        return AdaptedProduct(
            brand=self.brand,
            rank=rank,
            product_id=product_id,
            sku_id=sku_id,
            spu_id=spu_id,
            product_name=_string_value(
                raw, "product_name", "productName", "name", "webLabel", "title"
            ),
            image_url=_image_value(raw),
            original_price=original_price,
            discount_price=discount_price,
            attributes=attributes,
            raw=dict(raw),
        )


class SummitProductAdapter(GenericProductAdapter):
    """Adapter preserving Summit's SPU-with-many-models semantics."""

    def adapt(self, raw: Mapping[str, Any], rank: int) -> AdaptedProduct:
        if not isinstance(raw, Mapping):
            raise ProductAdapterError("PRODUCT_ROW_INVALID", "商品结果不是对象")
        raw_models = raw.get("models") or raw.get("summit_models_details") or []
        models = [dict(model) for model in raw_models if isinstance(model, Mapping)]
        model_codes = [
            str(model["modelCode"]).strip()
            for model in models
            if model.get("modelCode") is not None and str(model["modelCode"]).strip()
        ]
        product_id = _string_value(raw, "productId", "product_id", "dsmCode")
        if product_id is None and model_codes:
            product_id = ";".join(model_codes)
        if product_id is None:
            raise ProductAdapterError("PRODUCT_ID_MISSING", "Summit商品缺少商品 ID 或 modelCode")

        if model_codes and not _string_value(raw, "productId", "product_id"):
            product_id = ";".join(model_codes)
        sku_id = _same_identifier(product_id, _string_value(raw, "skuId", "sku_id"))
        spu_id = _same_identifier(product_id, _string_value(raw, "spuId", "spu_id", "spu"))

        spu_attributes = {
            "description": _first_value(raw, "designedFor", "description", "desc"),
            "gender": _first_value(raw, "gender", "sex"),
            "series": _first_value(raw, "family", "series", "collection"),
            "category": _first_value(raw, "productNatureText", "category", "mainCategory"),
            "style": _first_value(raw, "style", "styleName"),
            "color": _first_value(raw, "color", "colorLabel", "colour"),
            "size": _first_value(raw, "size", "sizeLabel"),
            "material": _first_value(raw, "material", "fabric"),
            "design": _first_value(raw, "design", "pattern"),
            "stock": _number_value(_first_value(raw, "stock", "inventory")),
            "isNew": _first_value(raw, "isNew", "is_new", "newProduct"),
        }

        variants: list[ProductVariant] = []
        for model in models:
            model_id = _string_value(model, "modelCode", "productId", "skuId")
            if model_id is None:
                continue
            model_attributes = dict(spu_attributes)
            for key, value in _generic_attributes(model).items():
                if value is not None:
                    model_attributes[key] = value
            model_attributes["description"] = spu_attributes["description"]
            model_attributes["series"] = spu_attributes["series"]
            model_attributes["category"] = spu_attributes["category"]
            model_attributes["stock"] = _number_value(
                _first_value(model, "stock", "inventory")
            )
            model_price = _price_value(model, "discountPrice", "salePrice", "price")
            model_original_price = _price_value(model, "originalPrice", "price")
            if model_original_price is None:
                model_original_price = model_price
            model_discount_price = _price_value(
                model, "discountPrice", "salePrice", "price"
            )
            if model_discount_price is None:
                model_discount_price = model_original_price
            variants.append(
                ProductVariant(
                    product_id=model_id,
                    product_name=_string_value(
                        model, "webLabel", "shortLabel", "productName", "name", "title"
                    ),
                    image_url=_image_value(model),
                    price=model_original_price,
                    discount_price=model_discount_price,
                    attributes=model_attributes,
                    raw=model,
                )
            )

        first_variant = variants[0] if variants else None
        if first_variant:
            for key in ("color", "stock"):
                if first_variant.attributes.get(key) is not None:
                    spu_attributes[key] = first_variant.attributes[key]
        original_price = (
            first_variant.price if first_variant else _price_value(raw, "originalPrice", "price")
        )
        discount_price = (
            first_variant.discount_price
            if first_variant
            else _price_value(raw, "discountPrice", "salePrice", "price")
        )
        if discount_price is None:
            discount_price = original_price
        return AdaptedProduct(
            brand="summit",
            rank=rank,
            product_id=product_id,
            sku_id=sku_id,
            spu_id=spu_id,
            product_name=(
                first_variant.product_name
                if first_variant
                else _string_value(raw, "productName", "name", "webLabel", "title")
            ),
            image_url=(first_variant.image_url if first_variant else _image_value(raw)),
            original_price=original_price,
            discount_price=discount_price,
            attributes=spu_attributes,
            variants=tuple(variants),
            raw=dict(raw),
        )


class ProductAdapterRegistry:
    """Resolve a brand adapter while keeping one shared downstream contract."""

    def __init__(self, adapters: Mapping[str, ProductAdapter] | None = None) -> None:
        self._adapters = {"summit": SummitProductAdapter("summit")}
        if adapters:
            self._adapters.update({str(key).strip().lower(): value for key, value in adapters.items()})

    def for_brand(self, brand: str) -> ProductAdapter:
        normalized = str(brand or "").strip().lower()
        return self._adapters.get(normalized, GenericProductAdapter(normalized))

    def adapt(self, brand: str, raw: Mapping[str, Any], rank: int) -> AdaptedProduct:
        return self.for_brand(brand).adapt(raw, rank)


__all__ = [
    "AdaptedProduct",
    "SummitProductAdapter",
    "GenericProductAdapter",
    "ProductAdapter",
    "ProductAdapterError",
    "ProductAdapterRegistry",
    "ProductVariant",
    "RELEVANCE_FIELD_NAMES",
]
