from __future__ import annotations

from datetime import date
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.persistence.models import Base
from src.query_governance.models import (
    QueryGovernanceCandidate,
    QueryGovernanceSelectionBatch,
    QueryGovernanceTask,
)
from src.query_governance.selection import (
    BrandQueryMetrics,
    CandidateDataset,
    FixedExperimentSample,
    InMemoryQuerySelectionStore,
    QueryCandidateMetrics,
    QuerySelectionConfig,
    QuerySelectionService,
    QuerySourceSpec,
    SqlAlchemyQuerySelectionStore,
    build_candidate_sql,
    is_valid_governance_query,
)


class FakeCandidateSource:
    def __init__(self, dataset: CandidateDataset) -> None:
        self.dataset = dataset
        self.calls: list[tuple[str, date, QuerySelectionConfig]] = []

    def fetch(
        self,
        brand: str,
        data_date: date,
        config: QuerySelectionConfig,
    ) -> CandidateDataset:
        self.calls.append((brand, data_date, config))
        return self.dataset


class RecordingWorkflow:
    def __init__(self) -> None:
        self.specs = []

    def run_batch(self, specs, *, batch_id=None, data_date=None):
        self.specs = list(specs)
        return {spec.query_id: {"query": spec.query} for spec in self.specs}


@pytest.mark.parametrize("has_candidates", [False, True])
def test_empty_dynamic_selection_skips_workflow_and_is_idempotent(has_candidates):
    source = FakeCandidateSource(CandidateDataset(
        brand=BrandQueryMetrics(search_uv=100, click_uv=20, purchase_uv=5),
        queries=(metrics("连衣裙", pv=30),) if has_candidates else (),
    ))
    store = InMemoryQuerySelectionStore()
    service = QuerySelectionService(source=source, store=store)
    workflow = Mock()
    frozen = service.select_batch(
        batch_id="empty-dynamic", brand="atlas", data_date=date(2026, 9, 7),
        config=QuerySelectionConfig(high_pv_limit=1),
    )
    store = InMemoryQuerySelectionStore()
    store.save_batch(replace(frozen, candidates=tuple(
        replace(candidate, selected=False) for candidate in frozen.candidates
    )))
    service = QuerySelectionService(source=source, store=store)
    for _ in range(2):
        batch, runs = service.select_and_start(
            batch_id="empty-dynamic", brand="atlas", data_date=date(2026, 9, 7),
            config=QuerySelectionConfig(high_pv_limit=1), workflow=workflow,
        )
        assert not batch.selected
        assert bool(batch.candidates) == has_candidates
        assert runs == {}
    workflow.run_batch.assert_not_called()
    assert len(source.calls) == 1


def test_control_only_fixed_samples_keep_existing_experiment_behavior():
    source = Mock()
    workflow = RecordingWorkflow()
    service = QuerySelectionService(source=source, store=InMemoryQuerySelectionStore())
    service.select_and_start(
        batch_id="control-only", brand="atlas", data_date=date(2026, 9, 7),
        config=QuerySelectionConfig(), workflow=workflow,
        fixed_samples=(FixedExperimentSample("exp-1", "外套", "control", 1),),
    )
    source.fetch.assert_not_called()
    assert [spec.query for spec in workflow.specs] == ["外套"]


def metrics(
    query: str,
    *,
    pv: float,
    uv: float = 100,
    click: float = 20,
    purchase: float = 5,
    history_uv: float = 100,
    history_click: float = 20,
    history_purchase: float = 5,
    history_avg_pv: float = 20,
    history_max_daily_pv: float = 20,
) -> QueryCandidateMetrics:
    return QueryCandidateMetrics(
        query=query,
        search_pv=pv,
        search_uv=uv,
        click_uv=click,
        purchase_uv=purchase,
        history_search_uv=history_uv,
        history_click_uv=history_click,
        history_purchase_uv=history_purchase,
        history_average_pv=history_avg_pv,
        history_max_daily_pv=history_max_daily_pv,
    )


