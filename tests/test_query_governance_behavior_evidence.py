from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, text

from src.config import DEFAULT_BRAND_BUSINESS_CONFIG
from src.api.shared import TOP_QUERY_CONFIG_KEYS
from src.query_governance.behavior_evidence import (
    BehaviorEvidenceConfig,
    BehaviorEvidenceError,
    BehaviorEvidenceProduct,
    BehaviorEvidenceResult,
    BehaviorEvidenceService,
    BehaviorEvidenceSourceSpec,
    DEFAULT_BEHAVIOR_EVIDENCE_SQL_VERSION,
    OperationalProductDetailGateway,
    SqlBehaviorEvidenceSource,
    _quote_identifier,
)


class _StaticSource:
    def __init__(self, products: tuple[BehaviorEvidenceProduct, ...]) -> None:
        self.products = products
        self.calls = []

    def fetch(self, brand, query, data_date, config):
        self.calls.append((brand, query, data_date, config))
        return self.products


class _RecordingDetails:
    def __init__(self, details):
        self.details = details
        self.calls = []

    def fetch(self, brand, product_ids):
        self.calls.append((brand, tuple(product_ids)))
        return self.details


def _create_behavior_table(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE product_funnel (
                    biz_date TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    raw_query TEXT NOT NULL,
                    standard_query TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_click_pv INTEGER NOT NULL,
                    product_pay_pv INTEGER NOT NULL,
                    click_rank INTEGER,
                    pay_rank INTEGER,
                    search_result_position_avg REAL,
                    search_result_position_min INTEGER,
                    search_result_position_max INTEGER,
                    search_result_position_mode INTEGER,
                    is_active INTEGER NOT NULL,
                    product_name TEXT,
                    product_description TEXT,
                    category TEXT,
                    style TEXT,
                    color TEXT,
                    main_image_url TEXT,
                    price REAL,
                    discount_price REAL
                )
                """
            )
        )


@pytest.fixture
def behavior_engine():
    engine = create_engine("sqlite:///:memory:")
    try:
        yield engine
    finally:
        engine.dispose()


def test_config_defaults_to_eight_products_per_ranking_and_validates_values():
    assert BehaviorEvidenceConfig.from_mapping({}) == BehaviorEvidenceConfig(
        top_purchase_product_limit=8,
        top_click_product_limit=8,
    )
    assert BehaviorEvidenceConfig.from_mapping(
        {
            "query_governance_preference_top_purchase_product_limit": 5,
            "query_governance_preference_top_click_product_limit": 6,
        }
    ) == BehaviorEvidenceConfig(5, 6)

    with pytest.raises(ValueError):
        BehaviorEvidenceConfig(top_purchase_product_limit=0)
    with pytest.raises(ValueError):
        BehaviorEvidenceConfig(top_click_product_limit=True)
    with pytest.raises(ValueError):
        BehaviorEvidenceConfig.from_mapping(
            {"query_governance_preference_top_click_product_limit": True}
        )
    assert BehaviorEvidenceConfig.from_mapping(
        {"query_governance_preference_behavior_evidence_fallback_days": 3}
    ).fallback_days == 3
    with pytest.raises(ValueError):
        BehaviorEvidenceConfig(fallback_days=-1)


def test_table_reference_accepts_table_at_database_notation_without_reversing_names():
    assert _quote_identifier(
        "dws_brand_query_product_funnel_daily@industry_knowledge_base"
    ) == "`industry_knowledge_base`.`dws_brand_query_product_funnel_daily`"


def test_behavior_evidence_limits_are_exposed_as_brand_business_config():
    keys = {
        "query_governance_preference_top_purchase_product_limit",
        "query_governance_preference_top_click_product_limit",
    }

    assert {
        key: DEFAULT_BRAND_BUSINESS_CONFIG[key]
        for key in keys
    } == {
        "query_governance_preference_top_purchase_product_limit": 8,
        "query_governance_preference_top_click_product_limit": 8,
    }
    assert keys.issubset(TOP_QUERY_CONFIG_KEYS)


def test_sql_source_reads_one_day_and_merges_click_and_purchase_rankings(behavior_engine):
    engine = behavior_engine
    _create_behavior_table(engine)
    rows = [
        ("2026-08-27", "ATLAS", "连衣裙", "连衣裙", "P1", 90, 20, 1, 2, 1),
        ("2026-08-27", "ATLAS", "连衣裙", "连衣裙", "P2", 80, 0, 2, 8, 1),
        ("2026-08-27", "ATLAS", "连衣裙", "连衣裙", "P3", 2, 30, 9, 1, 1),
        ("2026-08-27", "ATLAS", "连衣裙", "连衣裙", "P4", 70, 25, 3, 3, 1),
        ("2026-08-26", "ATLAS", "连衣裙", "连衣裙", "OLD", 999, 999, 1, 1, 1),
        ("2026-08-27", "ATLAS", "连衣裙", "连衣裙", "OFF", 999, 999, 1, 1, 0),
    ]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO product_funnel (
                    biz_date, brand, raw_query, standard_query, product_id,
                    product_click_pv, product_pay_pv, click_rank, pay_rank, is_active
                ) VALUES
                (:biz_date, :brand, :raw_query, :standard_query, :product_id,
                 :click_pv, :pay_pv, :click_rank, :pay_rank, :is_active)
                """
            ),
            [
                {
                    "biz_date": row[0],
                    "brand": row[1],
                    "raw_query": row[2],
                    "standard_query": row[3],
                    "product_id": row[4],
                    "click_pv": row[5],
                    "pay_pv": row[6],
                    "click_rank": row[7],
                    "pay_rank": row[8],
                    "is_active": row[9],
                }
                for row in rows
            ],
        )

    source = SqlBehaviorEvidenceSource(
        engine,
        specs={
            "atlas": BehaviorEvidenceSourceSpec(
                table_name="product_funnel",
                source_brand="ATLAS",
            )
        },
    )

    products = source.fetch(
        "atlas",
        "连衣裙",
        date(2026, 8, 27),
        BehaviorEvidenceConfig(2, 2),
    )

    assert [product.product_id for product in products] == ["P1", "P3", "P2"]
    assert products[0].click_rank == 1
    assert products[0].purchase_rank == 2
    assert products[0].current_rank is None
    assert products[0].click_pv == 90
    assert products[0].purchase_pv == 20
    assert products[1].click_rank == 9
    assert products[1].purchase_rank == 1
    assert products[2].click_rank == 2
    assert products[2].purchase_rank == 8


