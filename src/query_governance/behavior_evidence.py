"""Query behavior evidence and product attribute enrichment.

The behavior table is a read-only analytical source.  This module keeps the
SQL contract, ranking merge, product-detail lookup and brand adaptation at a
single boundary so preference generation can consume one stable payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import math
import re
from typing import Any, Protocol

from sqlalchemy import text

from src.governance.query_identity import normalize_query_key, normalize_query_text

from .product_adapter import ProductAdapterError, ProductAdapterRegistry


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_BEHAVIOR_TABLE = "industry_knowledge_base.dws_brand_query_product_funnel_daily"
DEFAULT_BEHAVIOR_EVIDENCE_SQL_VERSION = "behavior-evidence-sql-v1"
DEFAULT_SOURCE_BRAND_VALUES = {
    "atlas": "ATLAS",
    "harbor": "Harbor",
    "summit": "Summit",
    "riviera": "Riviera",
    "meadow": "MEADOW",
}


class BehaviorEvidenceError(RuntimeError):
    """A behavior evidence source or product-detail request failed."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class BehaviorEvidenceConfig:
    """Per-brand limits captured when a Query governance batch starts."""

    top_purchase_product_limit: int = 8
    top_click_product_limit: int = 8
    fallback_days: int | None = None

    def __post_init__(self) -> None:
        for name in ("top_purchase_product_limit", "top_click_product_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.fallback_days is not None and (isinstance(self.fallback_days, bool) or not isinstance(self.fallback_days, int) or self.fallback_days < 0):
            raise ValueError("fallback_days must be a non-negative integer")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "BehaviorEvidenceConfig":
        values = values or {}
        purchase_limit = values.get(
            "query_governance_preference_top_purchase_product_limit",
            cls.top_purchase_product_limit,
        )
        click_limit = values.get(
            "query_governance_preference_top_click_product_limit",
            cls.top_click_product_limit,
        )
        fallback_days = values.get("query_governance_preference_behavior_evidence_fallback_days", cls.fallback_days)
        if isinstance(purchase_limit, bool) or not isinstance(purchase_limit, int):
            raise ValueError("top_purchase_product_limit must be a positive integer")
        if isinstance(click_limit, bool) or not isinstance(click_limit, int):
            raise ValueError("top_click_product_limit must be a positive integer")
        if fallback_days is not None and (isinstance(fallback_days, bool) or not isinstance(fallback_days, int)):
            raise ValueError("fallback_days must be a non-negative integer")
        return cls(
            top_purchase_product_limit=purchase_limit,
            top_click_product_limit=click_limit,
            fallback_days=fallback_days,
        )

    def as_mapping(self) -> dict[str, int]:
        result = {
            "query_governance_preference_top_purchase_product_limit": self.top_purchase_product_limit,
            "query_governance_preference_top_click_product_limit": self.top_click_product_limit,
        }
        if self.fallback_days is not None:
            result["query_governance_preference_behavior_evidence_fallback_days"] = self.fallback_days
        return result


@dataclass(frozen=True)
class BehaviorEvidenceSourceSpec:
    """Physical table and column contract for one behavior source."""

    table_name: str = DEFAULT_BEHAVIOR_TABLE
    source_brand: str | None = None
    date_column: str = "biz_date"
    brand_column: str = "brand"
    raw_query_column: str = "raw_query"
    standard_query_column: str = "standard_query"
    product_id_column: str = "product_id"
    click_pv_column: str = "product_click_pv"
    purchase_pv_column: str = "product_pay_pv"
    click_rank_column: str = "click_rank"
    purchase_rank_column: str = "pay_rank"
    current_rank_column: str | None = None
    search_result_position_avg_column: str = "search_result_position_avg"
    search_result_position_min_column: str = "search_result_position_min"
    search_result_position_max_column: str = "search_result_position_max"
    search_result_position_mode_column: str = "search_result_position_mode"
    product_name_column: str = "product_name"
    product_description_column: str = "product_description"
    category_column: str = "category"
    style_column: str = "style"
    color_column: str = "color"
    image_url_column: str = "main_image_url"
    price_column: str = "price"
    discount_price_column: str = "discount_price"
    active_column: str = "is_active"
    sql_version: str = DEFAULT_BEHAVIOR_EVIDENCE_SQL_VERSION

    def __post_init__(self) -> None:
        if not str(self.sql_version).strip():
            raise ValueError("sql_version must not be empty")
        identifiers = (
            self.date_column,
            self.brand_column,
            self.raw_query_column,
            self.standard_query_column,
            self.product_id_column,
            self.click_pv_column,
            self.purchase_pv_column,
            self.click_rank_column,
            self.purchase_rank_column,
            self.product_name_column,
            self.product_description_column,
            self.category_column,
            self.style_column,
            self.color_column,
            self.image_url_column,
            self.price_column,
            self.discount_price_column,
            self.active_column,
        )
        if self.current_rank_column:
            identifiers = (*identifiers, self.current_rank_column)
        identifiers = (
            *identifiers,
            self.search_result_position_avg_column,
            self.search_result_position_min_column,
            self.search_result_position_max_column,
            self.search_result_position_mode_column,
        )
        for identifier in identifiers:
            if not _IDENTIFIER_RE.fullmatch(identifier):
                raise ValueError(f"invalid behavior evidence column: {identifier}")


@dataclass(frozen=True)
class BehaviorEvidenceProduct:
    """One product's behavior metrics and daily observed rank statistics.

    The four ``search_result_position_*`` values are independent statistics
    over the day and must not be collapsed into a single current rank.
    """

    product_id: str
    click_rank: int | None
    purchase_rank: int | None
    current_rank: int | None
    click_pv: int | float
    purchase_pv: int | float
    product_name: str | None = None
    product_description: str | None = None
    category: str | None = None
    style: str | None = None
    color: str | None = None
    image_url: str | None = None
    price: int | float | None = None
    discount_price: int | float | None = None
    search_result_position_avg: int | float | None = None
    search_result_position_min: int | None = None
    search_result_position_max: int | None = None
    search_result_position_mode: int | None = None


@dataclass(frozen=True)
class BehaviorEvidenceResult:
    status: str
    brand: str
    query: str
    data_date: date
    products: tuple[dict[str, Any], ...]
    sql_version: str = DEFAULT_BEHAVIOR_EVIDENCE_SQL_VERSION
    requested_data_date: date | None = None
    date_fallback_reason: str | None = None


@dataclass(frozen=True)
class BehaviorEvidenceFetchResult:
    """Metrics plus the date actually used by the evidence source."""

    products: tuple[BehaviorEvidenceProduct, ...]
    data_date: date
    requested_data_date: date
    date_fallback_reason: str | None = None


class BehaviorEvidenceSource(Protocol):
    def fetch(
        self,
        brand: str,
        query: str,
        data_date: date,
        config: BehaviorEvidenceConfig,
    ) -> Sequence[BehaviorEvidenceProduct]:
        ...


class ProductDetailsGateway(Protocol):
    def fetch(
        self, brand: str, product_ids: Sequence[str]
    ) -> Mapping[str, Mapping[str, Any]]:
        ...


def _quote_identifier(identifier: str) -> str:
    """Quote a schema-qualified identifier after validating every component."""

    normalized = str(identifier or "").strip()
    if "@" in normalized:
        table_name, database_name = normalized.split("@", 1)
        normalized = f"{database_name}.{table_name}"
    parts = normalized.split(".")
    if not parts or any(not _IDENTIFIER_RE.fullmatch(part) for part in parts):
        raise ValueError(f"invalid behavior evidence table: {identifier}")
    return ".".join(f"`{part}`" for part in parts)


def _number(value: Any, *, default: int | float = 0) -> int | float:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            return default
    if not math.isfinite(number):
        return default
    return int(number) if number.is_integer() else number


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    number = _number(value, default=-1)
    return int(number) if number >= 0 else None


def _optional_number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    number = _number(value, default=float("nan"))
    if isinstance(number, (int, float)) and math.isfinite(number):
        return number
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _clean_ids(product_ids: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in product_ids:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _expand_composite_ids(product_ids: Sequence[str]) -> list[str]:
    """Expand evidence IDs that contain several SPUs into API query IDs."""
    expanded: list[str] = []
    seen: set[str] = set()
    for value in product_ids:
        for part in re.split(r"[,;，；]", str(value or "")):
            item = part.strip()
            if item and item not in seen:
                seen.add(item)
                expanded.append(item)
    return expanded


def _identifiers(raw: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "product_id",
        "productId",
        "spu_id",
        "spuId",
        "spu",
        "sku_id",
        "skuId",
        "modelCode",
        "code",
        "id",
    ):
        value = raw.get(key)
        if value is None:
            continue
        item = str(value).strip()
        if item.startswith("summit_"):
            item = item[len("summit_") :]
        if item and item not in values:
            values.append(item)
    return values


class SqlBehaviorEvidenceSource:
    """Read the configured day's top click/purchase products from SQL."""

    def __init__(
        self,
        engine: Any,
        specs: Mapping[str, BehaviorEvidenceSourceSpec] | None = None,
    ) -> None:
        self.engine = engine
        self.specs = {
            str(brand).strip().lower(): spec
            for brand, spec in (specs or {}).items()
        }

    def sql_version_for(self, brand: str) -> str:
        """Return the SQL contract version used for a brand."""

        brand_key = normalize_query_key(brand)
        spec = self.specs.get(brand_key) or BehaviorEvidenceSourceSpec()
        return spec.sql_version

    def fetch(
        self,
        brand: str,
        query: str,
        data_date: date,
        config: BehaviorEvidenceConfig,
    ) -> tuple[BehaviorEvidenceProduct, ...]:
        return self.fetch_with_metadata(brand, query, data_date, config).products

    def fetch_with_metadata(
        self,
        brand: str,
        query: str,
        data_date: date,
        config: BehaviorEvidenceConfig,
    ) -> BehaviorEvidenceFetchResult:
        brand_key = normalize_query_key(brand)
        spec = self.specs.get(brand_key)
        if spec is None:
            spec = BehaviorEvidenceSourceSpec()
        table = _quote_identifier(spec.table_name)
        current_rank = (
            f"`{spec.current_rank_column}` AS current_rank"
            if spec.current_rank_column
            else "NULL AS current_rank"
        )
        sql = text(
            f"""
            SELECT
                `{spec.product_id_column}` AS product_id,
                `{spec.click_pv_column}` AS click_pv,
                `{spec.purchase_pv_column}` AS purchase_pv,
                `{spec.click_rank_column}` AS click_rank,
                `{spec.purchase_rank_column}` AS purchase_rank,
                {current_rank},
                `{spec.search_result_position_avg_column}` AS search_result_position_avg,
                `{spec.search_result_position_min_column}` AS search_result_position_min,
                `{spec.search_result_position_max_column}` AS search_result_position_max,
                `{spec.search_result_position_mode_column}` AS search_result_position_mode,
                `{spec.product_name_column}` AS product_name,
                `{spec.product_description_column}` AS product_description,
                `{spec.category_column}` AS category,
                `{spec.style_column}` AS style,
                `{spec.color_column}` AS color,
                `{spec.image_url_column}` AS image_url,
                `{spec.price_column}` AS price,
                `{spec.discount_price_column}` AS discount_price
            FROM {table}
            WHERE `{spec.date_column}` = :data_date
              AND `{spec.brand_column}` = :brand
              AND (
                    `{spec.standard_query_column}` = :query_text
                    OR `{spec.standard_query_column}` = :query_key
                    OR `{spec.raw_query_column}` = :query_text
                  )
              AND `{spec.active_column}` = :active
              AND (
                    (`{spec.click_rank_column}` IS NOT NULL
                     AND `{spec.click_rank_column}` <= :click_limit)
                    OR
                    (`{spec.purchase_rank_column}` IS NOT NULL
                     AND `{spec.purchase_rank_column}` <= :purchase_limit)
                  )
            """.strip()
        )
        params = {
            "data_date": data_date.isoformat(),
            "brand": spec.source_brand or DEFAULT_SOURCE_BRAND_VALUES.get(
                brand_key, brand
            ),
            "query_text": normalize_query_text(query),
            "query_key": normalize_query_key(query),
            "active": 1,
            "click_limit": config.top_click_product_limit,
            "purchase_limit": config.top_purchase_product_limit,
        }
        def fetch_date(candidate_date: date) -> tuple[BehaviorEvidenceProduct, ...]:
            params["data_date"] = candidate_date.isoformat()
            try:
                with self.engine.connect() as connection:
                    rows = connection.execute(sql, params).mappings().all()
            except Exception as exc:
                message = str(exc).strip() or exc.__class__.__name__
                raise BehaviorEvidenceError(
                    "BEHAVIOR_EVIDENCE_QUERY_FAILED", message
                ) from exc
            return tuple(
            BehaviorEvidenceProduct(
                product_id=str(row.get("product_id") or "").strip(),
                click_rank=_optional_int(row.get("click_rank")),
                purchase_rank=_optional_int(row.get("purchase_rank")),
                current_rank=_optional_int(row.get("current_rank")),
                click_pv=_number(row.get("click_pv")),
                purchase_pv=_number(row.get("purchase_pv")),
                search_result_position_avg=_optional_number(
                    row.get("search_result_position_avg")
                ),
                search_result_position_min=_optional_int(
                    row.get("search_result_position_min")
                ),
                search_result_position_max=_optional_int(
                    row.get("search_result_position_max")
                ),
                search_result_position_mode=_optional_int(
                    row.get("search_result_position_mode")
                ),
                product_name=_optional_text(row.get("product_name")),
                product_description=_optional_text(row.get("product_description")),
                category=_optional_text(row.get("category")),
                style=_optional_text(row.get("style")),
                color=_optional_text(row.get("color")),
                image_url=_optional_text(row.get("image_url")),
                price=_optional_number(row.get("price")),
                discount_price=_optional_number(row.get("discount_price")),
            )
            for row in rows
            if str(row.get("product_id") or "").strip()
            )

        products = fetch_date(data_date)
        used_date = data_date
        fallback_reason = None
        if not products:
            available_dates = self._available_dates(spec=spec, brand=brand, query=query)
            candidate_dates = [
                date.fromisoformat(value)
                for value in available_dates
                if value <= data_date.isoformat()
                and (config.fallback_days is None or (data_date - date.fromisoformat(value)).days <= config.fallback_days)
            ]
            for candidate_date in candidate_dates:
                if candidate_date == data_date:
                    continue
                products = fetch_date(candidate_date)
                if products:
                    used_date = candidate_date
                    fallback_reason = "requested_date_unavailable"
                    break
        # The SQL result is already one row per product under the source PK,
        # but keeping the merge deterministic protects against accidental
        # duplicate analytical rows.
        unique: dict[str, BehaviorEvidenceProduct] = {}
        for product in products:
            previous = unique.get(product.product_id)
            if previous is None:
                unique[product.product_id] = product
                continue
            unique[product.product_id] = BehaviorEvidenceProduct(
                product_id=product.product_id,
                click_rank=min(
                    value
                    for value in (previous.click_rank, product.click_rank)
                    if value is not None
                )
                if previous.click_rank is not None or product.click_rank is not None
                else None,
                purchase_rank=min(
                    value
                    for value in (previous.purchase_rank, product.purchase_rank)
                    if value is not None
                )
                if previous.purchase_rank is not None or product.purchase_rank is not None
                else None,
                current_rank=product.current_rank
                if product.current_rank is not None
                else previous.current_rank,
                search_result_position_avg=(
                    product.search_result_position_avg
                    if product.search_result_position_avg is not None
                    else previous.search_result_position_avg
                ),
                search_result_position_min=(
                    product.search_result_position_min
                    if product.search_result_position_min is not None
                    else previous.search_result_position_min
                ),
                search_result_position_max=(
                    product.search_result_position_max
                    if product.search_result_position_max is not None
                    else previous.search_result_position_max
                ),
                search_result_position_mode=(
                    product.search_result_position_mode
                    if product.search_result_position_mode is not None
                    else previous.search_result_position_mode
                ),
                click_pv=max(previous.click_pv, product.click_pv),
                purchase_pv=max(previous.purchase_pv, product.purchase_pv),
                product_name=product.product_name or previous.product_name,
                product_description=(
                    product.product_description or previous.product_description
                ),
                category=product.category or previous.category,
                style=product.style or previous.style,
                color=product.color or previous.color,
                image_url=product.image_url or previous.image_url,
                price=product.price if product.price is not None else previous.price,
                discount_price=(
                    product.discount_price
                    if product.discount_price is not None
                    else previous.discount_price
                ),
            )
        merged_products = tuple(
            sorted(
                unique.values(),
                key=lambda item: (
                    min(
                        rank
                        for rank in (item.click_rank, item.purchase_rank)
                        if rank is not None
                    )
                    if item.click_rank is not None or item.purchase_rank is not None
                    else 2**31,
                    item.click_rank if item.click_rank is not None else 2**31,
                    item.purchase_rank if item.purchase_rank is not None else 2**31,
                    item.product_id,
                ),
            )
        )
        return BehaviorEvidenceFetchResult(
            merged_products, used_date, data_date, fallback_reason
        )

    def _available_dates(
        self,
        *,
        spec: BehaviorEvidenceSourceSpec,
        brand: str,
        query: str,
    ) -> tuple[str, ...]:
        table = _quote_identifier(spec.table_name)
        sql = text(
            f"""
            SELECT DISTINCT `{spec.date_column}` AS data_date
            FROM {table}
            WHERE `{spec.brand_column}` = :brand
              AND (
                    `{spec.standard_query_column}` = :query_text
                    OR `{spec.standard_query_column}` = :query_key
                    OR `{spec.raw_query_column}` = :query_text
                  )
              AND `{spec.active_column}` = :active
            ORDER BY `{spec.date_column}` DESC
            LIMIT 10
            """.strip()
        )
        params = {
            "brand": spec.source_brand or DEFAULT_SOURCE_BRAND_VALUES.get(
                normalize_query_key(brand), brand
            ),
            "query_text": normalize_query_text(query),
            "query_key": normalize_query_key(query),
            "active": 1,
        }
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(sql, params).mappings().all()
        except Exception as exc:
            raise BehaviorEvidenceError(
                "BEHAVIOR_EVIDENCE_DATE_CHECK_FAILED",
                str(exc).strip() or exc.__class__.__name__,
            ) from exc
        return tuple(
            sorted(
                {
                    str(row.get("data_date"))
                    for row in rows
                    if row.get("data_date") is not None
                },
                reverse=True,
            )
        )


class OperationalProductDetailGateway:
    """Expose raw product details without using the legacy field-cropping API."""

    def __init__(self, operational_service: Any, *, include_tag_sources: bool = False) -> None:
        self.operational_service = operational_service
        self.include_tag_sources = include_tag_sources

    def fetch(
        self, brand: str, product_ids: Sequence[str]
    ) -> Mapping[str, Mapping[str, Any]]:
        clean_ids = _clean_ids(product_ids)
        if not clean_ids:
            return {}
        if normalize_query_key(brand) == "summit":
            return self._fetch_summit(clean_ids)
        expanded_ids = _expand_composite_ids(clean_ids)
        try:
            status_map = self.operational_service.get_product_status_map(
                brand, expanded_ids
            )
        except Exception as exc:
            raise BehaviorEvidenceError(
                "BEHAVIOR_PRODUCT_DETAILS_FAILED", self._error_text(exc)
            ) from exc
        if status_map is None:
            raise BehaviorEvidenceError(
                "BEHAVIOR_PRODUCT_DETAILS_FAILED",
                "商品详情接口未返回有效结果",
            )
        result: dict[str, Mapping[str, Any]] = {}
        for requested_id in clean_ids:
            candidate_ids = [requested_id]
            if requested_id not in expanded_ids:
                candidate_ids = [
                    item.strip()
                    for item in re.split(r"[,;，；]", requested_id)
                    if item.strip()
                ]
            raw = None
            for candidate_id in candidate_ids:
                entry = status_map.get(candidate_id)
                raw = entry.get("raw") if isinstance(entry, Mapping) else None
                if isinstance(raw, Mapping):
                    break
            if isinstance(raw, Mapping):
                result[requested_id] = dict(raw)
                if self.include_tag_sources:
                    if raw.get("id"):
                        try:
                            result[requested_id]["tag_sources"] = self.operational_service.get_product_tag_sources(brand, str(raw["id"]))
                        except Exception:
                            result[requested_id]["tag_sources_error"] = "标签及推理信息查询失败"
                    else:
                        result[requested_id]["tag_sources_error"] = "缺少商品记录id"
        return result

    def _fetch_summit(
        self, product_ids: Sequence[str]
    ) -> Mapping[str, Mapping[str, Any]]:
        service = getattr(self.operational_service, "summit_service", None)
        method = getattr(service, "get_product_details_with_status", None)
        if not callable(method):
            raise BehaviorEvidenceError(
                "BEHAVIOR_PRODUCT_DETAILS_FAILED",
                "Summit商品详情接口未配置",
            )
        result: dict[str, Mapping[str, Any]] = {}
        for requested_id in product_ids:
            try:
                response = method(requested_id)
            except Exception as exc:
                raise BehaviorEvidenceError(
                    "BEHAVIOR_PRODUCT_DETAILS_FAILED", self._error_text(exc)
                ) from exc
            if not isinstance(response, Mapping) or response.get("success") is not True:
                reason = (
                    response.get("error_reason")
                    if isinstance(response, Mapping)
                    else None
                )
                raise BehaviorEvidenceError(
                    "BEHAVIOR_PRODUCT_DETAILS_FAILED",
                    str(reason or "Summit商品详情接口返回失败"),
                )
            rows = response.get("products")
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                raise BehaviorEvidenceError(
                    "BEHAVIOR_PRODUCT_DETAILS_FAILED",
                    "Summit商品详情接口返回结构无效",
                )
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                models = row.get("models")
                if isinstance(models, Sequence) and not isinstance(models, (str, bytes)):
                    for model in models:
                        if not isinstance(model, Mapping):
                            continue
                        model_id = str(model.get("modelCode") or "").strip()
                        if model_id != requested_id:
                            continue
                        merged = dict(row)
                        merged["models"] = [dict(model)]
                        merged.update(dict(model))
                        merged["productId"] = model_id
                        result[model_id] = merged
                    continue
                if requested_id not in _identifiers(row):
                    continue
                result[requested_id] = dict(row)
        return result

    @staticmethod
    def _error_text(exc: Exception) -> str:
        return str(exc).strip() or exc.__class__.__name__


def _tags(raw: Mapping[str, Any]) -> Any:
    for key in ("tags", "tagName", "englishTags", "englishTag", "stickers"):
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


class BehaviorEvidenceService:
    """Collect ranking evidence and enrich it with one shared product contract."""

    def __init__(
        self,
        *,
        source: BehaviorEvidenceSource,
        product_details: ProductDetailsGateway,
        adapters: ProductAdapterRegistry | None = None,
    ) -> None:
        self.source = source
        self.product_details = product_details
        self.adapters = adapters or ProductAdapterRegistry()

    def collect(
        self,
        *,
        brand: str,
        query: str,
        data_date: date,
        config: BehaviorEvidenceConfig | None = None,
    ) -> BehaviorEvidenceResult:
        config = config or BehaviorEvidenceConfig()
        sql_version = self._sql_version_for(brand)
        fetcher = getattr(self.source, "fetch_with_metadata", None)
        try:
            if callable(fetcher):
                fetched = fetcher(brand, query, data_date, config)
                metrics = tuple(fetched.products)
                evidence_date = fetched.data_date
                fallback_reason = fetched.date_fallback_reason
            else:
                metrics = tuple(self.source.fetch(brand, query, data_date, config))
                evidence_date = data_date
                fallback_reason = None
        except BehaviorEvidenceError:
            raise
        except Exception as exc:
            raise BehaviorEvidenceError(
                "BEHAVIOR_EVIDENCE_QUERY_FAILED", self._error_text(exc)
            ) from exc
        if not metrics:
            return BehaviorEvidenceResult(
                status="no_data",
                brand=brand,
                query=query,
                data_date=evidence_date,
                products=(),
                sql_version=sql_version,
                requested_data_date=data_date,
                date_fallback_reason=fallback_reason,
            )

        product_ids = _clean_ids([item.product_id for item in metrics])
        try:
            details = self.product_details.fetch(brand, product_ids)
        except BehaviorEvidenceError:
            raise
        except Exception as exc:
            raise BehaviorEvidenceError(
                "BEHAVIOR_PRODUCT_DETAILS_FAILED", self._error_text(exc)
            ) from exc
        if not isinstance(details, Mapping):
            raise BehaviorEvidenceError(
                "BEHAVIOR_PRODUCT_DETAILS_FAILED", "商品详情结果不是对象"
            )

        products = tuple(
            self._build_product(brand, metric, details.get(metric.product_id))
            for metric in metrics
        )
        return BehaviorEvidenceResult(
            status="success",
            brand=brand,
            query=query,
            data_date=evidence_date,
            products=products,
            sql_version=sql_version,
            requested_data_date=data_date,
            date_fallback_reason=fallback_reason,
        )

    def _sql_version_for(self, brand: str) -> str:
        resolver = getattr(self.source, "sql_version_for", None)
        if callable(resolver):
            value = resolver(brand)
            if str(value).strip():
                return str(value).strip()
        return DEFAULT_BEHAVIOR_EVIDENCE_SQL_VERSION

    def _build_product(
        self,
        brand: str,
        metric: BehaviorEvidenceProduct,
        raw: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        adapted = None
        if isinstance(raw, Mapping):
            detail = dict(raw)
            if normalize_query_key(brand) == "summit" and not any(
                detail.get(key) for key in ("productId", "product_id", "dsmCode")
            ):
                identifiers = _identifiers(detail)
                if identifiers:
                    detail["productId"] = identifiers[0]
            try:
                adapted = self.adapters.adapt(brand, detail, rank=metric.current_rank or 0)
            except ProductAdapterError:
                # A detail response can legitimately omit its ID after the
                # analytical row was written.  Preserve the ranking evidence
                # and expose missing text fields as null instead of changing
                # the Query's no-data semantics.
                adapted = None

        attributes = dict(adapted.attributes) if adapted is not None else {}
        return {
            "product_id": metric.product_id,
            "click_rank": metric.click_rank,
            "purchase_rank": metric.purchase_rank,
            "current_rank": metric.current_rank,
            "search_result_position_avg": metric.search_result_position_avg,
            "search_result_position_min": metric.search_result_position_min,
            "search_result_position_max": metric.search_result_position_max,
            "search_result_position_mode": metric.search_result_position_mode,
            "click_pv": metric.click_pv,
            "purchase_pv": metric.purchase_pv,
            "product_name": metric.product_name
            or (adapted.product_name if adapted is not None else None),
            "image_url": metric.image_url
            or (adapted.image_url if adapted is not None else None),
            "price": metric.price
            if metric.price is not None
            else (adapted.original_price if adapted is not None else None),
            "discount_price": metric.discount_price
            if metric.discount_price is not None
            else (adapted.discount_price if adapted is not None else None),
            "description": metric.product_description or attributes.get("description"),
            "gender": attributes.get("gender"),
            "series": attributes.get("series"),
            "category": metric.category or attributes.get("category"),
            "style": metric.style or attributes.get("style"),
            "color": metric.color or attributes.get("color"),
            "size": attributes.get("size"),
            "material": attributes.get("material"),
            "design": attributes.get("design"),
            "stock": attributes.get("stock"),
            "is_new": attributes.get("isNew"),
            "tags": _tags(raw) if isinstance(raw, Mapping) else None,
        }

    @staticmethod
    def _error_text(exc: Exception) -> str:
        return str(exc).strip() or exc.__class__.__name__


__all__ = [
    "BehaviorEvidenceConfig",
    "DEFAULT_BEHAVIOR_EVIDENCE_SQL_VERSION",
    "BehaviorEvidenceError",
    "BehaviorEvidenceFetchResult",
    "BehaviorEvidenceProduct",
    "BehaviorEvidenceResult",
    "BehaviorEvidenceService",
    "BehaviorEvidenceSource",
    "BehaviorEvidenceSourceSpec",
    "OperationalProductDetailGateway",
    "ProductDetailsGateway",
    "SqlBehaviorEvidenceSource",
]