def test_candidate_sql_filters_invalid_queries_and_min_pv_without_source_limit():
    sql = build_candidate_sql(
        QuerySourceSpec(
            table_name="dwd_query_behavior_daily",
            query_column="query",
            search_pv_column="query_search_pv",
            search_uv_column="query_search_uv",
            click_uv_column="query_search_click_uv",
            purchase_uv_column="query_search_pay_uv",
        )
    )

    assert ":query_min_metric" in sql
    assert "REGEXP" in sql
    assert "history_average_pv" in sql
    assert "history_max_daily_pv" in sql
    assert "LIMIT" not in sql.upper()
    assert "折扣" not in sql
    assert "[[:space:]]*" in sql


def test_candidate_sql_history_aggregates_single_level_over_base_table():
    # MySQL 8 磁盘临时表对带 ci collation 的派生表列再 GROUP BY 会产生伪 1062
    # Duplicate entry（2026-10-14 HARBOR 历史窗口实发），历史聚合必须直查基表单层聚合。
    sql = build_candidate_sql(
        QuerySourceSpec(
            table_name="dwd_harbor_query_behavior_daily",
            query_column="query",
            search_pv_column="query_search_pv",
            search_uv_column="query_search_uv",
            click_uv_column="query_search_click_uv",
            purchase_uv_column="query_search_pay_uv",
        )
    )

    assert "history_daily" not in sql
    assert "GROUP BY query_text" not in sql
    assert sql.count("GROUP BY TRIM(`query`)") == 2
    assert "MAX(`query_search_pv`) AS history_max_daily_pv" in sql
    assert "FROM `dwd_harbor_query_behavior_daily`\n    WHERE `date` BETWEEN :history_start_date AND :history_end_date" in sql


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("6050802800 女士", False),
        ("6050802800女士", False),
        ("3205/810/800 女士", False),
        ("123 女士", True),
        ("Z1975 女士", True),
        ("100%棉", True),
    ],
)
def test_governance_query_validation_reuses_product_code_filter(query, expected):
    assert is_valid_governance_query(query) is expected


def test_dynamic_selection_excludes_numeric_product_code_with_atlas_suffix():
    source = FakeCandidateSource(CandidateDataset(
        brand=BrandQueryMetrics(search_uv=200, click_uv=40, purchase_uv=10),
        queries=(
            metrics("6050802800 女士", pv=100),
            metrics("连衣裙 女士", pv=50),
        ),
    ))
    service = QuerySelectionService(source=source, store=InMemoryQuerySelectionStore())

    batch = service.select_batch(
        batch_id="numeric-code-filter",
        brand="atlas",
        data_date=date(2026, 9, 13),
        config=QuerySelectionConfig(high_pv_limit=2),
    )

    assert [candidate.query for candidate in batch.candidates] == ["连衣裙 女士"]


def test_candidate_sql_can_filter_by_uv_when_uv_is_the_configured_floor():
    sql = build_candidate_sql(
        QuerySourceSpec(
            table_name="dwd_query_behavior_daily",
            query_column="query",
            search_pv_column="query_search_pv",
            search_uv_column="query_search_uv",
            click_uv_column="query_search_click_uv",
            purchase_uv_column="query_search_pay_uv",
        ),
        metric_type="uv",
    )

    assert "HAVING SUM(`query_search_uv`) >= :query_min_metric" in sql
    assert ":query_min_pv" not in sql


def test_selection_config_defaults_to_uv_and_supports_a_pv_override():
    default = QuerySelectionConfig.from_mapping({
        "query_governance_query_min_metric_type": "uv",
        "query_governance_query_min_metric_value": 12,
    })
    assert default.query_min_metric_type == "uv"
    assert default.query_min_metric_value == 12
    assert QuerySelectionConfig.from_mapping({
        "query_governance_query_min_metric_type": "pv",
        "query_governance_query_min_metric_value": 30,
    }).query_min_metric_type == "pv"