def test_sql_source_keeps_all_eight_click_and_eight_purchase_candidates_after_dedup(behavior_engine):
    engine = behavior_engine
    _create_behavior_table(engine)
    rows = []
    for rank in range(1, 9):
        rows.append({
            "biz_date": "2026-08-27", "brand": "ATLAS", "raw_query": "连衣裙",
            "standard_query": "连衣裙", "product_id": f"C{rank}",
            "click_pv": 100 - rank, "pay_pv": 0, "click_rank": rank,
            "pay_rank": None, "is_active": 1,
        })
    for rank in range(1, 9):
        rows.append({
            "biz_date": "2026-08-27", "brand": "ATLAS", "raw_query": "连衣裙",
            "standard_query": "连衣裙", "product_id": f"C{rank}" if rank <= 4 else f"P{rank}",
            "click_pv": 0, "pay_pv": 100 - rank, "click_rank": None,
            "pay_rank": rank, "is_active": 1,
        })
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO product_funnel (
                biz_date, brand, raw_query, standard_query, product_id,
                product_click_pv, product_pay_pv, click_rank, pay_rank, is_active
            ) VALUES (:biz_date, :brand, :raw_query, :standard_query, :product_id,
                      :click_pv, :pay_pv, :click_rank, :pay_rank, :is_active)
        """), rows)
    products = SqlBehaviorEvidenceSource(
        engine, specs={"atlas": BehaviorEvidenceSourceSpec(table_name="product_funnel", source_brand="ATLAS")}
    ).fetch("atlas", "连衣裙", date(2026, 8, 27), BehaviorEvidenceConfig())
    assert len(products) == 12
    assert len({item.product_id for item in products}) == 12
    assert {item.product_id for item in products} == {f"C{i}" for i in range(1, 9)} | {f"P{i}" for i in range(5, 9)}


def test_sql_source_accepts_raw_query_and_returns_empty_when_no_prior_date_is_available(behavior_engine):
    engine = behavior_engine
    _create_behavior_table(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO product_funnel (
                    biz_date, brand, raw_query, standard_query, product_id,
                    product_click_pv, product_pay_pv, click_rank, pay_rank, is_active
                ) VALUES
                ('2026-08-26', 'Harbor', '女士 衬衫', '女士衬衫', 'P1', 10, 2, 1, 1, 1)
                """
            )
        )
    source = SqlBehaviorEvidenceSource(
        engine,
        specs={
            "harbor": BehaviorEvidenceSourceSpec(
                table_name="product_funnel",
                source_brand="Harbor",
            )
        },
    )

    old_products = source.fetch(
        "harbor",
        "女士 衬衫",
        date(2026, 8, 26),
        BehaviorEvidenceConfig(),
    )
    missing_products = source.fetch(
        "harbor",
        "女士 衬衫",
        date(2026, 8, 25),
        BehaviorEvidenceConfig(),
    )

    assert [product.product_id for product in old_products] == ["P1"]
    assert missing_products == ()


