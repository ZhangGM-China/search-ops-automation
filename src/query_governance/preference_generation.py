"""Batch Query preference generation for the deterministic reorder stage.

The runner owns batching, validation and failure isolation.  It deliberately
does not select Queries or write ranking actions; those responsibilities stay
with the selection and reorder stages.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import json
from typing import Any, Protocol
from uuid import uuid4

from .models import NodeResult, NodeStatus, QuerySpec


class PreferenceGenerationRuntime(Protocol):
    def invoke(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> Any:
        ...


@dataclass(frozen=True)
class PreferenceGenerationConfig:
    """Limits captured when a reorder stage starts."""

    batch_query_limit: int = 10
    concurrency: int = 4
    token_limit: int = 12_000

    def __post_init__(self) -> None:
        for name in ("batch_query_limit", "concurrency", "token_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "PreferenceGenerationConfig":
        values = values or {}
        return cls(
            batch_query_limit=values.get(
                "query_governance_preference_generation_batch_query_limit",
                cls.batch_query_limit,
            ),
            concurrency=values.get(
                "query_governance_preference_generation_concurrency",
                cls.concurrency,
            ),
            token_limit=values.get(
                "query_governance_preference_generation_token_limit",
                cls.token_limit,
            ),
        )


@dataclass(frozen=True)
class PreferenceGenerationInput:
    spec: QuerySpec
    shared_context: Mapping[str, Any] = field(default_factory=dict)
    brand_context: Mapping[str, Any] = field(default_factory=dict)
    products: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    requested_data_date: str | None = None
    evidence_data_date: str | None = None
    evidence_date_fallback_reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "query_id": self.spec.query_id,
            "brand": self.spec.brand,
            "query": self.spec.query,
            "shared_context": dict(self.shared_context),
            "brand_context": dict(self.brand_context),
            "products": [dict(product) for product in self.products],
            "behavior_evidence": {
                "requested_data_date": self.requested_data_date,
                "evidence_data_date": self.evidence_data_date,
                "fallback_reason": self.evidence_date_fallback_reason,
            },
        }


class PreferenceGenerationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class PreferenceGenerationRunner:
    """Generate preferences for eligible Queries using dynamic full-Query batches."""

    _SYSTEM_PROMPT = (
        "你是电商搜索 Query 治理流程中的偏好策略生成 Agent。\n"
        "【业务场景】系统已通过搜索行为指标和治理规则确认这些 Query 需要排序重排。"
        "你的任务是结合 Query、品牌上下文、用户行为证据、商品属性和当前场景，"
        "生成供排序模型使用的商品偏好策略；不判断是否治理，不执行排序、写入或其他动作。\n"
        "【规则】只使用输入事实，不得臆测；不重复 Query 已明确表达的条件；"
        "优先提炼能区分商品的颜色、材质、版型、风格、功能和季节适配性；"
        "天气、季节、节日只能辅助，不得覆盖 Query 意图；每个 Query 只生成一条简洁中文策略。\n"
        "【输出】只输出 JSON：{\"brand\":\"品牌\",\"strategies\":{\"Query\":\"偏好策略\"}}。"
        "strategies 的键必须与输入 Query 完全一致，不输出其他字段。"
    )

    def __init__(
        self,
        *,
        runtime: PreferenceGenerationRuntime,
        config: PreferenceGenerationConfig | None = None,
    ) -> None:
        self.runtime = runtime
        self.config = config or PreferenceGenerationConfig()

    def run(
        self,
        items: Sequence[PreferenceGenerationInput],
        *,
        callbacks: Sequence[Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        batch_config: Mapping[str, Any] | None = None,
    ) -> dict[str, NodeResult]:
        inputs = list(items)
        self._validate_inputs(inputs)
        results: dict[str, NodeResult] = {}
        eligible: list[PreferenceGenerationInput] = []
        for item in inputs:
            if item.spec.reorder_eligible is False:
                results[item.spec.query_id] = NodeResult.skipped(
                    item.spec.reorder_skip_reason or "CTR/CVR 正常"
                )
            else:
                eligible.append(item)

        batches = self._pack(eligible)
        if not batches:
            return results
        invoke_kwargs = self._invoke_kwargs(
            callbacks=callbacks, metadata=metadata, batch_config=batch_config
        )
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as pool:
            futures = {
                pool.submit(self._run_batch, batch, invoke_kwargs): batch
                for batch in batches
            }
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    results.update(future.result())
                except Exception as exc:
                    for item in batch:
                        results[item.spec.query_id] = NodeResult.error(
                            "PREFERENCE_MODEL_FAILED", self._error_text(exc)
                        )
        return {item.spec.query_id: results[item.spec.query_id] for item in inputs}

    def _pack(
        self, items: Sequence[PreferenceGenerationInput]
    ) -> list[tuple[PreferenceGenerationInput, ...]]:
        batches: list[tuple[PreferenceGenerationInput, ...]] = []
        for brand in dict.fromkeys(str(item.spec.brand).strip() for item in items):
            brand_items = [item for item in items if str(item.spec.brand).strip() == brand]
            current: list[PreferenceGenerationInput] = []
            current_tokens = 0
            for item in brand_items:
                item_tokens = self._estimate_tokens(item.as_payload())
                exceeds_count = len(current) >= self.config.batch_query_limit
                exceeds_tokens = bool(current) and current_tokens + item_tokens > self.config.token_limit
                if current and (exceeds_count or exceeds_tokens):
                    batches.append(tuple(current))
                    current = []
                    current_tokens = 0
                current.append(item)
                current_tokens += item_tokens
                if item_tokens > self.config.token_limit:
                    batches.append(tuple(current))
                    current = []
                    current_tokens = 0
            if current:
                batches.append(tuple(current))
        return batches

    def _run_batch(
        self,
        batch: tuple[PreferenceGenerationInput, ...],
        invoke_kwargs: Mapping[str, Any],
    ) -> dict[str, NodeResult]:
        try:
            payload, audit = self._invoke(batch, invoke_kwargs)
            return self._normalize(payload, batch, audit=audit)
        except PreferenceGenerationError as exc:
            # A malformed or failed mixed request is isolated by retrying each
            # complete Query group once. Other Queries retain their result.
            if len(batch) > 1:
                isolated: dict[str, NodeResult] = {}
                for item in batch:
                    try:
                        payload, audit = self._invoke((item,), invoke_kwargs)
                        isolated.update(
                            self._normalize(payload, (item,), audit=audit)
                        )
                    except PreferenceGenerationError as retry_exc:
                        isolated[item.spec.query_id] = NodeResult.error(
                            retry_exc.error_code, retry_exc.message
                        )
                    except Exception as retry_exc:
                        isolated[item.spec.query_id] = NodeResult.error(
                            "PREFERENCE_MODEL_FAILED", self._error_text(retry_exc)
                        )
                return isolated
            query_id = batch[0].spec.query_id
            return {query_id: NodeResult.error(exc.error_code, exc.message)}
        except Exception as exc:
            return {
                item.spec.query_id: NodeResult.error(
                    "PREFERENCE_MODEL_FAILED", self._error_text(exc)
                )
                for item in batch
            }

    def _invoke(
        self,
        batch: tuple[PreferenceGenerationInput, ...],
        invoke_kwargs: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
        request = self._build_request(batch)
        messages = [
            {"role": "system", "content": self._SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ]
        invocation = self.runtime.invoke(messages, **dict(invoke_kwargs))
        audit = getattr(invocation, "audit", None)
        if isinstance(audit, Mapping):
            audit = {
                **dict(audit),
                "batchKey": f"preference:{uuid4().hex}",
                "batchQueryCount": len(batch),
            }
        response = getattr(invocation, "response", invocation)
        content = getattr(response, "content", response)
        if not isinstance(content, str):
            raise PreferenceGenerationError(
                "PREFERENCE_OUTPUT_INVALID", "model response content must be text"
            )
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PreferenceGenerationError(
                "PREFERENCE_OUTPUT_INVALID", "model response is not valid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise PreferenceGenerationError(
                "PREFERENCE_OUTPUT_INVALID", "model response must be an object"
            )
        return payload, dict(audit) if isinstance(audit, Mapping) else None

    def _normalize(
        self,
        payload: Mapping[str, Any],
        batch: tuple[PreferenceGenerationInput, ...],
        audit: Mapping[str, Any] | None = None,
    ) -> dict[str, NodeResult]:
        expected = {item.spec.query_id: item for item in batch}
        strategies = payload.get("strategies")
        if isinstance(strategies, Mapping):
            brand = str(payload.get("brand") or "").strip()
            if brand and brand.lower() != str(batch[0].spec.brand).strip().lower():
                raise PreferenceGenerationError("PREFERENCE_OUTPUT_INVALID", "brand mismatch")
            normalized: dict[str, NodeResult] = {}
            for item in batch:
                value = strategies.get(item.spec.query)
                if not isinstance(value, str) or not value.strip():
                    raise PreferenceGenerationError("PREFERENCE_OUTPUT_INVALID", "strategies missing a Query")
                result = {
                    "query_id": item.spec.query_id,
                    "should_reorder": True,
                    "preference_description": value.strip(),
                    "reason": None,
                }
                if audit:
                    result["audit"] = dict(audit)
                normalized[item.spec.query_id] = NodeResult.executed(result)
            return normalized

        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise PreferenceGenerationError(
                "PREFERENCE_OUTPUT_INVALID", "strategies must be an object"
            )
        by_id: dict[str, Mapping[str, Any]] = {}
        for raw in raw_results:
            if not isinstance(raw, Mapping):
                raise PreferenceGenerationError(
                    "PREFERENCE_OUTPUT_INVALID", "each result must be an object"
                )
            query_id = str(raw.get("query_id") or "").strip()
            if not query_id:
                query = str(raw.get("query") or "").strip()
                matches = [item.spec.query_id for item in batch if item.spec.query == query]
                query_id = matches[0] if len(matches) == 1 else ""
            if query_id not in expected or query_id in by_id:
                raise PreferenceGenerationError(
                    "PREFERENCE_OUTPUT_INVALID", "results contain unknown or duplicate Query"
                )
            by_id[query_id] = raw
        if set(by_id) != set(expected):
            raise PreferenceGenerationError(
                "PREFERENCE_OUTPUT_INVALID", "results are missing a Query"
            )
        normalized: dict[str, NodeResult] = {}
        for query_id, raw in by_id.items():
            preference = raw.get("preference_description")
            reason = raw.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise PreferenceGenerationError(
                    "PREFERENCE_OUTPUT_INVALID", "reason must be text or null"
                )
            if not isinstance(preference, str):
                raise PreferenceGenerationError(
                    "PREFERENCE_OUTPUT_INVALID", "preference_description is required"
                )
            # Whether to run reorder is decided by deterministic eligibility
            # gates before this Agent is called; the Agent always supplies a
            # strategy for an eligible Query.
            should_reorder = True
            result = {
                "query_id": query_id,
                "should_reorder": should_reorder,
                "preference_description": preference.strip() if isinstance(preference, str) else None,
                "reason": reason.strip() if isinstance(reason, str) else None,
            }
            if not result["preference_description"]:
                raise PreferenceGenerationError(
                    "PREFERENCE_OUTPUT_INVALID", "preference_description is empty"
                )
            warnings: list[str] = []
            if self._estimate_tokens(expected[query_id].as_payload()) > self.config.token_limit:
                warnings.append("preference_batch_token_limit_exceeded")
            if warnings:
                result["warnings"] = warnings
            if audit:
                result["audit"] = dict(audit)
            normalized[query_id] = NodeResult.executed(result)
        return normalized

    @classmethod
    def _build_request(cls, batch: tuple[PreferenceGenerationInput, ...]) -> dict[str, Any]:
        first = batch[0]
        global_facts = dict(first.shared_context or {})
        brand_context = dict(first.brand_context or {})
        weather: list[dict[str, Any]] = []
        for city in global_facts.get("weather_cities") or []:
            if isinstance(city, Mapping):
                weather.append({
                    "city": city.get("city"),
                    "weather": cls._zh_weather(city.get("weather") or city.get("weather_state")),
                    "temperature": cls._zh_temperature(city.get("temperature") or city.get("temperature_level")),
                })
        if not weather:
            states = (global_facts.get("weather") or {}).get("states") or []
            weather = [
                {"weather": cls._zh_weather(state), "temperature": cls._zh_temperature(global_facts.get("temperature"))}
                for state in states
            ]
        queries: dict[str, list[dict[str, Any]]] = {}
        for item in batch:
            # ``products`` already contains the merged high-click/high-purchase
            # evidence; preserve its ranking order without a second truncation.
            products: list[dict[str, Any]] = []
            for rank, raw in enumerate(item.products, 1):
                if not isinstance(raw, Mapping):
                    continue
                product: dict[str, Any] = {"rank": rank, "signal": cls._signal(raw)}
                for key in ("product_id", "product_name", "description", "category", "gender", "style", "color", "material", "size", "design", "tags"):
                    if raw.get(key) not in (None, ""):
                        product[key] = raw[key]
                products.append(product)
            queries[item.spec.query] = products
        return {
            "brand": str(first.spec.brand),
            "scene": {
                "date": first.requested_data_date or first.evidence_data_date,
                "season": cls._zh_season(global_facts.get("season")),
                "weather": weather,
                "holiday": cls._holiday(global_facts.get("festival")),
            },
            "brand_context": {
                "brand_info": brand_context.get("brand_info") or {},
                "social_trends": [
                    {"title": x.get("title"), "description": x.get("description")}
                    for x in brand_context.get("social_trends") or []
                    if isinstance(x, Mapping)
                ],
            },
            "queries": queries,
        }

    @staticmethod
    def _signal(raw: Mapping[str, Any]) -> str:
        click = raw.get("click_rank") is not None
        purchase = raw.get("purchase_rank") is not None
        return "高点击+高购买" if click and purchase else ("高点击" if click else "高购买")

    @staticmethod
    def _zh_season(value: Any) -> Any:
        return {"spring": "春季", "summer": "夏季", "autumn": "秋季", "fall": "秋季", "winter": "冬季"}.get(str(value).lower(), value)

    @staticmethod
    def _zh_weather(value: Any) -> Any:
        return {"clear": "晴", "cloudy": "多云", "rain": "雨", "storm": "雷雨", "snow": "雪", "haze": "雾霾", "fog": "雾"}.get(str(value).lower(), value)

    @staticmethod
    def _zh_temperature(value: Any) -> Any:
        return {"high": "高温", "normal": "常温", "cold": "低温"}.get(str(value).lower(), value)

    @staticmethod
    def _holiday(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        kind = {"holiday": "法定节假日", "workday": "工作日", "normal": "周末" if value.get("is_holiday") else "工作日"}.get(value.get("type"), value.get("type"))
        return {"date": value.get("date"), "name": value.get("name") or "", "type": kind}

    @staticmethod
    def _estimate_tokens(payload: Mapping[str, Any]) -> int:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return max(1, (len(encoded) + 3) // 4)

    @staticmethod
    def _validate_inputs(items: Sequence[PreferenceGenerationInput]) -> None:
        seen: set[str] = set()
        for item in items:
            query_id = str(item.spec.query_id).strip()
            if not query_id:
                raise ValueError("query_id is required")
            if query_id in seen:
                raise ValueError(f"duplicate query_id: {query_id}")
            seen.add(query_id)
            if not str(item.spec.brand).strip() or not str(item.spec.query).strip():
                raise ValueError("brand and query are required")

    @staticmethod
    def _invoke_kwargs(
        *,
        callbacks: Sequence[Any] | None,
        metadata: Mapping[str, Any] | None,
        batch_config: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if batch_config:
            kwargs.update(dict(batch_config))
        if callbacks is not None:
            kwargs["callbacks"] = list(callbacks)
        if metadata:
            merged = dict(kwargs.get("metadata") or {})
            merged.update(metadata)
            kwargs["metadata"] = merged
        return kwargs

    @staticmethod
    def _error_text(exc: Exception) -> str:
        return str(exc).strip() or exc.__class__.__name__


__all__ = [
    "PreferenceGenerationConfig",
    "PreferenceGenerationError",
    "PreferenceGenerationInput",
    "PreferenceGenerationRunner",
    "PreferenceGenerationRuntime",
]

# Keep the domain name available to callers that treat this component as an
# Agent, while the Runner name makes its batch orchestration responsibility
# explicit.
PreferenceGenerationAgent = PreferenceGenerationRunner
__all__.append("PreferenceGenerationAgent")