def test_selection_generates_union_of_all_seven_candidate_rules():
    config = QuerySelectionConfig(
        extract_limit=20,
        query_min_pv=5,
        high_pv_limit=1,
        alert_ctr_gap_pp=5,
        alert_cvr_gap_pp=2,
        history_days=7,
        new_query_pv_threshold=5,
        pv_growth_ratio_floor=1.2,
        pv_growth_sqrt_multiplier=2,
        pv_growth_abs_floor=5,
    )
    dataset = CandidateDataset(
        brand=BrandQueryMetrics(search_uv=1000, click_uv=300, purchase_uv=100),
        queries=(
            metrics(
                "多规则",
                pv=100,
                click=10,
                purchase=1,
                history_click=30,
                history_purchase=10,
                history_avg_pv=20,
                history_max_daily_pv=25,
            ),
            metrics(
                "新词",
                pv=8,
                click=30,
                purchase=10,
                history_uv=0,
                history_click=0,
                history_purchase=0,
                history_avg_pv=1,
                history_max_daily_pv=4,
            ),
        ),
    )
    service = QuerySelectionService(
        source=FakeCandidateSource(dataset),
        store=InMemoryQuerySelectionStore(),
    )

    result = service.select_batch(
        batch_id="batch-1",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=config,
    )

    rules = {rule for candidate in result.candidates for rule in candidate.rules}
    assert rules == {
        "high_pv",
        "low_ctr",
        "low_cvr",
        "ctr_decline",
        "cvr_decline",
        "new_query",
        "rapid_pv_growth",
    }
    assert [item.query for item in result.selected] == ["多规则", "新词"]


def test_selection_sorts_by_rule_count_ctr_decline_then_target_day_pv():
    config = QuerySelectionConfig(extract_limit=3, high_pv_limit=0)
    dataset = CandidateDataset(
        brand=BrandQueryMetrics(search_uv=1000, click_uv=160, purchase_uv=50),
        queries=(
            metrics("两规则", pv=10, click=10, history_click=20),
            metrics("一规则下降大", pv=10, click=15, history_click=30),
            metrics(
                "一规则PV高",
                pv=30,
                click=20,
                history_click=30,
                history_avg_pv=30,
            ),
            metrics(
                "一规则PV低",
                pv=20,
                click=20,
                history_click=30,
                history_avg_pv=20,
            ),
        ),
    )
    service = QuerySelectionService(
        source=FakeCandidateSource(dataset),
        store=InMemoryQuerySelectionStore(),
    )

    result = service.select_batch(
        batch_id="batch-sort",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=config,
    )

    assert [candidate.query for candidate in result.candidates] == [
        "两规则",
        "一规则下降大",
        "一规则PV高",
        "一规则PV低",
    ]
    assert [item.query for item in result.selected] == [
        "两规则",
        "一规则下降大",
        "一规则PV高",
    ]
    assert result.candidates[-1].selected is False


def test_batch_uses_frozen_config_even_if_input_mapping_changes():
    raw_config = {
        "query_governance_extract_limit": 1,
        "query_governance_candidate_high_pv_limit": 2,
    }
    config = QuerySelectionConfig.from_mapping(raw_config)
    raw_config["query_governance_extract_limit"] = 99
    dataset = CandidateDataset(
        brand=BrandQueryMetrics(search_uv=100, click_uv=20, purchase_uv=5),
        queries=(metrics("连衣裙", pv=30), metrics("外套", pv=20)),
    )
    source = FakeCandidateSource(dataset)
    service = QuerySelectionService(
        source=source,
        store=InMemoryQuerySelectionStore(),
    )

    result = service.select_batch(
        batch_id="batch-config",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=config,
    )

    assert result.config.extract_limit == 1
    assert len(result.selected) == 1
    assert source.calls[0][2] is config


def test_fixed_experiment_treatment_bypasses_dynamic_selection_and_control():
    source = FakeCandidateSource(
        CandidateDataset(
            brand=BrandQueryMetrics(search_uv=0, click_uv=0, purchase_uv=0),
            queries=(),
        )
    )
    service = QuerySelectionService(
        source=source,
        store=InMemoryQuerySelectionStore(),
    )

    result = service.select_batch(
        batch_id="experiment-1",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=QuerySelectionConfig(extract_limit=1),
        fixed_samples=(
            FixedExperimentSample("experiment-1", "连衣裙", "treatment", 1),
            FixedExperimentSample("experiment-1", "外套", "control", 2),
        ),
    )

    assert source.calls == []
    assert [item.query for item in result.selected] == ["连衣裙"]
    assert result.selected[0].source_type == "experiment_treatment"
    assert result.selected[0].rules == ("fixed_experiment_sample",)