def test_service_falls_back_to_nearest_available_prior_date_and_records_it(behavior_engine):
    engine = behavior_engine
    _create_behavior_table(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO product_funnel (
                    biz_date, brand, raw_query, standard_query, product_id,
                    product_click_pv, product_pay_pv, click_rank, pay_rank, is_active
                ) VALUES
                ('2026-08-26', 'ATLAS', '连衣裙', '连衣裙', 'P1', 10, 2, 1, 1, 1)
                """
            )
        )
    source = SqlBehaviorEvidenceSource(
        engine,
        specs={"atlas": BehaviorEvidenceSourceSpec(table_name="product_funnel", source_brand="ATLAS")},
    )

    result = BehaviorEvidenceService(
        source=source, product_details=_RecordingDetails({"P1": {"productId": "P1"}})
    ).collect(brand="atlas", query="连衣裙", data_date=date(2026, 8, 27))

    assert result.status == "success"
    assert result.data_date == date(2026, 8, 26)
    assert result.requested_data_date == date(2026, 8, 27)
    assert result.date_fallback_reason == "requested_date_unavailable"
    assert [item["product_id"] for item in result.products] == ["P1"]


def test_service_returns_no_data_when_no_prior_date_is_available(behavior_engine):
    _create_behavior_table(behavior_engine)
    source = SqlBehaviorEvidenceSource(
        behavior_engine,
        specs={"atlas": BehaviorEvidenceSourceSpec(table_name="product_funnel", source_brand="ATLAS")},
    )

    result = BehaviorEvidenceService(
        source=source, product_details=_RecordingDetails({})
    ).collect(brand="atlas", query="连衣裙", data_date=date(2026, 8, 27))

    assert result.status == "no_data"
    assert result.data_date == date(2026, 8, 27)
    assert result.requested_data_date == date(2026, 8, 27)
    assert result.date_fallback_reason is None


def test_service_can_disable_date_fallback(behavior_engine):
    _create_behavior_table(behavior_engine)
    with behavior_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO product_funnel (
                    biz_date, brand, raw_query, standard_query, product_id,
                    product_click_pv, product_pay_pv, click_rank, pay_rank, is_active
                ) VALUES
                ('2026-08-26', 'ATLAS', '连衣裙', '连衣裙', 'P1', 10, 2, 1, 1, 1)
                """
            )
        )
    source = SqlBehaviorEvidenceSource(
        behavior_engine,
        specs={"atlas": BehaviorEvidenceSourceSpec(table_name="product_funnel", source_brand="ATLAS")},
    )

    result = BehaviorEvidenceService(
        source=source, product_details=_RecordingDetails({})
    ).collect(
        brand="atlas", query="连衣裙", data_date=date(2026, 8, 27),
        config=BehaviorEvidenceConfig(fallback_days=0),
    )

    assert result.status == "no_data"
    assert result.date_fallback_reason is None


