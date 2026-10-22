"""Vector re-ranking integration for the deterministic reorder node.

The module owns only the vector service boundary and its Query-level result
normalization.  Sorting writes remain behind the action gateway and belong to
the following sorting-execution task.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import os
import threading
import time
from typing import Any

import requests

from .models import NodeResult, NodeStatus, QuerySpec
from .reorder_actions import build_query_reorder_action


DEFAULT_VECTOR_REORDER_ENDPOINT = (
    "http://vector.example.test/api/hologres/recall/search"
)
DEFAULT_VECTOR_REORDER_TIMEOUT_SECONDS = 10.0
DEFAULT_VECTOR_REORDER_PRODUCT_LIMIT = 16
DEFAULT_VECTOR_REORDER_CONCURRENCY = 10
DEFAULT_VECTOR_REORDER_PAGE_NUM = 1
DEFAULT_VECTOR_REORDER_RETRY_ATTEMPTS = 3
DEFAULT_VECTOR_REORDER_RETRY_BACKOFF_SECONDS = 0.5
MAX_STOCK_FILTER_PAGES = 100

_GENDER_SUFFIXES = {
    "女士": "女士",
    "男士": "男士",
    "儿童": "儿童",
    "家居": "家居",
}


class VectorReorderError(RuntimeError):
    """A stable failure from the vector re-ranking boundary."""

    def __init__(self, error_code: str, message: str, *, attempts: int = 1) -> None:
        self.error_code = error_code
        self.message = message
        self.attempts = attempts
        super().__init__(message)


@dataclass(frozen=True)
class VectorReorderConfig:
    """Runtime limits captured for one vector-reorder execution."""

    endpoint_url: str = DEFAULT_VECTOR_REORDER_ENDPOINT
    timeout_seconds: float = DEFAULT_VECTOR_REORDER_TIMEOUT_SECONDS
    product_limit: int = DEFAULT_VECTOR_REORDER_PRODUCT_LIMIT
    concurrency: int = DEFAULT_VECTOR_REORDER_CONCURRENCY
    page_num: int = DEFAULT_VECTOR_REORDER_PAGE_NUM
    api_alias: str | None = None
    retry_attempts: int = DEFAULT_VECTOR_REORDER_RETRY_ATTEMPTS
    retry_backoff_seconds: float = DEFAULT_VECTOR_REORDER_RETRY_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        if not str(self.endpoint_url).strip():
            raise ValueError("endpoint_url is required")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        for name in ("product_limit", "concurrency", "page_num", "retry_attempts"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.retry_backoff_seconds, bool)
            or self.retry_backoff_seconds < 0
        ):
            raise ValueError("retry_backoff_seconds must be non-negative")

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, Any] | None
    ) -> "VectorReorderConfig":
        values = values or {}
        endpoint = (
            values.get("query_governance_vector_reorder_url")
            or values.get("endpoint_url")
            or os.getenv("QUERY_GOVERNANCE_VECTOR_REORDER_URL")
            or cls.endpoint_url
        )
        timeout = values.get("timeout_seconds", cls.timeout_seconds)
        product_limit = values.get(
            "query_governance_check_product_limit",
            values.get(
                "top_query_check_product_limit",
                values.get("product_limit", cls.product_limit),
            ),
        )
        concurrency = values.get(
            "query_governance_vector_reorder_concurrency",
            values.get("concurrency", cls.concurrency),
        )
        page_num = values.get(
            "query_governance_vector_reorder_page_num",
            values.get("page_num", cls.page_num),
        )
        api_alias = values.get("api_alias") or values.get("brand_alias")
        retry_attempts = values.get(
            "query_governance_vector_reorder_retry_attempts",
            values.get("retry_attempts", cls.retry_attempts),
        )
        retry_backoff_seconds = values.get(
            "query_governance_vector_reorder_retry_backoff_seconds",
            values.get("retry_backoff_seconds", cls.retry_backoff_seconds),
        )
        return cls(
            endpoint_url=str(endpoint),
            timeout_seconds=float(timeout),
            product_limit=int(product_limit),
            concurrency=int(concurrency),
            page_num=int(page_num),
            api_alias=str(api_alias).strip() if api_alias else None,
            retry_attempts=int(retry_attempts),
            retry_backoff_seconds=float(retry_backoff_seconds),
        )


@dataclass(frozen=True)
class VectorReorderResponse:
    """Normalized vector response, retaining returned order and raw fields."""

    items: tuple[Mapping[str, Any], ...]
    total: int | None
    page_num: int | None
    page_size: int | None
    request_payload: Mapping[str, Any]
    attempts: int = 1

    @property
    def selected_count(self) -> int:
        return len(self.items)


class VectorReorderClient:
    """Call and normalize the Hologres vector re-ranking endpoint."""

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        timeout_seconds: float = DEFAULT_VECTOR_REORDER_TIMEOUT_SECONDS,
        request_func: Callable[..., Any] | None = None,
        retry_attempts: int = DEFAULT_VECTOR_REORDER_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_VECTOR_REORDER_RETRY_BACKOFF_SECONDS,
        sleep_func: Callable[[float], Any] | None = None,
    ) -> None:
        endpoint = (
            endpoint_url
            or os.getenv("QUERY_GOVERNANCE_VECTOR_REORDER_URL")
            or DEFAULT_VECTOR_REORDER_ENDPOINT
        )
        self.endpoint_url = str(endpoint).strip()
        self.timeout_seconds = float(timeout_seconds)
        self.request_func = request_func or requests.post
        self.retry_attempts = self._validate_retry_attempts(retry_attempts)
        self.retry_backoff_seconds = self._validate_retry_backoff(retry_backoff_seconds)
        self.sleep_func = sleep_func or time.sleep
        if not self.endpoint_url:
            raise ValueError("endpoint_url is required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def rerank(
        self,
        brand: str,
        query: str,
        user_style: str,
        *,
        gender: str | None = None,
        page_size: int,
        page_num: int = DEFAULT_VECTOR_REORDER_PAGE_NUM,
        endpoint_url: str | None = None,
        timeout_seconds: float | None = None,
        retry_attempts: int | None = None,
        retry_backoff_seconds: float | None = None,
    ) -> VectorReorderResponse:
        normalized_brand = str(brand or "").strip()
        normalized_query = str(query or "").strip()
        normalized_style = str(user_style or "").strip()
        if not all((normalized_brand, normalized_query, normalized_style)):
            raise VectorReorderError(
                "VECTOR_REORDER_REQUEST_INVALID",
                "brand、query 和 userStyle 不能为空",
            )
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or page_size <= 0
        ):
            raise VectorReorderError(
                "VECTOR_REORDER_REQUEST_INVALID", "pageSize 必须是正整数"
            )
        if (
            isinstance(page_num, bool)
            or not isinstance(page_num, int)
            or page_num <= 0
        ):
            raise VectorReorderError(
                "VECTOR_REORDER_REQUEST_INVALID", "pageNum 必须是正整数"
            )

        payload: dict[str, Any] = {
            "brand": normalized_brand,
            "query": normalized_query,
            "userStyle": normalized_style,
            "pageSize": page_size,
            "pageNum": page_num,
        }
        normalized_gender = str(gender or "").strip()
        if normalized_gender:
            payload["gender"] = normalized_gender

        request_endpoint = str(endpoint_url or self.endpoint_url).strip()
        request_timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if not request_endpoint:
            raise VectorReorderError(
                "VECTOR_REORDER_REQUEST_INVALID", "向量接口地址不能为空"
            )
        if request_timeout <= 0:
            raise VectorReorderError(
                "VECTOR_REORDER_REQUEST_INVALID", "向量接口超时必须为正数"
            )
        attempts = self._validate_retry_attempts(
            self.retry_attempts if retry_attempts is None else retry_attempts
        )
        backoff = self._validate_retry_backoff(
            self.retry_backoff_seconds
            if retry_backoff_seconds is None
            else retry_backoff_seconds
        )
        for attempt in range(1, attempts + 1):
            try:
                response = self.request_func(
                    request_endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=request_timeout,
                )
            except Exception as exc:
                if self._is_retryable_exception(exc) and attempt < attempts:
                    self._wait_before_retry(backoff, attempt, payload)
                    continue
                raise VectorReorderError(
                    "VECTOR_REORDER_REQUEST_FAILED", _error_text(exc), attempts=attempt
                ) from exc

            status_code = getattr(response, "status_code", None)
            if isinstance(status_code, bool) or not isinstance(status_code, int):
                raise VectorReorderError(
                    "VECTOR_REORDER_RESPONSE_INVALID", "向量接口缺少有效 HTTP 状态码", attempts=attempt
                )
            if status_code < 200 or status_code >= 300:
                if (status_code == 429 or status_code >= 500) and attempt < attempts:
                    self._wait_before_retry(backoff, attempt, payload)
                    continue
                raise VectorReorderError(
                    "VECTOR_REORDER_HTTP_FAILED",
                    f"向量接口返回 HTTP {status_code}",
                    attempts=attempt,
                )

            try:
                body = response.json()
            except Exception as exc:
                if attempt < attempts:
                    self._wait_before_retry(backoff, attempt, payload)
                    continue
                raise VectorReorderError(
                    "VECTOR_REORDER_RESPONSE_INVALID", "向量接口返回内容不是有效 JSON", attempts=attempt
                ) from exc
            if not isinstance(body, Mapping):
                raise VectorReorderError(
                    "VECTOR_REORDER_RESPONSE_INVALID", "向量接口返回值不是对象", attempts=attempt
                )

            raw_items = body.get("result")
            if not isinstance(raw_items, list):
                raise VectorReorderError(
                    "VECTOR_REORDER_RESPONSE_INVALID", "向量接口 result 不是数组", attempts=attempt
                )
            if not raw_items:
                if attempt < attempts:
                    self._wait_before_retry(backoff, attempt, payload)
                    continue
                raise VectorReorderError(
                    "VECTOR_REORDER_NO_PRODUCTS", "向量接口没有返回可用商品", attempts=attempt
                )
            break
        else:
            raise VectorReorderError(
                "VECTOR_REORDER_NO_PRODUCTS", "向量接口没有返回可用商品", attempts=attempts
            )

        normalized_items: list[Mapping[str, Any]] = []
        for index, raw_item in enumerate(raw_items, start=1):
            if not isinstance(raw_item, Mapping):
                raise VectorReorderError(
                    "VECTOR_REORDER_RESPONSE_INVALID",
                    f"向量接口 result[{index - 1}] 不是商品对象",
                )
            product_id = _first_text(
                raw_item,
                "taggingUid",
                "tagging_uid",
                "productId",
                "product_id",
            )
            if product_id is None:
                raise VectorReorderError(
                    "VECTOR_REORDER_PRODUCT_ID_MISSING",
                    f"向量接口 result[{index - 1}] 缺少 taggingUid",
                    attempts=attempt,
                )
            item = dict(raw_item)
            item["product_id"] = product_id
            item["rank"] = index
            normalized_items.append(item)

        selected = tuple(normalized_items[:page_size])
        if not selected:
            raise VectorReorderError(
                "VECTOR_REORDER_NO_PRODUCTS",
                "向量接口没有返回可用商品",
                attempts=attempt,
            )
        return VectorReorderResponse(
            items=selected,
            total=_positive_int_or_none(body.get("total")),
            page_num=_positive_int_or_none(body.get("pageNum")),
            page_size=_positive_int_or_none(body.get("pageSize")),
            request_payload=payload,
            attempts=attempt,
        )

    @staticmethod
    def _validate_retry_attempts(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("retry_attempts must be a positive integer")
        return value

    @staticmethod
    def _validate_retry_backoff(value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("retry_backoff_seconds must be non-negative")
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("retry_backoff_seconds must be non-negative") from exc
        if normalized < 0:
            raise ValueError("retry_backoff_seconds must be non-negative")
        return normalized

    def _wait_before_retry(
        self, backoff: float, attempt: int, payload: Mapping[str, Any]
    ) -> None:
        seed = f"{payload['brand']}|{payload['query']}|{payload['pageNum']}"
        jitter = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:2], "big") / 65535
        delay = backoff * (2 ** (attempt - 1)) * (1 + jitter / 2)
        self.sleep_func(delay)

    @staticmethod
    def _is_retryable_exception(exc: Exception) -> bool:
        return isinstance(exc, (requests.Timeout, requests.ConnectionError))


@dataclass(frozen=True)
class VectorReorderInput:
    """One Query's preference result entering vector re-ranking."""

    spec: QuerySpec
    should_reorder: bool
    preference_description: str | None
    reason: str | None = None
    gender: str | None = None
    preference_audit: Mapping[str, Any] | None = None


