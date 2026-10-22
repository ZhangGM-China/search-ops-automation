#!/usr/bin/env python3
"""Benchmark the Query preference-generation Agent in isolation.

This script is read-only. It accepts a frozen, behavior-SQL-backed fixture and
does not call Mixed, vector ranking, ranking writes, or governance actions.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env", override=False)

DEFAULT_MODEL = "qwen3.6-plus"
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_COMPLETION_TOKENS = 4096
DEFAULT_BATCH_QUERY_LIMIT = 10
DEFAULT_TOKEN_LIMIT = 60_000
DEFAULT_CONCURRENCY = 1

SOCIAL_NOTES_SQL = """
SELECT note_title AS title, note_description AS description
FROM industry_knowledge_base.xhs_note_snapshots
WHERE LOWER(brand_name) = LOWER(:brand)
  AND crawled_date = :snapshot_date
ORDER BY overall_score DESC, engagement_count DESC
"""

SYSTEM_PROMPT = """你是电商搜索 Query 治理流程中的偏好策略生成 Agent。
系统已经根据搜索行为指标和治理规则确认输入 Query 需要排序重排。你的任务是结合品牌、Query、商品行为证据、商品属性和当前场景，生成供排序模型使用的商品偏好策略；不判断治理资格，不执行排序、写入或其他治理动作。
只使用输入事实，不得臆测；不要重复 Query 已明确表达的条件；优先提炼可区分商品的颜色、材质、版型、风格、功能和季节适配性。天气、季节、节日只能辅助，不得覆盖 Query 意图。每个 Query 只生成一条简洁中文策略。
只输出 JSON，不输出 Markdown 或解释文字：
{"brand":"品牌","strategies":{"Query":"偏好策略"}}
strategies 的键必须与输入 Query 完全一致，不输出其他字段。"""


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _row_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    if isinstance(row, dict):
        return dict(row)
    return dict(row)


def fetch_social_posts(
    engine: Any,
    *,
    brand: str,
    snapshot_date: str,
) -> list[dict[str, Any]] | None:
    """Read every raw hot note for the brand/date; no comments or normalization."""
    from sqlalchemy import text

    with engine.connect() as connection:
        notes_result = connection.execute(
            text(SOCIAL_NOTES_SQL),
            {"brand": brand, "snapshot_date": snapshot_date},
        )
        notes = [_row_dict(row) for row in notes_result]
        if not notes:
            return None
    return [dict(note) for note in notes]


def attach_social_posts(queries: list[dict[str, Any]], social_posts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Attach title/description-only posts to each fixture for brand-level assembly."""
    updated: list[dict[str, Any]] = []
    for query in queries:
        item = dict(query)
        brand_context = dict(item.get("brand_context") or {})
        brand_context["social_trends"] = social_posts
        item["brand_context"] = brand_context
        updated.append(item)
    return updated