def test_selected_queries_are_persisted_and_enter_s0_workflow():
    store = InMemoryQuerySelectionStore()
    workflow = RecordingWorkflow()
    service = QuerySelectionService(
        source=FakeCandidateSource(
            CandidateDataset(
                brand=BrandQueryMetrics(search_uv=100, click_uv=20, purchase_uv=5),
                queries=(metrics("连衣裙", pv=30),),
            )
        ),
        store=store,
    )

    result, workflow_result = service.select_and_start(
        batch_id="batch-start",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=QuerySelectionConfig(high_pv_limit=1),
        workflow=workflow,
    )

    assert store.get_batch("batch-start", "atlas") == result
    assert [spec.query for spec in workflow.specs] == ["连衣裙"]
    assert workflow_result == {workflow.specs[0].query_id: {"query": "连衣裙"}}


def test_selected_specs_carry_reorder_eligibility_from_ctr_or_cvr_decline():
    config = QuerySelectionConfig(
        extract_limit=10,
        high_pv_limit=0,
        alert_ctr_gap_pp=5,
        alert_cvr_gap_pp=2,
    )
    dataset = CandidateDataset(
        brand=BrandQueryMetrics(search_uv=1000, click_uv=200, purchase_uv=50),
        queries=(
            metrics(
                "CTR下降",
                pv=30,
                click=10,
                purchase=5,
                history_click=20,
                history_purchase=5,
            ),
            metrics(
                "CVR下降",
                pv=30,
                click=20,
                purchase=1,
                history_click=20,
                history_purchase=5,
            ),
            metrics(
                "指标正常",
                pv=30,
                click=20,
                purchase=5,
                history_click=20,
                history_purchase=5,
            ),
        ),
    )
    service = QuerySelectionService(
        source=FakeCandidateSource(dataset),
        store=InMemoryQuerySelectionStore(),
    )

    batch = service.select_batch(
        batch_id="batch-reorder-gate",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=config,
    )

    specs = {spec.query: spec for spec in batch.specs}
    assert specs["CTR下降"].reorder_eligible is True
    assert specs["CVR下降"].reorder_eligible is True
    assert specs["指标正常"].reorder_eligible is False
    assert specs["指标正常"].reorder_skip_reason == "CTR/CVR 正常"


def test_brand_reorder_disabled_skips_reorder_for_all_selected_queries():
    config = QuerySelectionConfig(
        extract_limit=10,
        high_pv_limit=1,
        reorder_enabled=False,
    )
    dataset = CandidateDataset(
        brand=BrandQueryMetrics(search_uv=1000, click_uv=200, purchase_uv=50),
        queries=(metrics("高PV词", pv=100),),
    )
    service = QuerySelectionService(
        source=FakeCandidateSource(dataset),
        store=InMemoryQuerySelectionStore(),
    )

    batch = service.select_batch(
        batch_id="batch-reorder-disabled",
        brand="summit",
        data_date=date(2026, 8, 27),
        config=config,
    )

    spec = batch.specs[0]
    assert spec.reorder_eligible is False
    assert spec.reorder_skip_reason == "品牌未启用重排序"


def test_fixed_experiment_treatment_remains_in_reorder_mainline():
    service = QuerySelectionService(
        source=FakeCandidateSource(
            CandidateDataset(
                brand=BrandQueryMetrics(search_uv=0, click_uv=0, purchase_uv=0),
                queries=(),
            )
        ),
        store=InMemoryQuerySelectionStore(),
    )

    batch = service.select_batch(
        batch_id="experiment-reorder-gate",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=QuerySelectionConfig(extract_limit=1),
        fixed_samples=(
            FixedExperimentSample("experiment-1", "连衣裙", "treatment", 1),
            FixedExperimentSample("experiment-1", "外套", "control", 2),
        ),
    )

    assert len(batch.specs) == 1
    assert batch.specs[0].query == "连衣裙"
    assert batch.specs[0].reorder_eligible is True


def test_reorder_gate_includes_exact_ctr_or_cvr_threshold_boundary():
    config = QuerySelectionConfig(
        extract_limit=10,
        high_pv_limit=0,
        alert_ctr_gap_pp=5,
        alert_cvr_gap_pp=2,
    )
    dataset = CandidateDataset(
        brand=BrandQueryMetrics(search_uv=1000, click_uv=200, purchase_uv=50),
        queries=(
            metrics(
                "CTR边界",
                pv=30,
                click=15,
                purchase=5,
                history_click=20,
                history_purchase=5,
            ),
            metrics(
                "CVR边界",
                pv=30,
                click=20,
                purchase=3,
                history_click=20,
                history_purchase=5,
            ),
        ),
    )
    service = QuerySelectionService(
        source=FakeCandidateSource(dataset),
        store=InMemoryQuerySelectionStore(),
    )

    batch = service.select_batch(
        batch_id="batch-reorder-boundary",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=config,
    )

    assert all(spec.reorder_eligible is True for spec in batch.specs)