class VectorReorderBatchRunner:
    """Run independent Query vector calls with bounded concurrency."""

    def __init__(
        self,
        *,
        client: VectorReorderClient,
        config: VectorReorderConfig | None = None,
        config_provider: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> None:
        self.client = client
        self.config = config or VectorReorderConfig()
        self.config_provider = config_provider

    def run_batch(
        self, inputs: Sequence[VectorReorderInput]
    ) -> dict[str, NodeResult]:
        items = list(inputs)
        self._validate_inputs(items)
        batch_started = time.perf_counter()
        results: dict[str, NodeResult] = {}
        eligible: list[VectorReorderInput] = []
        configs: dict[str, VectorReorderConfig] = {}
        for item in items:
            config = self._config_for(item.spec.brand)
            configs[item.spec.query_id] = config
            if not item.should_reorder:
                results[item.spec.query_id] = NodeResult.skipped(
                    item.reason or "未满足重排序执行条件"
                )
            else:
                eligible.append(item)
        if not eligible:
            return {
                item.spec.query_id: results[item.spec.query_id]
                for item in items
            }

        max_workers = max(
            configs[item.spec.query_id].concurrency for item in eligible
        )
        semaphores: dict[tuple[str, str, float, int], threading.Semaphore] = {}
        for item in eligible:
            config = configs[item.spec.query_id]
            key = (
                item.spec.brand,
                config.endpoint_url,
                config.timeout_seconds,
                config.concurrency,
            )
            semaphores.setdefault(key, threading.Semaphore(config.concurrency))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    self._run_one_limited,
                    item,
                    configs[item.spec.query_id],
                    semaphores[
                        (
                            item.spec.brand,
                            configs[item.spec.query_id].endpoint_url,
                            configs[item.spec.query_id].timeout_seconds,
                            configs[item.spec.query_id].concurrency,
                        )
                    ],
                ): item
                for item in eligible
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    outcome = future.result()
                except VectorReorderError as exc:
                    outcome = NodeResult.error(
                        exc.error_code, exc.message
                    )
                except Exception as exc:
                    outcome = NodeResult.error(
                        "VECTOR_REORDER_FAILED", _error_text(exc)
                    )
                outcome.telemetry = {
                    **dict(outcome.telemetry or {}),
                    "batchQueryCount": len(items),
                    "maxConcurrency": configs[item.spec.query_id].concurrency,
                }
                results[item.spec.query_id] = outcome
        batch_seconds = round(max(0.0, time.perf_counter() - batch_started), 6)
        for item in items:
            outcome = results[item.spec.query_id]
            outcome.telemetry = {
                **dict(outcome.telemetry or {}),
                "batchWallClockSeconds": batch_seconds,
            }
        return {
            item.spec.query_id: results[item.spec.query_id]
            for item in items
        }

    def _run_one_limited(
        self,
        item: VectorReorderInput,
        config: VectorReorderConfig,
        semaphore: threading.Semaphore,
    ) -> NodeResult:
        with semaphore:
            return self.run_one(item, config)

    def run_one(
        self,
        item: VectorReorderInput,
        config: VectorReorderConfig | None = None,
    ) -> NodeResult:
        if not item.should_reorder:
            return NodeResult.skipped(item.reason or "未满足重排序执行条件")
        preference = str(item.preference_description or "").strip()
        if not preference:
            return NodeResult.error(
                "PREFERENCE_RESULT_INVALID", "should_reorder=true 但缺少偏好描述"
            )
        config = config or self._config_for(item.spec.brand)
        brand = str(item.spec.brand or "").strip().lower()
        query, derived_gender = split_query_gender(item.spec.query, brand=brand)
        gender = None
        if brand == "atlas":
            gender = str(item.gender or derived_gender or "").strip() or None
        started = time.perf_counter()
        try:
            response, selected, filtered_low_stock_ids, pages_requested, page_requests = self._rerank_with_stock_filter(
                item,
                query,
                preference,
                gender,
                config,
            )
        except VectorReorderError as exc:
            outcome = NodeResult.error(exc.error_code, exc.message)
            outcome.result = {
                "should_reorder": True,
                "preference_description": preference,
                "preference_audit": dict(item.preference_audit or {}),
            }
            outcome.telemetry = {
                "executionSeconds": round(max(0.0, time.perf_counter() - started), 6),
                "vectorRetryAttempts": exc.attempts,
            }
            return outcome
        except Exception as exc:
            outcome = NodeResult.error("VECTOR_REORDER_FAILED", _error_text(exc))
            outcome.result = {
                "should_reorder": True,
                "preference_description": preference,
                "preference_audit": dict(item.preference_audit or {}),
            }
            outcome.telemetry = {
                "executionSeconds": round(max(0.0, time.perf_counter() - started), 6),
            }
            return outcome
        result = {
            "should_reorder": True,
            "preference_description": preference,
            "request": dict(page_requests[0] if page_requests else response.request_payload),
            "products": [dict(product) for product in selected],
            "product_ids": [product["product_id"] for product in selected],
            "selected_count": len(selected),
            "requested_page_size": config.product_limit,
            "total": response.total,
            "page_num": response.page_num,
            "page_size": response.page_size,
            "filtered_low_stock_product_ids": filtered_low_stock_ids,
            "pages_requested": pages_requested,
            "page_requests": page_requests,
        }
        if item.preference_audit:
            result["preference_audit"] = dict(item.preference_audit)
        outcome = NodeResult.executed(
            result,
            action=build_query_reorder_action(item.spec, result),
        )
        outcome.telemetry = {
            "executionSeconds": round(max(0.0, time.perf_counter() - started), 6),
            "vectorRetryAttempts": response.attempts,
        }
        return outcome

    def _rerank_with_stock_filter(
        self,
        item: VectorReorderInput,
        query: str,
        preference: str,
        gender: str | None,
        config: VectorReorderConfig,
    ) -> tuple[VectorReorderResponse, list[dict[str, Any]], list[str], int, list[Mapping[str, Any]]]:
        threshold = low_stock_threshold(item.spec.brand)
        selected: list[dict[str, Any]] = []
        filtered_ids: list[str] = []
        seen_ids: set[str] = set()
        page_num = config.page_num
        last_response: VectorReorderResponse | None = None
        pages_requested = 0
        page_requests: list[Mapping[str, Any]] = []
        while len(selected) < config.product_limit and pages_requested < MAX_STOCK_FILTER_PAGES:
            response = self.client.rerank(
                config.api_alias or item.spec.brand,
                query,
                preference,
                gender=gender,
                page_size=config.product_limit,
                page_num=page_num,
                endpoint_url=config.endpoint_url,
                timeout_seconds=config.timeout_seconds,
                retry_attempts=config.retry_attempts,
                retry_backoff_seconds=config.retry_backoff_seconds,
            )
            last_response = response
            page_requests.append(dict(response.request_payload))
            pages_requested += 1
            new_ids = 0
            for product in response.items:
                product_id = str(product.get("product_id") or "").strip().upper()
                if not product_id or product_id in seen_ids:
                    continue
                seen_ids.add(product_id)
                new_ids += 1
                stock = _stock_value(product)
                if stock is not None and stock <= threshold:
                    filtered_ids.append(str(product.get("product_id") or product_id))
                    continue
                candidate = dict(product)
                candidate["source_page_num"] = page_num
                selected.append(candidate)
                if len(selected) >= config.product_limit:
                    break
            if len(selected) >= config.product_limit:
                break
            if new_ids == 0 or len(response.items) < config.product_limit:
                break
            page_num += 1
        if last_response is None or not selected:
            raise VectorReorderError(
                "VECTOR_REORDER_NO_PRODUCTS", "过滤低库存后没有可用商品"
            )
        return last_response, selected, filtered_ids, pages_requested, page_requests

    def _config_for(self, brand: str) -> VectorReorderConfig:
        if self.config_provider is None:
            return self.config
        values = self.config_provider(brand) or {}
        return VectorReorderConfig.from_mapping(
            {
                "endpoint_url": self.config.endpoint_url,
                "timeout_seconds": self.config.timeout_seconds,
                "product_limit": self.config.product_limit,
                "concurrency": self.config.concurrency,
                "page_num": self.config.page_num,
                **dict(values),
            }
        )

    @staticmethod
    def _validate_inputs(items: Sequence[VectorReorderInput]) -> None:
        seen: set[str] = set()
        for item in items:
            query_id = str(item.spec.query_id).strip()
            if not query_id:
                raise ValueError("query_id is required")
            if query_id in seen:
                raise ValueError(f"duplicate query_id: {query_id}")
            seen.add(query_id)
            if (
                not str(item.spec.brand).strip()
                or not str(item.spec.query).strip()
            ):
                raise ValueError("brand and query are required")
            if not isinstance(item.should_reorder, bool):
                raise ValueError("should_reorder must be boolean")


