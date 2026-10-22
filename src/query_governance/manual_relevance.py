"""Caller-driven relevance checks with automatic product-detail enrichment.

The manual boundary is intentionally separate from the governance workflow:
it resolves caller-provided SKUs through read-only business APIs and reuses the
existing multimodal prompt/parser, but it never reads or writes persisted
relevance verdicts and never creates governance actions.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .models import QueryGovernanceManualRelevanceCheck, QuerySpec
from .langfuse import QueryGovernanceObservability
from .product_adapter import AdaptedProduct, ProductAdapterError, ProductAdapterRegistry
from .relevance import (
    RelevanceCheckRunner,
    build_relevance_messages,
    normalize_image_url,
)


class ManualCheckStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class ManualQueryStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class ManualProductStatus(str, Enum):
    PENDING = "pending"
    FETCHING_DETAIL = "fetching_detail"
    CHECKING = "checking"
    COMPLETED = "completed"
    NOT_FOUND = "not_found"
    DETAIL_ERROR = "detail_error"
    MODEL_ERROR = "model_error"


@dataclass(frozen=True)
class ManualCheckSubmission:
    check_id: str
    status: str
    created: bool


@dataclass(frozen=True)
class ManualCheckRecord:
    check_id: str
    brand: str
    idempotency_key: str
    status: ManualCheckStatus
    request: Mapping[str, Any]
    result: Mapping[str, Any] | None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ProductDetailResult:
    status: str
    row: Mapping[str, Any] | None = None
    reason: str | None = None


class RawProductDetailClient(Protocol):
    def get_raw_product_details(
        self, brand: str, product_id: str
    ) -> list[dict[str, Any]]:
        ...


def _json_dump(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("manual relevance JSON payload must be an object")
    return parsed


class ManualRelevanceCheckRepository:
    """Durable lifecycle storage for manual relevance jobs."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        model: type[Any] = QueryGovernanceManualRelevanceCheck,
    ) -> None:
        self._session_factory = session_factory
        self._model = model

    def create_or_get(
        self,
        *,
        brand: str,
        idempotency_key: str,
        request: Mapping[str, Any],
    ) -> tuple[ManualCheckRecord, bool]:
        serialized = _json_dump(request)
        with self._session_factory() as session:
            existing = self._find_by_key(session, brand, idempotency_key)
            if existing is not None:
                self._assert_same_request(existing, serialized)
                return self._to_record(existing), False
            row = self._model(
                check_id=f"relchk_{uuid4().hex}",
                brand=brand,
                idempotency_key=idempotency_key,
                status=ManualCheckStatus.QUEUED.value,
                request_json=serialized,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self._find_by_key(session, brand, idempotency_key)
                if existing is None:
                    raise
                self._assert_same_request(existing, serialized)
                return self._to_record(existing), False
            session.refresh(row)
            return self._to_record(row), True

    def get(self, check_id: str) -> ManualCheckRecord | None:
        with self._session_factory() as session:
            row = session.get(self._model, check_id)
            return self._to_record(row) if row is not None else None

    def get_latest_for_query(self, *, brand: str, query: str) -> ManualCheckRecord | None:
        normalized_brand = str(brand or "").strip().lower()
        normalized_query = str(query or "").strip()
        if not normalized_brand or not normalized_query:
            return None
        with self._session_factory() as session:
            rows = session.execute(
                select(self._model)
                .where(self._model.brand == normalized_brand)
                .order_by(self._model.created_at.desc())
            ).scalars()
            for row in rows:
                request = _json_load(row.request_json) or {}
                queries = request.get("queries")
                if isinstance(queries, Sequence) and any(
                    isinstance(item, Mapping)
                    and str(item.get("query") or "").strip() == normalized_query
                    for item in queries
                ):
                    return self._to_record(row)
        return None

    def claim(self, check_id: str) -> bool:
        with self._session_factory() as session:
            result = session.execute(
                update(self._model)
                .where(
                    self._model.check_id == check_id,
                    self._model.status == ManualCheckStatus.QUEUED.value,
                )
                .values(status=ManualCheckStatus.RUNNING.value)
            )
            session.commit()
            return bool(result.rowcount)

    def complete(
        self,
        check_id: str,
        *,
        status: ManualCheckStatus,
        result: Mapping[str, Any],
    ) -> ManualCheckRecord:
        if status not in {
            ManualCheckStatus.COMPLETED,
            ManualCheckStatus.COMPLETED_WITH_ERRORS,
            ManualCheckStatus.FAILED,
        }:
            raise ValueError("manual relevance completion requires a terminal status")
        return self._update(
            check_id,
            status=status.value,
            result_json=_json_dump(result),
            error_code=None,
            error_message=None,
        )

    def fail(self, check_id: str, code: str, message: str) -> ManualCheckRecord:
        return self._update(
            check_id,
            status=ManualCheckStatus.FAILED.value,
            error_code=code,
            error_message=message,
        )

    def _update(self, check_id: str, **values: Any) -> ManualCheckRecord:
        with self._session_factory() as session:
            row = session.get(self._model, check_id)
            if row is None:
                raise KeyError(f"manual relevance check not found: {check_id}")
            for key, value in values.items():
                setattr(row, key, value)
            session.commit()
            session.refresh(row)
            return self._to_record(row)

    def _find_by_key(self, session: Any, brand: str, key: str) -> Any:
        return session.execute(
            select(self._model).where(
                self._model.brand == brand,
                self._model.idempotency_key == key,
            )
        ).scalar_one_or_none()

    @staticmethod
    def _assert_same_request(row: Any, serialized: str) -> None:
        if row.request_json != serialized:
            raise ValueError("idempotency key collision")

    @staticmethod
    def _to_record(row: Any) -> ManualCheckRecord:
        return ManualCheckRecord(
            check_id=str(row.check_id),
            brand=str(row.brand),
            idempotency_key=str(row.idempotency_key),
            status=ManualCheckStatus(row.status),
            request=_json_load(row.request_json) or {},
            result=_json_load(row.result_json),
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at.isoformat() if row.created_at else None,
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )


class ProductDetailBatchGateway:
    """Resolve caller SKUs through the brand-aware read-only detail API."""

    def __init__(self, client: RawProductDetailClient, *, search_client: Any | None = None) -> None:
        self.client = client
        self.search_client = search_client

    def resolve(
        self, brand: str, skus: Sequence[str], *, query: str | None = None
    ) -> dict[str, ProductDetailResult]:
        unique_skus = list(dict.fromkeys(str(sku).strip() for sku in skus if str(sku).strip()))
        resolved: dict[str, ProductDetailResult] = {}
        for sku in unique_skus:
            try:
                rows = self.client.get_raw_product_details(brand, sku)
            except Exception as exc:
                resolved[sku] = ProductDetailResult(
                    status=ManualProductStatus.DETAIL_ERROR.value,
                    reason=str(exc).strip() or "商品详情接口调用失败",
                )
                continue
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                resolved[sku] = ProductDetailResult(
                    status=ManualProductStatus.DETAIL_ERROR.value,
                    reason="商品详情接口返回格式无效",
                )
                continue
            matched = next(
                (
                    row
                    for row in rows
                    if isinstance(row, Mapping) and self._matches_identifier(row, sku)
                ),
                None,
            )
            if matched is None:
                resolved[sku] = ProductDetailResult(
                    status=ManualProductStatus.NOT_FOUND.value,
                    reason="未查询到商品详情",
                )
                continue
            normalized = dict(matched)
            normalized.setdefault("productId", sku)
            normalized.setdefault("skuId", sku)
            resolved[sku] = ProductDetailResult(status="found", row=normalized)
        self._resolve_from_search_fallback(brand, skus=unique_skus, query=query, resolved=resolved)
        return resolved

    def _resolve_from_search_fallback(
        self,
        brand: str,
        *,
        skus: Sequence[str],
        query: str | None,
        resolved: dict[str, ProductDetailResult],
    ) -> None:
        """Use the query's read-only recall snapshot when detail lookup is unavailable.

        The test tenant exposes the mixed search endpoint but may deny the
        administrative product-detail endpoint. The recall payload already
        contains the fields required by the relevance adapter, so using it as
        a fallback keeps manual verification functional without broadening any
        write permission.
        """
        normalized_query = str(query or "").strip()
        search_client = self.search_client
        if not normalized_query or search_client is None:
            return
        unresolved = [
            sku for sku in skus
            if resolved.get(sku) is not None
            and resolved[sku].status in {
                ManualProductStatus.NOT_FOUND.value,
                ManualProductStatus.DETAIL_ERROR.value,
            }
        ]
        if not unresolved or not hasattr(search_client, "search_products_with_total"):
            return
        try:
            response = search_client.search_products_with_total(
                brand,
                normalized_query,
                page_size=max(16, len(unresolved)),
            )
            rows = response.get("products") if isinstance(response, Mapping) else None
        except Exception:
            return
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return
        for sku in unresolved:
            matched = next(
                (
                    row for row in rows
                    if isinstance(row, Mapping) and self._matches_identifier(row, sku)
                ),
                None,
            )
            if matched is None:
                continue
            normalized = dict(matched)
            normalized.setdefault("productId", sku)
            normalized.setdefault("skuId", sku)
            resolved[sku] = ProductDetailResult(status="found", row=normalized)

    @classmethod
    def _matches_identifier(cls, row: Mapping[str, Any], sku: str) -> bool:
        expected = str(sku).strip()
        for value in cls._identifier_values(row):
            text = str(value or "").strip()
            if not text:
                continue
            if text == expected or text.endswith(f"_{expected}"):
                return True
            candidates = [item.strip() for item in re.split(r"[;,]", text) if item.strip()]
            expected_parts = {item.strip() for item in re.split(r"[;,]", expected) if item.strip()}
            if any(item == expected or item.endswith(f"_{expected}") for item in candidates):
                return True
            if len(expected_parts) > 1 and expected_parts.issubset(set(candidates)):
                return True
        return False

    @staticmethod
    def _identifier_values(row: Mapping[str, Any]) -> list[Any]:
        keys = (
            "id", "productId", "product_id", "skuId", "sku_id", "spu",
            "spuId", "modelCode", "code", "taggingUid", "privateId",
        )
        values = [row.get(key) for key in keys]
        for nested_key in ("models", "summit_models_details"):
            nested = row.get(nested_key)
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                for item in nested:
                    if isinstance(item, Mapping):
                        values.extend(item.get(key) for key in keys)
        return values


class ManualRelevanceCheckService:
    """Orchestrate detail enrichment and pure relevance inference."""

    def __init__(
        self,
        *,
        repository: ManualRelevanceCheckRepository,
        gateway: ProductDetailBatchGateway,
        runtime_factory: Callable[..., Any],
        image_cache: Any,
        batch_size: int | Callable[[str], int],
        guidance_reader: Callable[[str, str], str],
        adapters: ProductAdapterRegistry | None = None,
        observability: QueryGovernanceObservability | None = None,
    ) -> None:
        if not callable(batch_size) and batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.repository = repository
        self.gateway = gateway
        self.runtime_factory = runtime_factory
        self.image_cache = image_cache
        self.batch_size = batch_size
        self.guidance_reader = guidance_reader
        self.adapters = adapters or ProductAdapterRegistry()
        self.observability = observability

    def submit(
        self,
        *,
        brand: str,
        queries: Sequence[Mapping[str, Any]],
        idempotency_key: str,
    ) -> ManualCheckSubmission:
        normalized_brand = str(brand or "").strip().lower()
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_brand or not normalized_key:
            raise ValueError("brand and idempotency key are required")
        normalized_queries = self._normalize_queries(queries)
        record, created = self.repository.create_or_get(
            brand=normalized_brand,
            idempotency_key=normalized_key,
            request={"brand": normalized_brand, "queries": normalized_queries},
        )
        return ManualCheckSubmission(record.check_id, record.status.value, created)

    def get(self, check_id: str) -> dict[str, Any] | None:
        record = self.repository.get(check_id)
        if record is None:
            return None
        return self._payload_for_record(record)

    def get_latest(self, *, brand: str, query: str) -> dict[str, Any] | None:
        record = self.repository.get_latest_for_query(brand=brand, query=query)
        if record is None:
            return None
        payload = self._payload_for_record(record)
        payload["queries"] = [
            item for item in payload.get("queries", [])
            if str(item.get("query") or "").strip() == str(query or "").strip()
        ]
        return payload

    @staticmethod
    def _with_timestamps(record: ManualCheckRecord, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        if record.created_at:
            result["createdAt"] = record.created_at
        if record.updated_at:
            result["updatedAt"] = record.updated_at
        return result

    def _payload_for_record(self, record: ManualCheckRecord) -> dict[str, Any]:
        if record.result is not None:
            return self._with_timestamps(record, record.result)
        failed = record.status is ManualCheckStatus.FAILED
        return self._with_timestamps(record, {
            "checkId": record.check_id,
            "brand": record.brand,
            "status": record.status.value,
            "queries": [
                {
                    "query": str(item["query"]),
                    "status": (
                        ManualQueryStatus.FAILED.value
                        if failed
                        else ManualQueryStatus.RUNNING.value
                        if record.status is ManualCheckStatus.RUNNING
                        else ManualQueryStatus.PENDING.value
                    ),
                    "products": [
                        {
                            "sku": str(sku),
                            "status": (
                                ManualProductStatus.MODEL_ERROR.value
                                if failed
                                else ManualProductStatus.PENDING.value
                            ),
                            "isRelevant": None,
                            "reason": record.error_message if failed else None,
                        }
                        for sku in item["skus"]
                    ],
                }
                for item in record.request["queries"]
            ],
        })

    def run(self, check_id: str) -> None:
        if not self.repository.claim(check_id):
            return
        record = self.repository.get(check_id)
        if record is None:
            return
        try:
            result, terminal_status = self._execute(record)
            self.repository.complete(check_id, status=terminal_status, result=result)
        except Exception as exc:
            self.repository.fail(
                check_id,
                "MANUAL_RELEVANCE_CHECK_FAILED",
                str(exc).strip() or exc.__class__.__name__,
            )

    def _execute(
        self, record: ManualCheckRecord
    ) -> tuple[dict[str, Any], ManualCheckStatus]:
        queries = list(record.request["queries"])
        details: dict[str, ProductDetailResult] = {}
        for item in queries:
            details.update(
                self.gateway.resolve(
                    record.brand,
                    [str(sku) for sku in item["skus"]],
                    query=str(item["query"]),
                )
            )
        runtime_holder: list[Any] = []

        def runtime_provider() -> Any:
            if not runtime_holder:
                runtime_holder.append(
                    self._brand_dependency(self.runtime_factory, record.brand)
                )
            return runtime_holder[0]

        query_results = [
            self._evaluate_query(
                brand=record.brand,
                query_index=index,
                query=str(item["query"]),
                skus=[str(sku) for sku in item["skus"]],
                details=details,
                runtime_provider=runtime_provider,
                trace_seed=record.check_id,
            )
            for index, item in enumerate(queries)
        ]
        terminal = self._job_status(query_results)
        return {
            "checkId": record.check_id,
            "brand": record.brand,
            "status": terminal.value,
            "queries": query_results,
        }, terminal

    def _evaluate_query(
        self,
        *,
        brand: str,
        query_index: int,
        query: str,
        skus: list[str],
        details: Mapping[str, ProductDetailResult],
        runtime_provider: Callable[[], Any],
        trace_seed: str | None = None,
    ) -> dict[str, Any]:
        products = [
            {
                "sku": sku,
                "status": ManualProductStatus.PENDING.value,
                "isRelevant": None,
                "reason": None,
            }
            for sku in skus
        ]
        pending: list[tuple[int, AdaptedProduct]] = []
        for index, sku in enumerate(skus):
            detail = details.get(sku)
            if detail is None or detail.status == ManualProductStatus.NOT_FOUND.value:
                products[index].update(
                    status=ManualProductStatus.NOT_FOUND.value,
                    reason="未查询到商品详情",
                )
                continue
            if detail.status == ManualProductStatus.DETAIL_ERROR.value:
                products[index].update(
                    status=ManualProductStatus.DETAIL_ERROR.value,
                    reason=detail.reason or "商品详情接口调用失败",
                )
                continue
            try:
                adapted = self.adapters.adapt(brand, dict(detail.row or {}), rank=index + 1)
            except ProductAdapterError as exc:
                products[index].update(
                    status=ManualProductStatus.DETAIL_ERROR.value,
                    reason=exc.message,
                )
                continue
            except Exception as exc:
                products[index].update(
                    status=ManualProductStatus.DETAIL_ERROR.value,
                    reason=str(exc).strip() or "商品详情适配失败",
                )
                continue
            pending.append((index, adapted))

        try:
            guidance = str(self.guidance_reader(brand, query) or "")
        except Exception as exc:
            message = str(exc).strip() or "相关性规则读取失败"
            for output_index, _ in pending:
                products[output_index].update(
                    status=ManualProductStatus.MODEL_ERROR.value,
                    reason=message,
                )
            return {
                "query": query,
                "status": self._query_status(products).value,
                "products": products,
            }
        batch_size = self._batch_size(brand)
        for offset in range(0, len(pending), batch_size):
            chunk = pending[offset: offset + batch_size]
            self._evaluate_chunk(
                brand=brand,
                query_index=query_index,
                query=query,
                guidance=guidance,
                chunk=chunk,
                products=products,
                runtime_provider=runtime_provider,
                trace_seed=trace_seed,
            )
        return {
            "query": query,
            "status": self._query_status(products).value,
            "products": products,
        }

    def _evaluate_chunk(
        self,
        *,
        brand: str,
        query_index: int,
        query: str,
        guidance: str,
        chunk: Sequence[tuple[int, AdaptedProduct]],
        products: list[dict[str, Any]],
        runtime_provider: Callable[[], Any],
        trace_seed: str | None = None,
    ) -> None:
        adapted = [item[1] for item in chunk]
        urls = [normalize_image_url(item.image_url) for item in adapted]
        try:
            image_cache = self._brand_dependency(self.image_cache, brand)
            prepare_products = getattr(image_cache, "prepare_products", None)
            assets = (
                prepare_products(adapted, brand=brand)
                if callable(prepare_products)
                else image_cache.prepare_many(urls)
            )
            messages = build_relevance_messages(
                QuerySpec(
                    query_id=f"manual-{query_index}",
                    brand=brand,
                    query=query,
                ),
                adapted,
                assets,
                guidance=guidance,
            )
            runtime = runtime_provider()
            invoke_kwargs: dict[str, Any] = {}
            if self.observability is not None:
                query_id = f"manual-{query_index}"
                batch = self.observability.start_batch(
                    trace_seed or f"manual-relevance:{brand}:{query_index}",
                    [QuerySpec(query_id=query_id, brand=brand, query=query)],
                    trace_name="manual-relevance",
                )
                invoke_kwargs = batch.query_config(
                    query_id,
                    "query_governance.relevance_multimodal",
                )
            invocation = runtime.invoke(messages, **invoke_kwargs)
            response = getattr(invocation, "response", invocation)
            payload = RelevanceCheckRunner._decode_payload(
                getattr(response, "content", response)
            )
            decisions = RelevanceCheckRunner.parse_output(
                payload, query=query, products=adapted
            )
        except Exception as exc:
            message = str(exc).strip() or "相关性模型调用失败"
            for output_index, _ in chunk:
                products[output_index].update(
                    status=ManualProductStatus.MODEL_ERROR.value,
                    reason=message,
                )
            return
        for (output_index, _), decision in zip(chunk, decisions, strict=True):
            products[output_index].update(
                status=ManualProductStatus.COMPLETED.value,
                isRelevant=decision.is_relevant,
                reason=decision.reason,
            )

    @staticmethod
    def _query_status(products: Sequence[Mapping[str, Any]]) -> ManualQueryStatus:
        completed = sum(
            1 for item in products if item.get("status") == ManualProductStatus.COMPLETED.value
        )
        if completed == len(products):
            return ManualQueryStatus.COMPLETED
        if completed:
            return ManualQueryStatus.COMPLETED_WITH_ERRORS
        return ManualQueryStatus.FAILED

    @staticmethod
    def _job_status(queries: Sequence[Mapping[str, Any]]) -> ManualCheckStatus:
        statuses = [str(item.get("status") or "") for item in queries]
        if statuses and all(value == ManualQueryStatus.COMPLETED.value for value in statuses):
            return ManualCheckStatus.COMPLETED
        if any(
            value in {
                ManualQueryStatus.COMPLETED.value,
                ManualQueryStatus.COMPLETED_WITH_ERRORS.value,
            }
            for value in statuses
        ):
            return ManualCheckStatus.COMPLETED_WITH_ERRORS
        return ManualCheckStatus.FAILED

    @staticmethod
    def _normalize_queries(
        queries: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(queries, Sequence) or isinstance(queries, (str, bytes)) or not queries:
            raise ValueError("queries must be a non-empty array")
        result: list[dict[str, Any]] = []
        for item in queries:
            if not isinstance(item, Mapping):
                raise ValueError("each Query must be an object")
            query = str(item.get("query") or "").strip()
            raw_skus = item.get("skus")
            if not query or not isinstance(raw_skus, Sequence) or isinstance(raw_skus, (str, bytes)):
                raise ValueError("query and skus are required")
            skus = [str(sku or "").strip() for sku in raw_skus]
            if not skus or any(not sku for sku in skus):
                raise ValueError("products must contain non-empty SKUs")
            result.append({"query": query, "skus": skus})
        return result

    def _batch_size(self, brand: str) -> int:
        value = self._brand_dependency(self.batch_size, brand)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("batch_size must be positive")
        return value

    @staticmethod
    def _brand_dependency(dependency: Any, brand: str) -> Any:
        if not callable(dependency):
            return dependency
        try:
            parameters = inspect.signature(dependency).parameters.values()
        except (TypeError, ValueError):
            return dependency(brand)
        accepts_brand = any(
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            }
            for parameter in parameters
        )
        return dependency(brand) if accepts_brand else dependency()


__all__ = [
    "ManualCheckRecord",
    "ManualCheckStatus",
    "ManualCheckSubmission",
    "ManualProductStatus",
    "ManualQueryStatus",
    "ManualRelevanceCheckRepository",
    "ManualRelevanceCheckService",
    "ProductDetailBatchGateway",
    "ProductDetailResult",
]