def test_sqlalchemy_store_persists_batch_candidate_evidence_and_selected_tasks():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    config = QuerySelectionConfig(extract_limit=1, high_pv_limit=2)
    source = FakeCandidateSource(
        CandidateDataset(
            brand=BrandQueryMetrics(search_uv=1000, click_uv=300, purchase_uv=80),
            queries=(
                metrics("连衣裙", pv=100, click=10, history_click=30),
                metrics("半身裙", pv=80, click=20, history_click=20),
            ),
        )
    )
    workflow = RecordingWorkflow()
    service = QuerySelectionService(
        source=source,
        store=SqlAlchemyQuerySelectionStore(factory),
    )

    batch, _ = service.select_and_start(
        batch_id="batch-persisted",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=config,
        workflow=workflow,
    )

    with Session(engine) as db:
        stored_batch = db.query(QueryGovernanceSelectionBatch).one()
        candidates = (
            db.query(QueryGovernanceCandidate)
            .order_by(QueryGovernanceCandidate.selection_rank)
            .all()
        )
        tasks = db.query(QueryGovernanceTask).all()

    assert json.loads(stored_batch.config_json)["extract_limit"] == 1
    assert [row.query_text for row in candidates] == ["连衣裙", "半身裙"]
    assert json.loads(candidates[0].matched_rules_json) == list(batch.candidates[0].rules)
    assert json.loads(candidates[0].metrics_json)["search_pv"] == 100
    assert candidates[0].selected is True
    assert candidates[1].selected is False
    assert [(task.query_text, task.query_rank) for task in tasks] == [("连衣裙", 1)]
    assert [spec.query for spec in workflow.specs] == ["连衣裙"]

    restored = service.select_batch(
        batch_id="batch-persisted",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=QuerySelectionConfig(extract_limit=99),
    )
    assert restored.config == config
    assert source.calls == [("atlas", date(2026, 8, 27), config)]

    engine.dispose()


def test_excluded_keywords_filters_exact_match_and_atlas_trailing_gender_suffixes():
    config = QuerySelectionConfig.from_mapping(
        {
            "query_governance_extract_limit": 20,
            "query_governance_candidate_high_pv_limit": 20,
            "query_governance_excluded_keywords": [" 折扣 ", "特价", "", "折扣"],
        }
    )
    assert config.excluded_keywords == ("折扣", "特价")

    dataset = CandidateDataset(
        brand=BrandQueryMetrics(search_uv=1000, click_uv=200, purchase_uv=50),
        queries=(
            metrics("折扣", pv=100, uv=50),
            metrics("折扣 女士", pv=95, uv=48),
            metrics("折扣 男士", pv=90, uv=45),
            metrics("折扣女士", pv=85, uv=42),
            metrics("折扣男士", pv=80, uv=40),
            metrics("男士折扣", pv=75, uv=38),
            metrics("男士 折扣", pv=70, uv=35),
            metrics("连衣裙 女士", pv=60, uv=30),
        ),
    )
    service = QuerySelectionService(
        source=FakeCandidateSource(dataset),
        store=InMemoryQuerySelectionStore(),
    )

    atlas_batch = service.select_batch(
        batch_id="batch-atlas-excluded",
        brand="atlas",
        data_date=date(2026, 8, 27),
        config=config,
    )
    assert [item.query for item in atlas_batch.candidates] == [
        "男士折扣",
        "男士 折扣",
        "连衣裙 女士",
    ]

    meadow_batch = service.select_batch(
        batch_id="batch-meadow-excluded",
        brand="meadow",
        data_date=date(2026, 8, 27),
        config=config,
    )
    assert [item.query for item in meadow_batch.candidates] == [
        "折扣 女士",
        "折扣 男士",
        "折扣女士",
        "折扣男士",
        "男士折扣",
        "男士 折扣",
        "连衣裙 女士",
    ]