class VectorReorderNodeRunner:
    """Adapt vector reordering to the deterministic NodeRunner port."""

    def __init__(
        self,
        *,
        client: VectorReorderClient,
        config: VectorReorderConfig | None = None,
        config_provider: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> None:
        self.batch_runner = VectorReorderBatchRunner(
            client=client,
            config=config,
            config_provider=config_provider,
        )

    def run(self, spec: QuerySpec, context: Mapping[str, Any]) -> NodeResult:
        item = self.build_input(spec, context)
        if isinstance(item, NodeResult):
            return item
        try:
            return self.batch_runner.run_one(item)
        except VectorReorderError as exc:
            return NodeResult.error(exc.error_code, exc.message)
        except Exception as exc:
            return NodeResult.error("VECTOR_REORDER_FAILED", _error_text(exc))

    def run_batch(
        self,
        items: Sequence[tuple[QuerySpec, Mapping[str, Any]]],
    ) -> dict[str, NodeResult]:
        """Run independent Query requests through the configured batch gate."""
        inputs: list[VectorReorderInput] = []
        results: dict[str, NodeResult] = {}
        for spec, context in items:
            item = self.build_input(spec, context)
            if isinstance(item, NodeResult):
                results[spec.query_id] = item
            else:
                inputs.append(item)
        if inputs:
            results.update(self.batch_runner.run_batch(inputs))
        return {spec.query_id: results[spec.query_id] for spec, _ in items}

    def build_input(
        self, spec: QuerySpec, context: Mapping[str, Any]
    ) -> VectorReorderInput | NodeResult:
        preference = context.get("preference_result")
        if preference is None:
            preferences = context.get("preference_results")
            if isinstance(preferences, Mapping):
                preference = preferences.get(spec.query_id)
        if isinstance(preference, NodeResult):
            if preference.status is NodeStatus.SKIPPED:
                reason = (
                    preference.result.get("reason")
                    if isinstance(preference.result, Mapping)
                    else None
                )
                return NodeResult.skipped(str(reason or "未满足重排序执行条件"))
            if preference.status is NodeStatus.ERROR:
                return NodeResult.error(
                    preference.error_code or "PREFERENCE_MODEL_FAILED",
                    preference.error_message or "偏好生成失败",
                )
            preference = preference.result
        if not isinstance(preference, Mapping):
            return NodeResult.error(
                "PREFERENCE_RESULT_UNAVAILABLE", "当前 Query 缺少偏好生成结果"
            )
        should_reorder = preference.get("should_reorder")
        if not isinstance(should_reorder, bool):
            return NodeResult.error(
                "PREFERENCE_RESULT_INVALID", "偏好结果缺少有效 should_reorder"
            )
        item = VectorReorderInput(
            spec=spec,
            should_reorder=should_reorder,
            preference_description=(
                str(preference.get("preference_description") or "").strip()
                if should_reorder
                else None
            ),
            reason=(str(preference.get("reason") or "").strip() or None),
            gender=(str(context.get("gender") or "").strip() or None),
            preference_audit=(
                dict(preference.get("audit"))
                if isinstance(preference.get("audit"), Mapping)
                else None
            ),
        )
        return item


def split_query_gender(
    query: str, *, brand: str | None = None
) -> tuple[str, str | None]:
    """Split a trailing gender token only for Atlas vector requests."""

    normalized_brand = str(brand or "").strip().lower()
    normalized_query = str(query or "").strip()
    if normalized_brand != "atlas":
        return normalized_query, None
    parts = normalized_query.split()
    if len(parts) <= 1:
        return normalized_query, None
    suffix = _GENDER_SUFFIXES.get(parts[-1])
    if suffix is None:
        return " ".join(parts), None
    base_query = " ".join(parts[:-1]).strip()
    return (base_query or " ".join(parts), suffix)


def _first_text(value: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text:
            return text
    return None


def low_stock_threshold(brand: str) -> float:
    """Return the configured low-stock demotion boundary for one brand."""

    return 5.0 if str(brand or "").strip().lower() == "atlas" else 0.0


def _stock_value(product: Mapping[str, Any]) -> float | None:
    for key in ("stock", "inventory", "stockQuantity", "inventoryQuantity"):
        value = product.get(key)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _error_text(exc: Exception) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


__all__ = [
    "DEFAULT_VECTOR_REORDER_ENDPOINT",
    "DEFAULT_VECTOR_REORDER_RETRY_ATTEMPTS",
    "DEFAULT_VECTOR_REORDER_RETRY_BACKOFF_SECONDS",
    "VectorReorderBatchRunner",
    "VectorReorderClient",
    "VectorReorderConfig",
    "VectorReorderError",
    "VectorReorderInput",
    "VectorReorderNodeRunner",
    "VectorReorderResponse",
    "split_query_gender",
]