def extract_usage(response: Any) -> dict[str, int | bool | None]:
    usage = getattr(response, "usage_metadata", None)
    metadata = getattr(response, "response_metadata", None)
    if not isinstance(usage, dict) and isinstance(metadata, dict):
        usage = metadata.get("token_usage") or metadata.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def integer(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    input_tokens = integer("input_tokens", "prompt_tokens")
    output_tokens = integer("output_tokens", "completion_tokens")
    total_tokens = integer("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "reported": bool(usage),
    }


def estimate_query_tokens(query: dict[str, Any]) -> int:
    """Conservative preflight estimate; actual API usage remains authoritative."""
    payload = _json(query)
    return max(1, (len(payload.encode("utf-8")) + 1) // 2)


def pack_queries(
    queries: list[dict[str, Any]],
    *,
    batch_query_limit: int,
    token_limit: int,
) -> list[list[dict[str, Any]]]:
    if batch_query_limit < 1 or token_limit < 1:
        raise ValueError("batch_query_limit_and_token_limit_must_be_positive")
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for query in queries:
        query_tokens = estimate_query_tokens(query)
        would_exceed = current and (
            len(current) >= batch_query_limit or current_tokens + query_tokens > token_limit
        )
        if would_exceed:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(query)
        current_tokens += query_tokens
    if current:
        batches.append(current)
    return batches


def _normalise_query(saved: dict[str, Any], brand: str) -> dict[str, Any]:
    query = str(saved.get("query") or "").strip()
    if not query:
        raise ValueError("fixture_query_is_empty")
    products = saved.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError(f"fixture_products_missing:{query}")
    normalised_products: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict) or not str(product.get("product_id") or "").strip():
            raise ValueError(f"fixture_product_id_missing:{query}")
        normalised_products.append(dict(product))
    result = dict(saved)
    result["brand"] = str(saved.get("brand") or brand)
    result["query"] = query
    result["products"] = normalised_products
    return result


def load_fixture(path: Path, *, brand: str, query_limit: int | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("queries"), list):
        raise ValueError("fixture_must_contain_queries_array")
    fixture_brand = str(payload.get("brand") or brand)
    root_shared_context = payload.get("shared_context")
    root_brand_context = payload.get("brand_context")
    queries = []
    for saved in payload["queries"]:
        query_item = dict(saved)
        if root_shared_context is not None and "shared_context" not in query_item:
            query_item["shared_context"] = root_shared_context
        if root_brand_context is not None and "brand_context" not in query_item:
            query_item["brand_context"] = root_brand_context
        queries.append(_normalise_query(query_item, fixture_brand))
    seen: set[str] = set()
    for item in queries:
        if item["query"] in seen:
            raise ValueError(f"duplicate_fixture_query:{item['query']}")
        seen.add(item["query"])
    if query_limit is not None:
        queries = queries[:query_limit]
    eligible = [item for item in queries if bool(item.get("ranking_eligible", True))]
    skipped = [item for item in queries if not bool(item.get("ranking_eligible", True))]
    metadata = {
        "brand": fixture_brand,
        "fixture_path": str(path),
        "fixture_version": payload.get("fixture_version"),
        "evidence_source": payload.get("evidence_source"),
        "query_count": len(queries),
        "eligible_query_count": len(eligible),
        "skipped_query_count": len(skipped),
        "skipped_queries": [{"query": item["query"], "reason": "CTR/CVR 正常"} for item in skipped],
    }
    return metadata, eligible


def build_messages(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not queries:
        raise ValueError("cannot_build_empty_batch")
    brand = str(queries[0].get("brand") or "")
    shared_context = queries[0].get("shared_context") or {}
    brand_context = dict(queries[0].get("brand_context") or {})
    social_trends = brand_context.get("social_trends")
    if social_trends is None:
        social_trends = brand_context.get("social_posts")
    trends = []
    for item in social_trends or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("note_title")
        description = item.get("description") or item.get("note_description")
        if title or description:
            trends.append({"title": title, "description": description})
    weather = (shared_context.get("weather") or {}).get("cities") or []
    scene = {
        "date": shared_context.get("date") or shared_context.get("data_date"),
        "season": _zh_season(shared_context.get("season")),
        "weather": [
            {
                "city": item.get("city"),
                "weather": _zh_weather(item.get("weather") or item.get("weather_state")),
                "temperature": _zh_temperature(item.get("temperature") or item.get("temperature_level")),
            }
            for item in weather if isinstance(item, dict)
        ],
        "holiday": _holiday(shared_context.get("festival") or shared_context.get("holiday")),
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for query in queries:
        # The fixture is expected to contain the merged 8+8 evidence set;
        # benchmark assembly must not trim it again.
        products = []
        for rank, raw in enumerate(query["products"], 1):
            product = {"rank": rank, "signal": _product_signal(raw)}
            for key in ("product_id", "product_name", "description", "category", "gender", "style", "color", "material", "size", "design", "tags"):
                if raw.get(key) not in (None, ""):
                    product[key] = raw[key]
            products.append(product)
        groups[query["query"]] = products
    user_payload = {
        "brand": brand,
        "scene": scene,
        "brand_context": {
            "brand_info": brand_context.get("brand_info") or {},
            "social_trends": trends,
        },
        "queries": groups,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _json(user_payload)},
    ]


def _zh_season(value: Any) -> Any:
    return {"spring": "春季", "summer": "夏季", "autumn": "秋季", "fall": "秋季", "winter": "冬季"}.get(str(value).lower(), value)


def _zh_weather(value: Any) -> Any:
    return {"clear": "晴", "cloudy": "多云", "rain": "雨", "storm": "雷雨", "snow": "雪", "haze": "雾霾", "fog": "雾"}.get(str(value).lower(), value)


def _zh_temperature(value: Any) -> Any:
    return {"high": "高温", "normal": "常温", "cold": "低温"}.get(str(value).lower(), value)


def _holiday(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    kind = {"holiday": "法定节假日", "workday": "工作日", "normal": "周末" if value.get("is_holiday") else "工作日"}.get(value.get("type"), value.get("type"))
    return {"date": value.get("date"), "name": value.get("name") or "", "type": kind}


def _product_signal(product: dict[str, Any]) -> str:
    click = product.get("click_rank") is not None
    purchase = product.get("purchase_rank") is not None
    return "高点击+高购买" if click and purchase else ("高点击" if click else "高购买")


def _parse_json(raw: str) -> Any:
    text = str(raw or "").strip()
    candidates = [text, re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    return None


def validate_output(payload: Any, expected_queries: list[dict[str, Any]]) -> ValidationResult:
    if isinstance(payload, dict) and isinstance(payload.get("strategies"), dict):
        expected = [item["query"] for item in expected_queries]
        brand = str(payload.get("brand") or "").strip()
        expected_brand = str(expected_queries[0].get("brand") or "").strip() if expected_queries else ""
        errors: list[str] = []
        if brand and expected_brand and brand.lower() != expected_brand.lower():
            errors.append("brand_mismatch")
        strategies = payload["strategies"]
        if set(strategies) != set(expected):
            errors.append("missing_query")
        validated = []
        for query in expected:
            value = strategies.get(query)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"strategy_missing:{query}")
            validated.append({"query": query, "strategy": value})
        return ValidationResult(not errors, sorted(set(errors)), validated)

    groups = payload.get("results") if isinstance(payload, dict) else None
    errors: list[str] = []
    if not isinstance(groups, list):
        return ValidationResult(False, ["results_not_list"])
    expected = [item["query"] for item in expected_queries]
    expected_set = set(expected)
    returned = [item.get("query") for item in groups if isinstance(item, dict)]
    if len(returned) != len(groups):
        errors.append("result_not_object")
    if len(returned) != len(set(returned)):
        errors.append("duplicate_query")
    if any(query not in expected_set for query in returned):
        errors.append("unknown_query")
    if set(returned) != expected_set:
        errors.append("missing_query")
    if returned != expected:
        errors.append("query_order_mismatch")
    validated: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        should = group.get("should_reorder")
        description = group.get("preference_description")
        reason = group.get("reason")
        if not isinstance(should, bool):
            errors.append(f"should_reorder_not_bool:{group.get('query')}")
        if should is True and not isinstance(description, str):
            errors.append(f"preference_description_missing:{group.get('query')}")
        if should is True and isinstance(description, str) and not description.strip():
            errors.append(f"preference_description_empty:{group.get('query')}")
        if should is False and description not in (None, ""):
            errors.append(f"false_query_has_preference:{group.get('query')}")
        if reason is not None and not isinstance(reason, str):
            errors.append(f"reason_not_string:{group.get('query')}")
        validated.append({
            "query": group.get("query"),
            "should_reorder": should,
            "preference_description": description,
            "reason": reason,
        })
    return ValidationResult(not errors, sorted(set(errors)), validated)


def _build_llm(model_name: str, timeout: int, max_completion_tokens: int | None) -> Any:
    from src.llm.rd_client import build_rd_chat_model

    llm = build_rd_chat_model(
        timeout=timeout,
        default_model=model_name,
        temperature=0.1,
        max_retries=0,
        max_completion_tokens=max_completion_tokens,
    )
    if llm is None:
        raise RuntimeError("RD_LLM_API_KEY_is_not_configured")
    return llm


def _build_social_engine(db_url: str | None) -> Any:
    url = str(db_url or os.getenv("SOCIAL_DB_URL") or "").strip()
    if not url:
        raise RuntimeError("social_db_url_is_not_configured")
    from sqlalchemy import create_engine

    return create_engine(url, pool_pre_ping=True)


def _langfuse_client() -> Any | None:
    try:
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY")
        if not public_key or not secret_key:
            auth = subprocess.check_output(["launchctl", "getenv", "LANGFUSE_MCP_AUTHORIZATION"], text=True).strip()
            if auth.startswith("Basic "):
                public_key, secret_key = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":", 1)
        if not public_key or not secret_key:
            return None
        from langfuse import Langfuse

        return Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=os.getenv("LANGFUSE_BASE_URL", "http://langfuse.example.test"),
            environment="test",
            release="preference-generation-benchmark-v1",
        )
    except Exception:
        return None


def _invoke(llm: Any, messages: list[dict[str, Any]], stream: bool) -> tuple[Any, str, int]:
    if not stream:
        response = llm.invoke(messages)
        return response, str(getattr(response, "content", "") or ""), 0
    response = None
    raw = ""
    chunks = 0
    for chunk in llm.stream(messages, stream_usage=True):
        chunks += 1
        response = chunk if response is None else response + chunk
        raw = str(getattr(response, "content", "") or "")
    if response is None:
        raise RuntimeError("empty_stream_response")
    return response, raw, chunks


def run_batch(
    queries: list[dict[str, Any]],
    *,
    model_name: str,
    timeout: int,
    max_completion_tokens: int | None,
    stream: bool,
    batch_index: int,
    langfuse: Any | None,
) -> dict[str, Any]:
    messages = build_messages(queries)
    trace_id = langfuse.create_trace_id(seed=f"preference-batch-{time.time_ns()}") if langfuse else None
    root = langfuse.start_observation(
        trace_context={"trace_id": trace_id},
        name=f"preference-generation-batch-{batch_index}",
        as_type="chain",
        input={"query_count": len(queries), "queries": [item["query"] for item in queries]},
        metadata={"brand": queries[0].get("brand"), "model": model_name},
    ) if langfuse else None
    generation = langfuse.start_observation(
        trace_context={"trace_id": trace_id, "parent_span_id": root.id},
        name=f"preference-generation-{batch_index}",
        as_type="generation",
        input=messages,
        model=model_name,
        model_parameters={"temperature": 0.1, "stream": stream, "max_completion_tokens": max_completion_tokens},
    ) if langfuse and root else None
    started = time.perf_counter()
    error = None
    raw = ""
    response = None
    stream_chunks = 0
    try:
        response, raw, stream_chunks = _invoke(_build_llm(model_name, timeout, max_completion_tokens), messages, stream)
    except Exception as exc:
        error = type(exc).__name__
        raw = ""
        error_message = str(exc)[:2000]
    else:
        error_message = None
    elapsed = time.perf_counter() - started
    usage = extract_usage(response)
    validation = validate_output(_parse_json(raw), queries) if not error else ValidationResult(False, ["model_call_error"])
    if generation:
        generation.update(
            output=raw if not error else {"error": error, "message": error_message},
            level="ERROR" if error else None,
            status_message=error_message,
            usage_details={"input": usage["input_tokens"] or 0, "output": usage["output_tokens"] or 0, "total": usage["total_tokens"] or 0},
            metadata={"elapsed_seconds": round(elapsed, 3), "validation": validation.__dict__},
        )
        generation.end()
    if root:
        root.update(output={"elapsed_seconds": round(elapsed, 3), "valid": validation.valid})
        root.end()
    if langfuse:
        langfuse.flush()
    return {
        "batch_index": batch_index,
        "brand": queries[0].get("brand"),
        "queries": [item["query"] for item in queries],
        "query_count": len(queries),
        "product_count": sum(len(item["products"]) for item in queries),
        "model": model_name,
        "stream": stream,
        "elapsed_seconds": round(elapsed, 4),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "usage_reported": usage["reported"],
        "stream_chunks": stream_chunks,
        "error": error,
        "error_message": error_message,
        "quality": validation.__dict__,
        "langfuse_trace_url": langfuse.get_trace_url(trace_id=trace_id) if langfuse and trace_id else None,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    metadata, queries = load_fixture(Path(args.fixture), brand=args.brand, query_limit=args.query_limit)
    social_date = getattr(args, "social_date", None)
    if social_date:
        engine = _build_social_engine(getattr(args, "social_db_url", None))
        social_posts = fetch_social_posts(
            engine,
            brand=args.brand,
            snapshot_date=social_date,
        )
        queries = attach_social_posts(queries, social_posts)
        metadata["social_posts_count"] = len(social_posts or []) if social_posts is not None else 0
        metadata["social_posts_source"] = "sql"
    else:
        metadata["social_posts_source"] = "fixture"
    batches = pack_queries(queries, batch_query_limit=args.batch_query_limit, token_limit=args.token_limit)
    langfuse = _langfuse_client() if not args.no_langfuse else None
    started = time.perf_counter()
    runner = lambda index_batch: run_batch(
        index_batch[1], model_name=args.model, timeout=args.timeout,
        max_completion_tokens=args.max_completion_tokens, stream=args.stream,
        batch_index=index_batch[0], langfuse=langfuse,
    )
    indexed = list(enumerate(batches, start=1))
    if args.concurrency == 1:
        results = [runner(pair) for pair in indexed]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(runner, indexed))
    total_tokens = sum(item["total_tokens"] or 0 for item in results)
    output = {
        "metadata": {
            **metadata,
            "model": args.model,
            "batch_query_limit": args.batch_query_limit,
            "token_limit": args.token_limit,
            "concurrency": args.concurrency,
            "stream": args.stream,
            "max_completion_tokens": args.max_completion_tokens,
            "batch_count": len(batches),
        },
        "summary": {
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            "total_input_tokens": sum(item["input_tokens"] or 0 for item in results),
            "total_output_tokens": sum(item["output_tokens"] or 0 for item in results),
            "total_tokens": total_tokens,
            "average_tokens_per_query": total_tokens / len(queries) if queries else None,
            "valid_batch_count": sum(1 for item in results if item["quality"]["valid"]),
            "error_batch_count": sum(1 for item in results if item["error"]),
        },
        "batches": results,
    }
    if args.output:
        Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, help="frozen behavior-evidence fixture JSON")
    parser.add_argument("--brand", required=True)
    parser.add_argument("--query-limit", type=int)
    parser.add_argument("--batch-query-limit", type=int, default=DEFAULT_BATCH_QUERY_LIMIT)
    parser.add_argument("--token-limit", type=int, default=DEFAULT_TOKEN_LIMIT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--model", default=os.getenv("PREFERENCE_GENERATION_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-completion-tokens", type=int, default=DEFAULT_MAX_COMPLETION_TOKENS)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--no-langfuse", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--social-db-url", help="SQLAlchemy URL for the read-only social data source")
    parser.add_argument("--social-date", help="crawled_date used to fetch raw XHS posts")
    return parser


if __name__ == "__main__":
    arguments = _parser().parse_args()
    if arguments.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    print(json.dumps(run(arguments), ensure_ascii=False, indent=2))
