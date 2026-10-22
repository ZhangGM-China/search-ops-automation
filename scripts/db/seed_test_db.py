#!/usr/bin/env python3
"""Backfill local metrics from the read-only source database.

There is intentionally no synthetic-data fallback. A source failure or an
empty source day aborts before any local rows are replaced.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text


if (os.getenv("ENV") or "test").strip().lower() != "test":
    print("blocked: seed_test_db.py only runs with ENV=test", file=sys.stderr)
    raise SystemExit(2)
os.environ["ENV"] = "test"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.integrations.query_extractor import QueryExtractor  # noqa: E402
from src.persistence import BrandDailyMetric, SessionLocal, init_db  # noqa: E402
from src.top_query.extractor import TopQueryDataExtractor  # noqa: E402


DEFAULT_DAYS = 30
DEFAULT_BRAND = "atlas"


def iter_dates(start_date: dt.date, end_date: dt.date) -> list[dt.date]:
    if end_date < start_date:
        raise ValueError("end date must be on or after start date")
    return [start_date + dt.timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _as_non_negative_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"source metric {field} is invalid") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"source metric {field} is invalid")
    return number


def _as_count(value: Any, field: str) -> int:
    number = _as_non_negative_number(value, field)
    if not number.is_integer():
        raise ValueError(f"source metric {field} must be an integer")
    return int(number)


def build_metric_record(metric: dict[str, Any], query_rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Convert source results into the new local BrandDailyMetric contract."""
    if metric.get("metric_source") != "user_behavior":
        raise ValueError("metric_source must be user_behavior")
    brand = str(metric.get("brand") or "").strip()
    data_date = str(metric.get("data_date") or "").strip()
    if not brand or not data_date:
        raise ValueError("brand and data_date are required")

    counts = {
        field: _as_count(metric.get(field), field)
        for field in ("brand_uv", "brand_pv")
    }
    if counts["brand_uv"] == 0 and counts["brand_pv"] == 0:
        raise ValueError(f"source metric is empty for {brand} {data_date}")
    counts.update({
        field: _as_count(metric.get(field), field)
        for field in ("click_uv", "add_to_cart_uv", "purchase_uv")
    })
    rates = {field: _as_non_negative_number(metric.get(field), field) for field in ("ctr", "atc", "cvr")}
    queries = list(query_rows)
    if not queries:
        raise ValueError(f"source query metrics are empty for {brand} {data_date}")
    top_query_pv = sum(_as_count(row.get("search_pv"), "search_pv") for row in queries)
    top_query_share = top_query_pv / counts["brand_pv"] if counts["brand_pv"] else 0.0
    raw_payload = {
        "metric_source": "user_behavior",
        "click_uv": counts["click_uv"],
        "add_to_cart_uv": counts["add_to_cart_uv"],
        "purchase_uv": counts["purchase_uv"],
        "purchase_amount": _as_non_negative_number(metric.get("purchase_amount"), "purchase_amount"),
        "atc": rates["atc"],
        "source_query_count": len(queries),
        "source_query_pv": top_query_pv,
    }
    return {
        "brand": brand,
        "data_date": data_date,
        **counts,
        "ctr": rates["ctr"],
        "cvr": rates["cvr"],
        "top_query_count": len(queries),
        "top_query_pv": top_query_pv,
        "top_query_pv_share": top_query_share,
        "raw_payload": raw_payload,
    }


def _source_date_column(brand: str) -> tuple[str, str]:
    if brand == "riviera":
        return "rl_daily", "date"
    return QueryExtractor._get_brand_user_behavior_table(brand), "user_date"


def discover_end_date(extractor: QueryExtractor, brand: str) -> dt.date:
    table, column = _source_date_column(brand)
    engine = getattr(extractor, "engine", None)
    if engine is None:
        engine = extractor._get_extractor_for_brand(brand).engine
    with engine.connect() as conn:
        value = conn.execute(text(f"SELECT MAX(`{column}`) FROM {table}")).scalar()
    if value is None:
        raise RuntimeError(f"source has no dates for brand {brand}")
    if isinstance(value, dt.datetime):
        value = value.date()
    elif isinstance(value, str):
        value = dt.date.fromisoformat(value[:10])
    if not isinstance(value, dt.date):
        raise RuntimeError(f"source returned an invalid latest date for brand {brand}")
    return min(value, dt.date.today() - dt.timedelta(days=1))


def _parse_date(value: str | None, field: str) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _resolve_window(extractor: QueryExtractor, brand: str, start_date: dt.date | None, end_date: dt.date | None, days: int) -> tuple[dt.date, dt.date]:
    if days < 1 or days > 366:
        raise ValueError("days must be between 1 and 366")
    resolved_end = end_date or discover_end_date(extractor, brand)
    resolved_start = start_date or resolved_end - dt.timedelta(days=days - 1)
    if resolved_end < resolved_start:
        raise ValueError("end date must be on or after start date")
    return resolved_start, resolved_end


def _replace_local_rows(records: list[dict[str, Any]]) -> None:
    db = SessionLocal()
    try:
        brand = records[0]["brand"]
        dates = [dt.date.fromisoformat(record["data_date"]) for record in records]
        db.query(BrandDailyMetric).filter(
            BrandDailyMetric.brand == brand,
            BrandDailyMetric.data_date >= min(dates),
            BrandDailyMetric.data_date <= max(dates),
        ).delete(synchronize_session=False)
        for record in records:
            db.add(BrandDailyMetric(
                brand=record["brand"],
                data_date=dt.date.fromisoformat(record["data_date"]),
                brand_uv=record["brand_uv"],
                brand_pv=record["brand_pv"],
                ctr=record["ctr"],
                cvr=record["cvr"],
                top_query_count=record["top_query_count"],
                top_query_pv=record["top_query_pv"],
                top_query_pv_share=record["top_query_pv_share"],
                raw_payload=json.dumps(record["raw_payload"], ensure_ascii=False, separators=(",", ":")),
            ))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def backfill(*, brand: str, start_date: dt.date | None = None, end_date: dt.date | None = None, days: int = DEFAULT_DAYS, extractor: QueryExtractor | None = None) -> tuple[dt.date, dt.date, int]:
    source = extractor or TopQueryDataExtractor()
    resolved_start, resolved_end = _resolve_window(source, brand, start_date, end_date, days)
    records = []
    for target_date in iter_dates(resolved_start, resolved_end):
        metric = source.get_brand_metric(brand, target_date)
        query_rows = source.get_top_queries(brand, target_date, limit=1000)
        records.append(build_metric_record(metric, query_rows))
    _replace_local_rows(records)
    return resolved_start, resolved_end, len(records)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill local metrics from the read-only source database")
    parser.add_argument("--brand", default=DEFAULT_BRAND)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--skip-metrics", action="store_true", help="only initialize schema; never writes synthetic metrics")
    return parser


def seed(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    init_db()
    if args.skip_metrics:
        print("initialized local database without metric rows")
        return 0
    try:
        start_date = _parse_date(args.start_date, "start-date")
        end_date = _parse_date(args.end_date, "end-date")
        start, end, count = backfill(brand=args.brand, start_date=start_date, end_date=end_date, days=args.days)
        print(f"backfilled {count} real BrandDailyMetric rows for {args.brand}: {start}..{end}")
        return 0
    except Exception as exc:
        print(f"backfill failed before local commit: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(seed())
