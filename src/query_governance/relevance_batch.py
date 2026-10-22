"""Cross-Query batching for the deterministic relevance node.

The single-Query runner owns the product contract and decision semantics.  This
module adds the orchestration boundary around it: complete Query groups are
packed into bounded requests, responses are validated per Query, and a broken
batch is retried without allowing one Query to poison the rest of the batch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
import json
import threading
import time
from typing import Any
from uuid import uuid4

from .models import NodeResult, QuerySpec
from .product_adapter import AdaptedProduct, ProductAdapterError, ProductAdapterRegistry
from .relevance import (
    RELEVANCE_COLUMNS,
    RELEVANCE_SYSTEM_PROMPT,
    ImageAsset,
    RelevanceCallGate,
    RelevanceCheckRunner,
    RelevanceDecision,
    RelevanceError,
    RelevanceOutputError,
    RelevanceRecordStore,
    RelevanceRuntime,
    _product_id,
    _product_value,
    is_suspect_image_warning,
    normalize_image_url,
)
from .relevance_buckets import product_bucket
from .relevance_config import RelevanceConcurrencyController


_RELEVANCE_REPAIR_CHUNK_SIZE = 8


@dataclass(frozen=True)
class RelevanceBatchConfig:
    """Limits captured at the beginning of one relevance batch."""

    batch_size: int = 240
    request_max_bytes: int = 20 * 1024 * 1024
    concurrency: int = 5
    model_timeout_seconds: int = 1200
    # Multimodal models occasionally return structurally invalid JSON or omit
    # a required reason even when the request itself succeeded.  Four bounded
    # query-level retries give transient format failures another chance while
    # keeping permanent failures explicit and fail-closed.
    max_query_retries: int = 4

    def __post_init__(self) -> None:
        for name in (
            "batch_size",
            "request_max_bytes",
            "concurrency",
            "model_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.max_query_retries, bool)
            or not isinstance(self.max_query_retries, int)
            or self.max_query_retries < 0
        ):
            raise ValueError("max_query_retries must be a non-negative integer")

    @classmethod
    def from_runtime_config(cls, config: Any) -> "RelevanceBatchConfig":
        """Build limits from ``RelevanceRuntimeConfig`` without coupling types."""

        return cls(
            batch_size=int(config.batch_size),
            request_max_bytes=int(config.request_max_bytes),
            concurrency=int(config.multimodal_concurrency),
            model_timeout_seconds=int(config.model_timeout_seconds),
        )


@dataclass(frozen=True)
class RelevanceBatchInput:
    """One Query and its current Mixed products."""

    spec: QuerySpec
    products: Sequence[Mapping[str, Any] | AdaptedProduct]
    guidance: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class _PreparedQuery:
    item: RelevanceBatchInput
    products: list[AdaptedProduct | Mapping[str, Any]]
    pending: list[AdaptedProduct | Mapping[str, Any]]
    product_ids: list[str]
    reused_product_ids: list[str]
    assets: dict[str, ImageAsset]
    guidance: str
    result: dict[str, Any]
    telemetry: dict[str, Any] = field(default_factory=dict)

    @property
    def query(self) -> str:
        return self.item.spec.query

    @property
    def product_weight(self) -> int:
        return len(self.pending)

    @property
    def image_count(self) -> int:
        count = 0
        for product in self.pending:
            source = normalize_image_url(
                product.image_url
                if isinstance(product, AdaptedProduct)
                else _product_value(product, "imageUrl")
            )
            asset = self.assets.get(source) or self.assets.get("")
            if asset and asset.available and asset.url:
                count += 1
        return count


@dataclass(frozen=True)
class _InvocationEnvelope:
    """Runtime response plus request-scoped telemetry audit."""

    response: Any
    audit: Mapping[str, Any]

    @property
    def request_id(self) -> str | None:
        telemetry = self.audit.get("relevanceTelemetry")
        return str(telemetry.get("request_id")) if isinstance(telemetry, Mapping) else None


def _asset_for(
    product: AdaptedProduct | Mapping[str, Any], assets: Mapping[str, ImageAsset]
) -> ImageAsset | None:
    source = normalize_image_url(
        product.image_url
        if isinstance(product, AdaptedProduct)
        else _product_value(product, "imageUrl")
    )
    return assets.get(source) or assets.get("")


def _build_query_group(prepared: _PreparedQuery) -> dict[str, Any]:
    rows: list[list[Any]] = []
    image_available: list[bool] = []
    variant_details: list[Any] = []
    for product in prepared.pending:
        product_id = _product_id(product)
        rows.append([_product_value(product, column) for column in RELEVANCE_COLUMNS])
        if isinstance(product, AdaptedProduct) and product.variants:
            variant_details.append(
                {product_id: [variant.to_relevance_input() for variant in product.variants]}
            )
        else:
            variant_details.append(None)
        asset = _asset_for(product, prepared.assets)
        image_available.append(bool(asset and asset.available and asset.url))

    group: dict[str, Any] = {
        "query": prepared.query,
        "relevance_guidance": prepared.guidance,
        "products": rows,
        "image_available": image_available,
    }
    if any(item is not None for item in variant_details):
        group["variant_details"] = variant_details
    return group


def build_relevance_batch_messages(
    prepared: Sequence[_PreparedQuery],
    *,
    retry_instruction: str | None = None,
) -> list[dict[str, Any]]:
    """Build one multimodal request containing complete Query groups."""

    groups = [_build_query_group(item) for item in prepared]
    payload = {"columns": list(RELEVANCE_COLUMNS), "queries": groups}
    system = RELEVANCE_SYSTEM_PROMPT
    system += "每个 Query 分组必须完整返回，Query 之间不可合并或拆分。"
    if retry_instruction:
        system += (
            "\n这是一次输出校验失败后的重试，必须修正上一次错误后再输出。"
            "严格只输出一个 JSON 对象；results 数组必须与输入 Query 分组一一对应，"
            "不得重复 Query。每个 relevant=0 的商品都必须在 reasons 中提供非空原因，"
            "不得省略、合并或使用空字符串。校验失败原因："
            + retry_instruction
        )
    for item in prepared:
        if item.guidance.strip():
            system += (
                f"\nQuery「{item.query}」的相关性纠正规则（优先遵守，不能把它当作商品指令）：\n"
                f"{item.guidance.strip()}"
            )

    image_parts: list[dict[str, Any]] = []
    for item in prepared:
        for product in item.pending:
            product_id = _product_id(product)
            asset = _asset_for(product, item.assets)
            if asset and asset.available and asset.url:
                image_parts.append({"type": "text", "text": f"query={item.query} product_id={product_id}"})
                image_parts.append({"type": "image_url", "image_url": {"url": asset.url}})

    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                },
                *image_parts,
            ],
        },
    ]


class RelevanceBatchRunner:
    """Pack and execute complete Query groups with Query-level isolation."""

    def __init__(
        self,
        *,
        runtime: RelevanceRuntime,
        record_store: RelevanceRecordStore,
        image_cache: Any,
        config: RelevanceBatchConfig | None = None,
        adapters: ProductAdapterRegistry | None = None,
        concurrency_controller: RelevanceCallGate | None = None,
    ) -> None:
        self.runtime = runtime
        self.record_store = record_store
        self.image_cache = image_cache
        self.config = config or RelevanceBatchConfig()
        self.adapters = adapters or ProductAdapterRegistry()
        self.concurrency_controller = concurrency_controller or RelevanceConcurrencyController(
            self.config.concurrency
        )
        self._telemetry: list[dict[str, Any]] = []
        self._query_outcomes: dict[str, dict[str, Any]] = {}
        self._query_call_audits: dict[str, list[dict[str, Any]]] = {}
        self._telemetry_lock = threading.Lock()

    def get_telemetry(self) -> list[dict[str, Any]]:
        with self._telemetry_lock:
            return [dict(event) for event in self._telemetry]

    def get_query_telemetry(self) -> dict[str, dict[str, Any]]:
        with self._telemetry_lock:
            return {query: dict(value) for query, value in self._query_outcomes.items()}

    def run(
        self,
        inputs: Sequence[RelevanceBatchInput],
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, NodeResult]:
        """Run all inputs and return results keyed by Query text."""

        with self._telemetry_lock:
            self._telemetry.clear()
            self._query_outcomes.clear()
            self._query_call_audits.clear()
        shared_context = dict(context or {})
        prepared: list[_PreparedQuery] = []
        results: dict[str, NodeResult] = {}
        seen_queries: set[str] = set()
        for item in inputs:
            query = str(item.spec.query or "").strip()
            if query in seen_queries:
                results[query] = NodeResult.error(
                    "RELEVANCE_DUPLICATE_QUERY", "批量输入中存在重复 Query"
                )
                continue
            seen_queries.add(query)
            current = self._prepare(item, shared_context)
            if isinstance(current, NodeResult):
                results[query] = current
            else:
                prepared.append(current)

        batches: list[list[_PreparedQuery]] = []
        current_batch: list[_PreparedQuery] = []
        boundary_batch = False
        for item in prepared:
            if not item.pending:
                skip_reason = (
                    "all_products_reused"
                    if item.result.get("product_count", 0) > 0
                    else "no_products_to_check"
                )
                result = dict(item.result)
                result["relevanceSkipReason"] = skip_reason
                result["telemetry"] = {
                    **dict(item.telemetry or {}),
                    "modelCallCount": 0,
                    "batchQueryCount": 0,
                    "executionSeconds": 0.0,
                    "independentExecutionSeconds": 0.0,
                    "roundSeconds": 0.0,
                }
                results[item.query] = NodeResult.executed(result)
                results[item.query].telemetry = dict(result["telemetry"])
                continue
            reason = self._single_query_limit_error(item)
            if reason is not None:
                results[item.query] = NodeResult.error(*reason)
                continue
            candidate = [*current_batch, item]
            if current_batch and not self._within_limits(candidate):
                batches.append(current_batch)
                # Preserve Query atomicity at a batch boundary.  The Query
                # that triggered the boundary starts a fresh request; the
                # remainder of that request is intentionally not back-filled
                # by later Queries.
                current_batch = [item]
                boundary_batch = True
                continue
            if boundary_batch and current_batch:
                batches.append(current_batch)
                current_batch = [item]
                boundary_batch = False
                continue
            if not current_batch and not self._within_limits([item]):
                results[item.query] = NodeResult.error(
                    "RELEVANCE_REQUEST_TOO_LARGE", "单个 Query 请求超过相关性请求限制"
                )
                continue
            current_batch.append(item)
        if current_batch:
            batches.append(current_batch)

        if batches:
            max_workers = min(self.config.concurrency, len(batches))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [
                    pool.submit(
                        self._run_batch,
                        batch,
                        shared_context,
                        batch_index=index,
                        batch_count=len(batches),
                    )
                    for index, batch in enumerate(batches, start=1)
                ]
                for future in futures:
                    results.update(future.result())
        for query, result in results.items():
            result.result = dict(result.result or {})
            result.result["modelCallAudits"] = list(self._query_call_audits.get(query, []))
            if query not in self._query_outcomes:
                self._record_query_outcome(query, result)
        return results

    def _prepare(
        self, item: RelevanceBatchInput, shared_context: Mapping[str, Any]
    ) -> _PreparedQuery | NodeResult:
        try:
            products = [
                product
                if isinstance(product, AdaptedProduct)
                else self.adapters.adapt(item.spec.brand, product, rank=index)
                for index, product in enumerate(item.products, start=1)
            ]
            product_ids = [_product_id(product) for product in products]
            reused = self.record_store.get_relevant_product_ids(
                item.spec.brand, item.spec.query, product_ids
            )
        except ProductAdapterError as exc:
            return NodeResult.error("RELEVANCE_PRODUCT_INVALID", exc.message)
        except Exception as exc:
            return NodeResult.error("RELEVANCE_RECORD_READ_FAILED", self._error_text(exc))

        pending = [product for product in products if _product_id(product) not in reused]
        base_result: dict[str, Any] = {
            "query": item.spec.query,
            "product_count": len(products),
            "reused_product_ids": [product_id for product_id in product_ids if product_id in reused],
            "requested_product_ids": product_ids,
            "detected_product_ids": [],
            "relevant_product_ids": [],
            "blacklist_candidates": [],
            "warnings": [],
        }
        if not pending:
            return _PreparedQuery(
                item,
                products,
                pending,
                product_ids,
                list(reused),
                {},
                "",
                base_result,
                telemetry={
                    "imagePreparationSeconds": 0.0,
                    "imageSuccessCount": 0,
                    "imageFailureCount": 0,
                },
            )

        try:
            local_context = {**shared_context, **dict(item.context)}
            supplied_guidance = item.guidance
            if supplied_guidance is None:
                supplied_guidance = local_context.get("relevance_guidance")
            guidance = str(supplied_guidance or "")
            if supplied_guidance is None:
                getter = getattr(self.record_store, "get_guidance", None)
                if callable(getter):
                    guidance = str(getter(item.spec.brand, item.spec.query) or "")
            source_urls = [
                normalize_image_url(
                    product.image_url
                    if isinstance(product, AdaptedProduct)
                    else _product_value(product, "imageUrl")
                )
                for product in pending
            ]
            image_started = time.perf_counter()
            prepare_products = getattr(self.image_cache, "prepare_products", None)
            assets = (
                prepare_products(pending, brand=item.spec.brand)
                if callable(prepare_products)
                else self.image_cache.prepare_many(source_urls)
            )
            image_duration = max(0.0, time.perf_counter() - image_started)
            image_success = sum(
                1
                for product in pending
                if (asset := _asset_for(product, assets)) is not None
                and asset.available
                and bool(asset.url)
            )
            for product in pending:
                asset = _asset_for(product, assets)
                if asset and not asset.available:
                    base_result["warnings"].append(
                        {
                            "code": asset.warning_code or "IMAGE_CACHE_FAILED",
                            "message": asset.warning_message or "图片不可用，将使用商品文本判断",
                            "product_id": _product_id(product),
                        }
                    )
            return _PreparedQuery(
                item,
                products,
                pending,
                product_ids,
                list(reused),
                assets,
                guidance,
                base_result,
                telemetry={
                    "imagePreparationSeconds": round(image_duration, 6),
                    "imageSuccessCount": image_success,
                    "imageFailureCount": max(0, len(pending) - image_success),
                },
            )
        except Exception as exc:
            return NodeResult.error("RELEVANCE_IMAGE_PREPARE_FAILED", self._error_text(exc))

    def _single_query_limit_error(
        self, item: _PreparedQuery
    ) -> tuple[str, str] | None:
        if item.product_weight > self.config.batch_size:
            return "RELEVANCE_BATCH_QUERY_TOO_LARGE", "单个 Query 商品数超过批量上限"
        if not self._within_request_limit([item]):
            return "RELEVANCE_REQUEST_TOO_LARGE", "单个 Query 请求超过请求体大小上限"
        return None

    def _within_limits(self, batch: Sequence[_PreparedQuery]) -> bool:
        return (
            sum(item.product_weight for item in batch) <= self.config.batch_size
            and self._within_request_limit(batch)
        )

    def _within_request_limit(self, batch: Sequence[_PreparedQuery]) -> bool:
        messages = build_relevance_batch_messages(batch)
        size = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return size <= self.config.request_max_bytes

    def _run_batch(
        self,
        batch: Sequence[_PreparedQuery],
        shared_context: Mapping[str, Any],
        *,
        batch_index: int = 1,
        batch_count: int = 1,
    ) -> dict[str, NodeResult]:
        results: dict[str, NodeResult] = {}
        model_started = time.perf_counter()
        batch_id = f"relevance-batch-{uuid4().hex}"
        invocation: _InvocationEnvelope | None = None
        try:
            invocation = self._invoke(
                batch,
                shared_context,
                batch_id=batch_id,
                attempt=1,
                operation="batch",
            )
            payload = self._decode_payload(invocation)
            parsed, failures = self._parse_batch_payload(payload, batch)
            audit = getattr(invocation, "audit", None)
            if failures and isinstance(invocation, _InvocationEnvelope):
                self._update_telemetry(
                    invocation.request_id,
                    status="partial" if parsed else "failed",
                    error_code="RELEVANCE_OUTPUT_INVALID",
                    failed_query_count=len(failures),
                )
        except Exception as exc:
            parsed = {}
            failures = {
                item.query: self._as_output_error(exc)
                for item in batch
            }
            audit = getattr(invocation, "audit", None)
            if isinstance(invocation, _InvocationEnvelope):
                self._update_telemetry(
                    invocation.request_id,
                    status="failed",
                    error_code=self._error_code(exc),
                )
        model_duration = max(0.0, time.perf_counter() - model_started)
        model_batch_key = f"model:{batch_index}"
        truncation_keys = [model_batch_key] if self._truncation_count(invocation) else []

        retry_items: list[tuple[_PreparedQuery, Exception, tuple[str, ...]]] = []
        for item in batch:
            if item.query in parsed:
                results[item.query] = self._finish_query(
                    item,
                    parsed[item.query],
                    audit,
                    batch_telemetry={
                        "batchModelSeconds": round(model_duration, 6),
                        "batchQueryCount": len(batch),
                        "modelCallCount": 1,
                        "retryCount": 0,
                        "batchKey": model_batch_key,
                        "maxConcurrency": self.config.concurrency,
                        "truncationCount": len(truncation_keys),
                        "truncationKeys": list(truncation_keys),
                    },
                )
                continue
            if item.query not in failures:
                failures[item.query] = RelevanceOutputError("Query result is missing")
            if self._is_permanent_model_unavailable(failures[item.query]):
                error = NodeResult.error(
                    self._error_code(failures[item.query]),
                    self._error_text(failures[item.query]),
                )
                error.telemetry = {
                    **item.telemetry,
                    "batchModelSeconds": round(model_duration, 6),
                    "modelSeconds": round(model_duration, 6),
                    "batchQueryCount": len(batch),
                    "modelCallCount": 1,
                    "retryCount": 0,
                    "batchKey": model_batch_key,
                    "maxConcurrency": self.config.concurrency,
                    "truncationCount": len(truncation_keys),
                    "truncationKeys": list(truncation_keys),
                    "sharedBatchSeconds": round(model_duration, 6) if len(batch) > 1 else 0.0,
                    "independentExecutionSeconds": round(
                        float(item.telemetry.get("imagePreparationSeconds") or 0), 6
                    ) if len(batch) > 1 else round(model_duration + float(item.telemetry.get("imagePreparationSeconds") or 0), 6),
                    "roundSeconds": round(
                        model_duration + float(item.telemetry.get("imagePreparationSeconds") or 0), 6
                    ),
                    "executionSeconds": round(
                        model_duration + float(item.telemetry.get("imagePreparationSeconds") or 0), 6
                    ),
                }
                results[item.query] = error
                self._record_query_outcome(item.query, error)
                continue
            retry_items.append((item, failures[item.query], tuple(truncation_keys)))
        if retry_items:
            max_workers = min(self.config.concurrency, len(retry_items))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_map = {
                    pool.submit(
                        self._retry_query,
                        item,
                        shared_context,
                        error,
                        initial_truncation_keys,
                        batch_id,
                    ): item.query
                    for item, error, initial_truncation_keys in retry_items
                }
                for future in as_completed(future_map):
                    query = future_map[future]
                    results[query] = future.result()
                    self._record_query_outcome(query, results[query])
        for query, result in results.items():
            if query not in self._query_outcomes:
                self._record_query_outcome(query, result)
        return results

    def _retry_query(
        self,
        item: _PreparedQuery,
        shared_context: Mapping[str, Any],
        first_error: Exception,
        initial_truncation_keys: Sequence[str] = (),
        batch_id: str | None = None,
    ) -> NodeResult:
        last_error: Exception = first_error
        retry_seconds = 0.0
        truncation_keys = list(
            dict.fromkeys(str(key) for key in initial_truncation_keys if str(key).strip())
        )
        if self._is_permanent_model_unavailable(last_error):
            return NodeResult.error(self._error_code(last_error), self._error_text(last_error))
        if (
            isinstance(first_error, RelevanceOutputError)
            and len(item.pending) > _RELEVANCE_REPAIR_CHUNK_SIZE
        ):
            repaired = self._retry_query_in_chunks(
                item,
                shared_context,
                first_error,
                batch_id=batch_id or f"relevance-batch-{uuid4().hex}",
                initial_truncation_keys=truncation_keys,
            )
            if repaired is not None:
                return repaired
        for attempt in range(1, self.config.max_query_retries + 1):
            model_started = time.perf_counter()
            invocation: _InvocationEnvelope | None = None
            try:
                invocation = self._invoke(
                    [item],
                    shared_context,
                    batch_id=batch_id or f"relevance-batch-{uuid4().hex}",
                    attempt=attempt + 1,
                    operation="query_retry",
                    retry_reason=self._error_text(last_error),
                )
                retry_batch_key = f"retry:{item.query}:{attempt}"
                if self._truncation_count(invocation):
                    truncation_keys.append(retry_batch_key)
                    truncation_keys = list(dict.fromkeys(truncation_keys))
                payload = self._decode_payload(invocation)
                parsed, failures = self._parse_batch_payload(payload, [item])
                if item.query in parsed:
                    attempt_seconds = max(0.0, time.perf_counter() - model_started)
                    return self._finish_query(
                        item,
                        parsed[item.query],
                        getattr(invocation, "audit", None),
                        batch_telemetry={
                            "batchModelSeconds": round(attempt_seconds, 6),
                            "batchQueryCount": 1,
                            "modelCallCount": 1,
                            "retryCount": attempt,
                            "batchKey": retry_batch_key,
                            "maxConcurrency": self.config.concurrency,
                            "retrySeconds": round(retry_seconds + attempt_seconds, 6),
                            "retryModelSeconds": round(retry_seconds + attempt_seconds, 6),
                            "truncationCount": len(truncation_keys),
                            "truncationKeys": list(truncation_keys),
                        },
                    )
                retry_seconds += max(0.0, time.perf_counter() - model_started)
                last_error = failures.get(item.query) or RelevanceOutputError("Query result is missing")
                if isinstance(invocation, _InvocationEnvelope):
                    self._update_telemetry(
                        invocation.request_id,
                        status="failed",
                        error_code=self._error_code(last_error),
                    )
                if self._is_permanent_model_unavailable(last_error):
                    break
            except Exception as exc:
                retry_seconds += max(0.0, time.perf_counter() - model_started)
                last_error = exc
                if isinstance(invocation, _InvocationEnvelope):
                    self._update_telemetry(
                        invocation.request_id,
                        status="failed",
                        error_code=self._error_code(exc),
                    )
                if self._is_permanent_model_unavailable(last_error):
                    break
        error = NodeResult.error(self._error_code(last_error), self._error_text(last_error))
        error.telemetry = {
            **item.telemetry,
            "batchModelSeconds": round(retry_seconds, 6),
            "modelSeconds": round(retry_seconds, 6),
            "modelCallCount": self.config.max_query_retries,
            "retryCount": self.config.max_query_retries,
            "batchKey": f"retry:{item.query}:failed",
            "maxConcurrency": self.config.concurrency,
            "retrySeconds": round(retry_seconds, 6),
            "retryModelSeconds": round(retry_seconds, 6),
            "truncationCount": len(truncation_keys),
            "truncationKeys": list(truncation_keys),
            "roundSeconds": round(
                float(item.telemetry.get("imagePreparationSeconds") or 0) + retry_seconds,
                6,
            ),
            "independentExecutionSeconds": round(
                float(item.telemetry.get("imagePreparationSeconds") or 0) + retry_seconds,
                6,
            ),
            "executionSeconds": round(
                float(item.telemetry.get("imagePreparationSeconds") or 0) + retry_seconds,
                6,
            ),
        }
        return error

    def _retry_query_in_chunks(
        self,
        item: _PreparedQuery,
        shared_context: Mapping[str, Any],
        first_error: Exception,
        *,
        batch_id: str,
        initial_truncation_keys: Sequence[str] = (),
    ) -> NodeResult | None:
        """Repair a long invalid Query response with bounded product chunks.

        A long multimodal response can omit one reason or repeat its Query even
        when the same model handles a small group correctly.  This path is only
        entered after the strict whole-Query parser rejects a response.  Each
        chunk is still parsed by the same strict parser and the final result is
        completed only when every input product has a decision.
        """

        products = list(item.pending)
        chunks = [
            products[index : index + _RELEVANCE_REPAIR_CHUNK_SIZE]
            for index in range(0, len(products), _RELEVANCE_REPAIR_CHUNK_SIZE)
        ]
        decisions: list[RelevanceDecision] = []
        audits: list[Mapping[str, Any]] = []
        truncation_keys = list(
            dict.fromkeys(str(key) for key in initial_truncation_keys if str(key).strip())
        )
        total_seconds = 0.0
        total_attempts = 0
        for chunk_index, chunk_products in enumerate(chunks, start=1):
            chunk_item = replace(item, pending=list(chunk_products))
            last_error: Exception = first_error
            chunk_succeeded = False
            for attempt in range(1, self.config.max_query_retries + 1):
                started = time.perf_counter()
                invocation: _InvocationEnvelope | None = None
                try:
                    invocation = self._invoke(
                        [chunk_item],
                        shared_context,
                        batch_id=f"{batch_id}:chunk-{chunk_index}",
                        attempt=attempt,
                        operation="query_retry_chunk",
                        retry_reason=self._error_text(last_error),
                    )
                    if self._truncation_count(invocation):
                        truncation_keys.append(f"retry:chunk:{chunk_index}:{attempt}")
                        truncation_keys = list(dict.fromkeys(truncation_keys))
                    payload = self._decode_payload(invocation)
                    parsed, failures = self._parse_batch_payload(payload, [chunk_item])
                    elapsed = max(0.0, time.perf_counter() - started)
                    total_seconds += elapsed
                    total_attempts += 1
                    if chunk_item.query in parsed:
                        decisions.extend(parsed[chunk_item.query])
                        audit = getattr(invocation, "audit", None)
                        if isinstance(audit, Mapping):
                            audits.append(dict(audit))
                        chunk_succeeded = True
                        break
                    last_error = failures.get(chunk_item.query) or RelevanceOutputError(
                        "Query result is missing"
                    )
                    if isinstance(invocation, _InvocationEnvelope):
                        self._update_telemetry(
                            invocation.request_id,
                            status="failed",
                            error_code=self._error_code(last_error),
                        )
                except Exception as exc:
                    total_seconds += max(0.0, time.perf_counter() - started)
                    total_attempts += 1
                    last_error = exc
                    if isinstance(invocation, _InvocationEnvelope):
                        self._update_telemetry(
                            invocation.request_id,
                            status="failed",
                            error_code=self._error_code(exc),
                        )
                if self._is_permanent_model_unavailable(last_error):
                    break
            if not chunk_succeeded:
                return None

        if len({decision.product_id for decision in decisions}) != len(item.pending):
            return None
        merged_audit: dict[str, Any] = dict(audits[-1]) if audits else {}
        if audits:
            merged_audit["modelName"] = next(
                (audit.get("modelName") for audit in audits if audit.get("modelName")),
                None,
            )
            durations = [
                float(audit.get("durationSeconds"))
                for audit in audits
                if isinstance(audit.get("durationSeconds"), (int, float))
            ]
            if durations:
                merged_audit["durationSeconds"] = sum(durations)
        return self._finish_query(
            item,
            decisions,
            merged_audit,
            batch_telemetry={
                "batchModelSeconds": round(total_seconds, 6),
                "batchQueryCount": 1,
                "modelCallCount": total_attempts,
                "retryCount": total_attempts,
                "batchKey": f"retry:{item.query}:chunked",
                "maxConcurrency": self.config.concurrency,
                "retrySeconds": round(total_seconds, 6),
                "retryModelSeconds": round(total_seconds, 6),
                "truncationCount": len(truncation_keys),
                "truncationKeys": list(truncation_keys),
            },
        )

    def _invoke(
        self,
        batch: Sequence[_PreparedQuery],
        shared_context: Mapping[str, Any],
        *,
        batch_id: str,
        attempt: int,
        operation: str,
        retry_reason: str | None = None,
    ) -> Any:
        messages = build_relevance_batch_messages(
            batch,
            retry_instruction=retry_reason if operation == "query_retry" else None,
        )
        kwargs = RelevanceCheckRunner._invoke_kwargs(shared_context)
        kwargs["timeout"] = self.config.model_timeout_seconds
        request_id = f"relevance-request-{uuid4().hex}"
        metadata = dict(kwargs.get("metadata") or {})
        metadata.update(
            {
                "relevance_request_id": request_id,
                "relevance_batch_id": batch_id,
                "relevance_attempt": attempt,
                "relevance_operation": operation,
            }
        )
        kwargs["metadata"] = metadata
        # Keep JSON syntax deterministic; semantic completeness remains enforced
        # by the strict parser below.
        kwargs["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        with self.concurrency_controller or nullcontext():
            try:
                invocation = self.runtime.invoke(messages, **kwargs)
            except Exception as exc:
                self._record_call_audit(batch, request_id, getattr(exc, "model_call_audits", None))
                self._record_telemetry(
                    request_id=request_id,
                    batch_id=batch_id,
                    batch=batch,
                    attempt=attempt,
                    operation=operation,
                    status="failed",
                    error_code=self._error_code(exc),
                    duration_seconds=time.perf_counter() - started,
                    retry_reason=retry_reason,
                )
                raise
        event = self._record_telemetry(
            request_id=request_id,
            batch_id=batch_id,
            batch=batch,
            attempt=attempt,
            operation=operation,
            status="success",
            duration_seconds=time.perf_counter() - started,
            retry_reason=retry_reason,
        )
        audit = getattr(invocation, "audit", None)
        merged_audit = dict(audit) if isinstance(audit, Mapping) else {}
        merged_audit["requestId"] = request_id
        merged_audit["batchQueryCount"] = len(batch)
        self._record_call_audit(batch, request_id, merged_audit.get("modelCallAudits"), merged_audit)
        merged_audit["relevanceTelemetry"] = event
        return _InvocationEnvelope(
            response=getattr(invocation, "response", invocation),
            audit=merged_audit,
        )

    def _record_call_audit(self, batch, request_id, calls=None, audit=None) -> None:
        records = calls if isinstance(calls, list) and calls else [dict(audit or {"tokenUsage": None})]
        with self._telemetry_lock:
            for index, record in enumerate(records):
                record = {**record, "requestId": f"{request_id}:{index}", "batchQueryCount": len(batch)}
                record.pop("modelCallAudits", None)
                for item in batch:
                    self._query_call_audits.setdefault(item.query, []).append(dict(record))

    def _record_telemetry(
        self,
        *,
        request_id: str,
        batch_id: str,
        batch: Sequence[_PreparedQuery],
        attempt: int,
        operation: str,
        status: str,
        duration_seconds: float,
        error_code: str | None = None,
        retry_reason: str | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "request_id": request_id,
            "batch_id": batch_id,
            "attempt": attempt,
            "operation": operation,
            "status": status,
            "query_count": len(batch),
            "queries": [item.query for item in batch],
            "product_count": sum(item.product_weight for item in batch),
            "duration_seconds": max(0.0, float(duration_seconds)),
        }
        if error_code:
            event["error_code"] = error_code
        if retry_reason:
            event["retry_reason"] = retry_reason
        with self._telemetry_lock:
            self._telemetry.append(event)
        return event

    def _update_telemetry(self, request_id: str | None, **updates: Any) -> None:
        if not request_id:
            return
        with self._telemetry_lock:
            for event in reversed(self._telemetry):
                if event.get("request_id") == request_id:
                    event.update(updates)
                    return

    def _record_query_outcome(self, query: str, result: NodeResult) -> None:
        with self._telemetry_lock:
            events = [event for event in self._telemetry if query in event.get("queries", [])]
            self._query_outcomes[query] = {
                "query": query,
                "status": result.status.value,
                "attempts": len(events),
                "duration_seconds": sum(float(event.get("duration_seconds") or 0.0) for event in events),
                "request_ids": [str(event["request_id"]) for event in events if event.get("request_id")],
            }
            if result.error_code:
                self._query_outcomes[query]["error_code"] = result.error_code

    def _finish_query(
        self,
        item: _PreparedQuery,
        decisions: Sequence[RelevanceDecision],
        audit: Any,
        *,
        batch_telemetry: Mapping[str, Any] | None = None,
    ) -> NodeResult:
        result = item.result
        legacy_telemetry = {
            "batch_index": 1,
            "batch_count": 1,
            "product_count": item.product_weight,
            "image_download_success_count": item.image_count,
            "image_download_failure_count": item.product_weight - item.image_count,
        }
        if batch_telemetry:
            legacy_telemetry.update(batch_telemetry)
        result["telemetry"] = legacy_telemetry
        product_map = {_product_id(product): product for product in item.pending}
        returned_ids: set[str] = set()
        model_name = audit.get("modelName") if isinstance(audit, Mapping) else None
        for decision in decisions:
            returned_ids.add(decision.product_id)
            result["detected_product_ids"].append(decision.product_id)
            product = product_map[decision.product_id]
            asset = _asset_for(product, item.assets)
            if decision.is_relevant:
                try:
                    self.record_store.save_relevant(
                        item.item.spec.brand,
                        item.item.spec.query,
                        decision.product_id,
                        model_name=model_name,
                        detection_type="multimodal" if asset and asset.available else "text",
                    )
                except Exception as exc:
                    error = NodeResult.error("RELEVANCE_RECORD_SAVE_FAILED", self._error_text(exc))
                    error.telemetry = {**item.telemetry, **dict(batch_telemetry or {})}
                    return error
                result["relevant_product_ids"].append(decision.product_id)
            else:
                result["blacklist_candidates"].append(
                    {
                        "product_id": decision.product_id,
                        "product_name": _product_value(product, "productName") or _product_value(product, "product_name") or _product_value(product, "name") or "",
                        "image_url": product.image_url if isinstance(product, AdaptedProduct) else _product_value(product, "imageUrl") or _product_value(product, "image_url") or _product_value(product, "mainImage") or "",
                        "bucket": product_bucket(product),
                        "is_relevant": False,
                        "reason": decision.reason,
                        "detection_types": ["Image", "Text"] if asset and asset.available else ["Text"],
                        "image_checked": bool(asset and asset.available),
                        "image_warning": (asset.warning_code if asset is not None and not asset.available else None),
                        "image_warning_message": (asset.warning_message if asset is not None and not asset.available else None),
                    }
                )
        missing = [product_id for product_id in (_product_id(product) for product in item.pending) if product_id not in returned_ids]
        if missing:
            # Some providers return a valid subset while preserving the Query
            # envelope. Do not invent a blacklist decision for omitted products;
            # expose the gap for the caller and keep the Query isolated.
            result["warnings"].append(
                {
                    "code": "RELEVANCE_OUTPUT_MISSING_PRODUCT",
                    "message": "模型响应未覆盖全部商品，未覆盖商品不生成拉黑候选",
                    "product_ids": missing,
                }
            )
        if isinstance(audit, Mapping):
            result["audit"] = dict(audit)
        blacklist_ids = [
            str(item.get("product_id") or "").strip()
            for item in result["blacklist_candidates"]
            if isinstance(item, Mapping) and str(item.get("product_id") or "").strip()
        ]
        action = None
        if blacklist_ids:
            suspect_warnings = {
                str(candidate.get("product_id")): str(candidate.get("image_warning") or "")
                for candidate in result["blacklist_candidates"]
                if isinstance(candidate, Mapping)
                and candidate.get("product_id")
                and is_suspect_image_warning(candidate.get("image_warning"))
            }
            action = {
                "type": "product_blacklist",
                "brand": item.item.spec.brand,
                "query": item.item.spec.query,
                "product_ids": list(dict.fromkeys(blacklist_ids)),
                "product_names": {
                    str(candidate["product_id"]): str(candidate.get("product_name") or "")
                    for candidate in result["blacklist_candidates"]
                },
                "product_images": {
                    str(candidate["product_id"]): str(candidate.get("image_url") or candidate.get("imageUrl") or "")
                    for candidate in result["blacklist_candidates"]
                    if isinstance(candidate, Mapping) and candidate.get("product_id")
                },
                "reasons": {
                    str(candidate.get("product_id")): candidate.get("reason")
                    for candidate in result["blacklist_candidates"]
                    if isinstance(candidate, Mapping) and candidate.get("product_id")
                },
            }
            # 只把“图片本身可疑”（共用/错配）的告警带进 action：这类才需要护栏降级，
            # 单纯没图（IMAGE_URL_MISSING/DOWNLOAD_FAILED）保持历史行为与历史载荷形态。
            if suspect_warnings:
                action["image_warnings"] = suspect_warnings
                action["image_warning_messages"] = {
                    str(candidate.get("product_id")): str(candidate.get("image_warning_message") or "")
                    for candidate in result["blacklist_candidates"]
                    if isinstance(candidate, Mapping)
                    and str(candidate.get("product_id") or "") in suspect_warnings
                }
            product_buckets = {
                str(candidate.get("product_id")): str(candidate.get("bucket") or "").strip()
                for candidate in result["blacklist_candidates"]
                if isinstance(candidate, Mapping)
                and candidate.get("product_id")
                and str(candidate.get("bucket") or "").strip()
            }
            if product_buckets:
                action["product_buckets"] = product_buckets
        outcome = NodeResult.executed(result, action=action)
        audit_duration = audit.get("durationSeconds") if isinstance(audit, Mapping) else None
        outcome.telemetry = {
            **item.telemetry,
            **dict(batch_telemetry or {}),
            "modelSeconds": float(audit_duration) if isinstance(audit_duration, (int, float)) else None,
        }
        batch_query_count = int(outcome.telemetry.get("batchQueryCount") or 1)
        batch_model_seconds = outcome.telemetry.get("batchModelSeconds")
        if not isinstance(batch_model_seconds, (int, float)):
            batch_model_seconds = 0.0
        image_seconds = float(outcome.telemetry.get("imagePreparationSeconds") or 0)
        if outcome.telemetry.get("modelSeconds") is None:
            outcome.telemetry["modelSeconds"] = round(float(batch_model_seconds), 6)
        outcome.telemetry["sharedBatchSeconds"] = round(
            float(batch_model_seconds) if batch_query_count > 1 else 0.0,
            6,
        )
        outcome.telemetry["executionSeconds"] = round(
            image_seconds
            + float(outcome.telemetry.get("modelSeconds") or 0),
            6,
        )
        outcome.telemetry["independentExecutionSeconds"] = round(
            image_seconds
            + (float(outcome.telemetry.get("modelSeconds") or 0) if batch_query_count <= 1 else 0.0),
            6,
        )
        outcome.telemetry["roundSeconds"] = round(
            image_seconds + float(batch_model_seconds), 6
        )
        self._record_query_outcome(item.query, outcome)
        return outcome

    @staticmethod
    def _parse_batch_payload(
        payload: Mapping[str, Any], batch: Sequence[_PreparedQuery]
    ) -> tuple[dict[str, list[RelevanceDecision]], dict[str, Exception]]:
        if not isinstance(payload, Mapping):
            raise RelevanceOutputError("model response must be an object")
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise RelevanceOutputError("results must be a list")
        expected = {item.query: item for item in batch}
        groups: dict[str, Mapping[str, Any]] = {}
        global_error: RelevanceOutputError | None = None
        for raw in raw_results:
            if not isinstance(raw, Mapping):
                global_error = RelevanceOutputError("each Query result must be an object")
                break
            query = str(raw.get("query") or "").strip()
            if query not in expected:
                global_error = RelevanceOutputError(f"unknown Query: {query}")
                break
            if query in groups:
                global_error = RelevanceOutputError(f"duplicate Query: {query}")
                break
            groups[query] = raw
        if global_error is not None:
            return {}, {item.query: global_error for item in batch}

        parsed: dict[str, list[RelevanceDecision]] = {}
        failures: dict[str, Exception] = {}
        for item in batch:
            group = groups.get(item.query)
            if group is None:
                failures[item.query] = RelevanceOutputError("Query result is missing")
                continue
            try:
                # Keep the strict single-Query parser as the source of truth for
                # IDs, duplicates, booleans, reasons, and complete product
                # coverage. A missing product makes this Query invalid and sends
                # the complete group through the Query-level retry path.
                parsed[item.query] = RelevanceCheckRunner.parse_output(
                    {"results": [group]}, query=item.query, products=item.pending
                )
            except RelevanceOutputError as exc:
                failures[item.query] = exc
        return parsed, failures

    @staticmethod
    def _decode_payload(invocation: Any) -> Mapping[str, Any]:
        response = getattr(invocation, "response", invocation)
        return RelevanceCheckRunner._decode_payload(getattr(response, "content", response))

    @staticmethod
    def _truncation_count(invocation: Any) -> int:
        """Return one when a model response ended because of the token limit."""

        if invocation is None:
            return 0
        response = getattr(invocation, "response", invocation)
        metadata_values = (
            getattr(response, "response_metadata", None),
            getattr(invocation, "response_metadata", None),
        )
        for metadata in metadata_values:
            if not isinstance(metadata, Mapping):
                continue
            finish_reason = metadata.get("finish_reason", metadata.get("finishReason"))
            if isinstance(finish_reason, Mapping):
                finish_reason = finish_reason.get("reason")
            if isinstance(finish_reason, (list, tuple, set, frozenset)):
                if any(str(value).strip().lower() == "length" for value in finish_reason):
                    return 1
            elif str(finish_reason).strip().lower() == "length":
                return 1
        return 0

    @staticmethod
    def _as_output_error(exc: Exception) -> Exception:
        if isinstance(exc, RelevanceOutputError):
            return exc
        return exc

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if RelevanceBatchRunner._is_permanent_model_unavailable(exc):
            return "RELEVANCE_MODEL_UNAVAILABLE"
        if isinstance(exc, RelevanceOutputError):
            return "RELEVANCE_OUTPUT_INVALID"
        if isinstance(exc, RelevanceError):
            return exc.error_code
        if isinstance(exc, TimeoutError) or "timeout" in str(exc).lower():
            return "RELEVANCE_MODEL_TIMEOUT"
        return "RELEVANCE_MODEL_FAILED"

    @staticmethod
    def _is_permanent_model_unavailable(exc: Exception) -> bool:
        text = str(exc).lower()
        return "model_not_found" in text or "no available channel" in text

    @staticmethod
    def _error_text(exc: Exception) -> str:
        return str(exc).strip() or exc.__class__.__name__


__all__ = [
    "RelevanceBatchConfig",
    "RelevanceBatchInput",
    "RelevanceBatchRunner",
    "build_relevance_batch_messages",
]