def test_sql_contract_version_is_carried_by_the_collected_result():
    source = _StaticSource(())
    source.sql_version_for = lambda _brand: "behavior-evidence-sql-v2"

    result = BehaviorEvidenceService(
        source=source, product_details=_RecordingDetails({})
    ).collect(
        brand="atlas",
        query="连衣裙",
        data_date=date(2026, 8, 27),
    )

    assert result.sql_version == "behavior-evidence-sql-v2"
    assert DEFAULT_BEHAVIOR_EVIDENCE_SQL_VERSION == "behavior-evidence-sql-v1"


def test_sql_contract_version_must_be_non_empty():
    with pytest.raises(ValueError, match="sql_version"):
        BehaviorEvidenceSourceSpec(sql_version=" ")


def test_sql_source_maps_internal_summit_brand_to_behavior_table_value(behavior_engine):
    engine = behavior_engine
    _create_behavior_table(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO product_funnel (
                    biz_date, brand, raw_query, standard_query, product_id,
                    product_click_pv, product_pay_pv, click_rank, pay_rank, is_active
                ) VALUES
                ('2026-08-27', 'Summit', '卫衣', '卫衣', '8603336', 10, 2, 1, 1, 1)
                """
            )
        )
    source = SqlBehaviorEvidenceSource(
        engine,
        specs={
            "summit": BehaviorEvidenceSourceSpec(table_name="product_funnel")
        },
    )

    products = source.fetch(
        "summit",
        "卫衣",
        date(2026, 8, 27),
        BehaviorEvidenceConfig(),
    )

    assert [product.product_id for product in products] == ["8603336"]


def test_sql_source_reads_behavior_table_product_attributes_and_optional_current_rank():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE product_funnel_attributes (
                    biz_date TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    raw_query TEXT NOT NULL,
                    standard_query TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_click_pv INTEGER NOT NULL,
                    product_pay_pv INTEGER NOT NULL,
                    click_rank INTEGER,
                    pay_rank INTEGER,
                    current_rank INTEGER,
                    search_result_position_avg REAL,
                    search_result_position_min INTEGER,
                    search_result_position_max INTEGER,
                    search_result_position_mode INTEGER,
                    product_name TEXT,
                    product_description TEXT,
                    category TEXT,
                    style TEXT,
                    color TEXT,
                    main_image_url TEXT,
                    price REAL,
                    discount_price REAL,
                    is_active INTEGER NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO product_funnel_attributes VALUES
                ('2026-08-27', 'ATLAS', '连衣裙', '连衣裙', 'P1',
                 90, 20, 1, 2, 7, 2.0, 1, 4, 1, '亚麻连衣裙', '轻盈透气',
                 '连衣裙', '修身', '红色', 'https://cdn.example/p1.jpg',
                 399, 299, 1)
                """
            )
        )

    source = SqlBehaviorEvidenceSource(
        engine,
        specs={
            "atlas": BehaviorEvidenceSourceSpec(
                table_name="product_funnel_attributes",
                source_brand="ATLAS",
                product_name_column="product_name",
                product_description_column="product_description",
                category_column="category",
                style_column="style",
                color_column="color",
                image_url_column="main_image_url",
                price_column="price",
                discount_price_column="discount_price",
                current_rank_column="current_rank",
            )
        },
    )

    products = source.fetch(
        "atlas",
        "连衣裙",
        date(2026, 8, 27),
        BehaviorEvidenceConfig(),
    )

    assert products == (
        BehaviorEvidenceProduct(
            product_id="P1",
            click_rank=1,
            purchase_rank=2,
            current_rank=7,
            click_pv=90,
            purchase_pv=20,
            search_result_position_avg=2.0,
            search_result_position_min=1,
            search_result_position_max=4,
            search_result_position_mode=1,
            product_name="亚麻连衣裙",
            product_description="轻盈透气",
            category="连衣裙",
            style="修身",
            color="红色",
            image_url="https://cdn.example/p1.jpg",
            price=399,
            discount_price=299,
        ),
    )


