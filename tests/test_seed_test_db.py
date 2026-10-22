import datetime as dt
import importlib.util
from pathlib import Path

import pytest
from src.persistence import BrandDailyMetric, SessionLocal, init_db, reset_engine_for_testing


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "db" / "seed_test_db.py"
SPEC = importlib.util.spec_from_file_location("seed_test_db", SCRIPT_PATH)
assert SPEC and SPEC.loader
seed_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed_module)


def test_build_metric_record_persists_real_source_fields_and_query_aggregate():
    metric = {
        "brand": "atlas",
        "data_date": "2026-09-08",
        "brand_uv": 9429.0,
        "brand_pv": 26381.0,
        "click_uv": 7128.0,
        "add_to_cart_uv": 2509.0,
        "purchase_uv": 64.0,
        "purchase_amount": 16480.0,
        "ctr": 0.756,
        "atc": 0.2661,
        "cvr": 0.0068,
        "metric_source": "user_behavior",
    }
    queries = [
        {"query": "香水 女士", "search_pv": 811},
        {"query": "牛仔裤 女士", "search_pv": 565},
    ]

    result = seed_module.build_metric_record(metric, queries)

    assert result["brand_uv"] == 9429
    assert result["brand_pv"] == 26381
    assert result["ctr"] == pytest.approx(0.756)
    assert result["cvr"] == pytest.approx(0.0068)
    assert result["top_query_count"] == 2
    assert result["top_query_pv"] == 1376
    assert result["top_query_pv_share"] == pytest.approx(1376 / 26381)
    assert result["raw_payload"]["click_uv"] == 7128
    assert result["raw_payload"]["add_to_cart_uv"] == 2509
    assert result["raw_payload"]["purchase_uv"] == 64
    assert result["raw_payload"]["metric_source"] == "user_behavior"


def test_build_metric_record_rejects_missing_or_non_real_source_data():
    with pytest.raises(ValueError, match="metric_source"):
        seed_module.build_metric_record(
            {"brand": "atlas", "data_date": "2026-09-08", "brand_uv": 1}, []
        )

    with pytest.raises(ValueError, match="source metric is empty"):
        seed_module.build_metric_record(
            {
                "brand": "atlas",
                "data_date": "2026-09-08",
                "brand_uv": 0,
                "brand_pv": 0,
                "metric_source": "user_behavior",
            },
            [],
        )


def test_date_window_is_inclusive_and_does_not_use_fixed_synthetic_anchor():
    dates = seed_module.iter_dates(dt.date(2026, 9, 7), dt.date(2026, 9, 9))
    assert dates == [
        dt.date(2026, 9, 7),
        dt.date(2026, 9, 8),
        dt.date(2026, 9, 9),
    ]


class _SourceFixture:
    def get_brand_metric(self, brand, target_date):
        return {
            "brand": brand,
            "data_date": target_date.isoformat(),
            "brand_uv": 100 + target_date.day,
            "brand_pv": 200 + target_date.day,
            "click_uv": 70,
            "add_to_cart_uv": 20,
            "purchase_uv": 3,
            "purchase_amount": 123.4,
            "ctr": 0.7,
            "atc": 0.2,
            "cvr": 0.03,
            "metric_source": "user_behavior",
        }

    def get_top_queries(self, brand, target_date, limit):
        return [{"query": "真实 Query", "search_pv": 12}]


def test_backfill_replaces_only_requested_window_and_persists_source_payload():
    reset_engine_for_testing()
    init_db()
    db = SessionLocal()
    db.add(BrandDailyMetric(brand="atlas", data_date=dt.date(2026, 8, 1), brand_uv=999))
    db.commit()
    db.close()

    start, end, count = seed_module.backfill(
        brand="atlas",
        start_date=dt.date(2026, 9, 7),
        end_date=dt.date(2026, 9, 8),
        extractor=_SourceFixture(),
    )

    assert (start, end, count) == (dt.date(2026, 9, 7), dt.date(2026, 9, 8), 2)
    rows = SessionLocal().query(BrandDailyMetric).order_by(BrandDailyMetric.data_date).all()
    assert [row.data_date for row in rows] == [dt.date(2026, 8, 1), dt.date(2026, 9, 7), dt.date(2026, 9, 8)]
    assert rows[-1].brand_uv == 108
    assert '"metric_source":"user_behavior"' in rows[-1].raw_payload