def test_sql_source_reads_all_position_statistics_without_collapsing_them():
    engine = create_engine("sqlite:///:memory:")
    _create_behavior_table(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO product_funnel (
                    biz_date, brand, raw_query, standard_query, product_id,
                    product_click_pv, product_pay_pv, click_rank, pay_rank,
                    search_result_position_avg, search_result_position_min,
                    search_result_position_max, search_result_position_mode,
                    is_active
                ) VALUES (
                    '2026-08-27', 'ATLAS', '裙子', '裙子', 'P1',
                    30, 8, 1, 2, 2.0, 1, 4, 1, 1
                )
                """
            )
        )

    products = SqlBehaviorEvidenceSource(
        engine,
        specs={
            "atlas": BehaviorEvidenceSourceSpec(
                table_name="product_funnel", source_brand="ATLAS"
            )
        },
    ).fetch("atlas", "裙子", date(2026, 8, 27), BehaviorEvidenceConfig())

    assert products[0].current_rank is None
    assert products[0].search_result_position_avg == 2
    assert products[0].search_result_position_min == 1
    assert products[0].search_result_position_max == 4
    assert products[0].search_result_position_mode == 1


def test_service_preserves_position_statistics_and_allows_missing_values():
    source = _StaticSource(
        (
            BehaviorEvidenceProduct(
                product_id="P1",
                click_rank=1,
                purchase_rank=2,
                current_rank=None,
                click_pv=30,
                purchase_pv=8,
                search_result_position_avg=2.0,
                search_result_position_min=1,
                search_result_position_max=4,
                search_result_position_mode=1,
            ),
            BehaviorEvidenceProduct(
                product_id="P2",
                click_rank=2,
                purchase_rank=None,
                current_rank=None,
                click_pv=20,
                purchase_pv=0,
            ),
        )
    )

    result = BehaviorEvidenceService(
        source=source, product_details=_RecordingDetails({})
    ).collect(
        brand="atlas",
        query="裙子",
        data_date=date(2026, 8, 27),
    )

    assert result.status == "success"
    assert result.products[0]["search_result_position_avg"] == 2.0
    assert result.products[0]["search_result_position_min"] == 1
    assert result.products[0]["search_result_position_max"] == 4
    assert result.products[0]["search_result_position_mode"] == 1
    assert result.products[1]["search_result_position_avg"] is None
    assert result.products[1]["search_result_position_min"] is None
    assert result.products[1]["search_result_position_max"] is None
    assert result.products[1]["search_result_position_mode"] is None


def test_service_uses_sql_product_attributes_when_detail_gateway_has_no_row():
    source = _StaticSource(
        (
            BehaviorEvidenceProduct(
                product_id="P1",
                click_rank=1,
                purchase_rank=2,
                current_rank=None,
                click_pv=30,
                purchase_pv=8,
                product_name="红色亚麻连衣裙",
                product_description="轻盈透气",
                category="连衣裙",
                style="修身",
                color="红色",
                image_url="https://cdn.example/p1.jpg",
                price=399,
                discount_price=299,
            ),
        )
    )
    details = _RecordingDetails({})

    result = BehaviorEvidenceService(source=source, product_details=details).collect(
        brand="atlas",
        query="连衣裙",
        data_date=date(2026, 8, 27),
        config=BehaviorEvidenceConfig(),
    )

    assert result.products[0]["product_name"] == "红色亚麻连衣裙"
    assert result.products[0]["description"] == "轻盈透气"
    assert result.products[0]["category"] == "连衣裙"
    assert result.products[0]["style"] == "修身"
    assert result.products[0]["color"] == "红色"
    assert result.products[0]["image_url"] == "https://cdn.example/p1.jpg"
    assert result.products[0]["price"] == 399.0
    assert result.products[0]["discount_price"] == 299.0


def test_service_returns_normal_no_data_result_without_calling_product_details():
    source = _StaticSource(())
    details = _RecordingDetails({})
    service = BehaviorEvidenceService(source=source, product_details=details)

    result = service.collect(
        brand="riviera",
        query="polo",
        data_date=date(2026, 8, 27),
        config=BehaviorEvidenceConfig(),
    )

    assert result == BehaviorEvidenceResult(
        status="no_data",
        brand="riviera",
        query="polo",
        data_date=date(2026, 8, 27),
        products=(),
        requested_data_date=date(2026, 8, 27),
    )
    assert details.calls == []


def test_service_enriches_generic_products_and_flattens_text_attributes():
    source = _StaticSource(
        (
            BehaviorEvidenceProduct("P1", 1, 2, None, 30, 8),
            BehaviorEvidenceProduct("P2", 2, None, None, 20, 0),
        )
    )
    details = _RecordingDetails(
        {
            "P1": {
                "productId": "P1",
                "productName": "红色亚麻连衣裙",
                "mainImage": "https://cdn.example.com/p1.jpg",
                "originalPrice": 399,
                "discountPrice": 299,
                "description": "轻盈透气",
                "gender": "women",
                "category": "连衣裙",
                "color": "红色",
                "material": "亚麻",
                "tagName": "夏季,通勤",
            },
            "P2": {
                "productId": "P2",
                "productName": "黑色连衣裙",
                "price": 199,
                "tags": ["基础款", "黑色"],
            },
        }
    )

    result = BehaviorEvidenceService(source=source, product_details=details).collect(
        brand="atlas",
        query="连衣裙",
        data_date=date(2026, 8, 27),
        config=BehaviorEvidenceConfig(),
    )

    assert result.status == "success"
    assert details.calls == [("atlas", ("P1", "P2"))]
    assert result.products[0] == {
        "product_id": "P1",
        "click_rank": 1,
        "purchase_rank": 2,
        "current_rank": None,
        "search_result_position_avg": None,
        "search_result_position_min": None,
        "search_result_position_max": None,
        "search_result_position_mode": None,
        "click_pv": 30,
        "purchase_pv": 8,
        "product_name": "红色亚麻连衣裙",
        "image_url": "https://cdn.example.com/p1.jpg",
        "price": 399.0,
        "discount_price": 299.0,
        "description": "轻盈透气",
        "gender": "women",
        "series": None,
        "category": "连衣裙",
        "style": None,
        "color": "红色",
        "size": None,
        "material": "亚麻",
        "design": None,
        "stock": None,
        "is_new": None,
        "tags": "夏季,通勤",
    }
    assert result.products[1]["purchase_rank"] is None
    assert result.products[1]["tags"] == ["基础款", "黑色"]


def test_service_enriches_documented_flat_summit_detail_response():
    source = _StaticSource(
        (BehaviorEvidenceProduct("8603336", 1, 1, None, 12, 4),)
    )
    details = _RecordingDetails(
        {
            "8603336": {
                "id": "summit_8603336",
                "productId": None,
                "spu": "8603336",
                "productName": "街舞情侣绿色连帽卫衣",
                "imageUrl": "https://cdn.example.com/8603336.jpg",
                "tagName": "运动,宽松,卫衣",
                "mainCategory": "卫衣",
                "color": "绿色",
                "material": "棉",
                "inventory": 100,
            }
        }
    )

    result = BehaviorEvidenceService(source=source, product_details=details).collect(
        brand="summit",
        query="卫衣",
        data_date=date(2026, 8, 27),
        config=BehaviorEvidenceConfig(),
    )

    assert result.status == "success"
    assert result.products[0]["product_id"] == "8603336"
    assert result.products[0]["product_name"] == "街舞情侣绿色连帽卫衣"
    assert result.products[0]["category"] == "卫衣"
    assert result.products[0]["color"] == "绿色"
    assert result.products[0]["material"] == "棉"
    assert result.products[0]["stock"] == 100
    assert result.products[0]["tags"] == "运动,宽松,卫衣"


class _FakeSummitService:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_product_details_with_status(self, product_ids):
        self.calls.append(product_ids)
        if isinstance(self.response, dict) and product_ids in self.response:
            return self.response[product_ids]
        return self.response


class _FakeOperationalService:
    def __init__(self, *, status_map=None, summit_response=None):
        self.status_map = status_map
        self.status_calls = []
        self.summit_service = _FakeSummitService(
            summit_response
            or {"success": True, "products": [], "error_reason": None}
        )

    def get_product_status_map(self, brand, product_ids):
        self.status_calls.append((brand, tuple(product_ids)))
        return self.status_map


def test_operational_gateway_preserves_raw_generic_and_summit_details():
    generic_raw = {"productId": "P1", "productName": "商品一"}
    generic_service = _FakeOperationalService(
        status_map={"P1": {"exists": True, "is_online": True, "raw": generic_raw}}
    )

    generic = OperationalProductDetailGateway(generic_service).fetch("atlas", ["P1"])

    assert generic == {"P1": generic_raw}
    assert generic_service.status_calls == [("atlas", ("P1",))]

    summit_raw = {
        "spu": "8603336",
        "productName": "Summit卫衣",
        "tagName": "运动,卫衣",
    }
    summit_service = _FakeOperationalService(
        summit_response={
            "success": True,
            "products": [summit_raw],
            "error_reason": None,
        }
    )

    summit = OperationalProductDetailGateway(summit_service).fetch(
        "summit", ["8603336"]
    )

    assert summit == {"8603336": summit_raw}
    assert summit_service.summit_service.calls == ["8603336"]


def test_operational_gateway_expands_composite_product_ids_and_maps_a_match_back():
    matched_raw = {"productId": "C07815250800", "productName": "条纹拉链外套"}
    service = _FakeOperationalService(
        status_map={
            "C07815250800": {"exists": True, "is_online": True, "raw": matched_raw},
            "7815002800": {
                "exists": True,
                "is_online": True,
                "raw": {"productId": "7815002800"},
            },
        }
    )

    details = OperationalProductDetailGateway(service).fetch(
        "atlas", ["C07815250800,7815002800,7815200800"]
    )

    assert service.status_calls == [
        ("atlas", ("C07815250800", "7815002800", "7815200800"))
    ]
    assert details == {
        "C07815250800,7815002800,7815200800": matched_raw,
    }


def test_operational_gateway_reads_each_summit_product_without_page_size_loss():
    rows = {
        product_id: {
            "success": True,
            "products": [{"spu": product_id, "productName": f"商品{product_id}"}],
            "error_reason": None,
        }
        for product_id in ("8603336", "8603337")
    }
    service = _FakeOperationalService(summit_response=rows)

    details = OperationalProductDetailGateway(service).fetch(
        "summit", ["8603336", "8603337"]
    )

    assert list(details) == ["8603336", "8603337"]
    assert service.summit_service.calls == ["8603336", "8603337"]


def test_sql_failure_and_product_detail_failure_have_explicit_error_codes():
    class _BrokenSource:
        def fetch(self, *_args):
            raise RuntimeError("database unavailable")

    with pytest.raises(BehaviorEvidenceError) as sql_error:
        BehaviorEvidenceService(
            source=_BrokenSource(), product_details=_RecordingDetails({})
        ).collect(
            brand="atlas",
            query="连衣裙",
            data_date=date(2026, 8, 27),
            config=BehaviorEvidenceConfig(),
        )
    assert sql_error.value.error_code == "BEHAVIOR_EVIDENCE_QUERY_FAILED"

    class _BrokenDetails:
        def fetch(self, *_args):
            raise RuntimeError("product API unavailable")

    with pytest.raises(BehaviorEvidenceError) as detail_error:
        BehaviorEvidenceService(
            source=_StaticSource(
                (BehaviorEvidenceProduct("P1", 1, 1, None, 10, 3),)
            ),
            product_details=_BrokenDetails(),
        ).collect(
            brand="atlas",
            query="连衣裙",
            data_date=date(2026, 8, 27),
            config=BehaviorEvidenceConfig(),
        )
    assert detail_error.value.error_code == "BEHAVIOR_PRODUCT_DETAILS_FAILED"
