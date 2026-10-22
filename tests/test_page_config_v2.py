from __future__ import annotations

from src.page_config import (
    apply_page_config_update,
    page_config_payload,
    page_config_values,
    validate_page_config_values,
)


def test_page_config_v2_registers_overview_and_detail_modules():
    payload = page_config_payload({})
    definitions = {item["key"]: item for item in payload["definitions"]}

    assert payload["schemaVersion"] == 2
    assert definitions["brand_overview.global_metrics"]["type"] == "metric_list"
    assert definitions["brand_overview.global_metrics"]["minItems"] == 3
    assert definitions["brand_overview.global_metrics"]["maxItems"] == 6
    assert definitions["brand_overview.card.primary_metric"]["minItems"] == 1
    assert definitions["brand_overview.card.primary_metric"]["maxItems"] == 1
    assert payload["values"]["brand_overview.card.primary_metric"] == ["query_count"]
    assert definitions["brand_overview.show_global_metrics"]["label"] == "数据看板"
    assert definitions["brand_overview.global_metrics"]["label"] == "看板指标"
    assert definitions["brand_overview.drawer.summary_metrics"]["maxItems"] == 20
    assert definitions["brand_detail.query_card.metrics"]["minItems"] == 2
    assert definitions["brand_detail.yesterday.metrics"]["maxItems"] == 6
    assert definitions["brand_detail.query_card.show_metrics"]["groupLabel"] == "Query 列表"
    assert definitions["brand_detail.tabs.show_governance_plans"]["groupKey"] == "detail_right"
    assert definitions["brand_detail.tabs.show_governance_plans"]["groupLabel"] == "Query 详情"
    assert "brand_overview.show_operational_data" not in definitions
    assert "brand_detail.show_query_metrics" not in definitions


def test_page_config_v2_uses_compact_labels_and_default_global_dashboard_visibility():
    payload = page_config_payload({})
    definitions = {item["key"]: item for item in payload["definitions"]}

    assert payload["values"]["brand_overview.show_global_metrics"] is True
    assert definitions["brand_overview.card.show_runtime"]["label"] == "耗时/Token"
    assert definitions["brand_overview.card.show_operational_data"]["label"] == "运营数据"
    assert definitions["brand_overview.drawer.summary_metrics"]["label"] == "基础数据"
    assert definitions["brand_overview.drawer.trend_metrics"]["label"] == "转化率"
    assert definitions["brand_overview.drawer.show_chart"]["label"] == "转化率图表"
    assert definitions["brand_overview.drawer.show_table"]["label"] == "转化率表单"
    assert definitions["brand_detail.query_card.show_metrics"]["label"] == "转化指标"
    assert definitions["brand_detail.tabs.show_relevance_rules"]["label"] == "相关性规则"


def test_page_config_v2_migrates_legacy_values_and_validates_metric_list_bounds():
    legacy = {
        "page_config": {
            "schemaVersion": 1,
            "values": {
                "brand_overview.show_operational_data": False,
                "brand_detail.show_query_metrics": False,
                "brand_overview.action_metric_1": "pending_count",
                "brand_overview.action_metric_2": "ai_processed_count",
                "brand_overview.action_metric_3": "query_count",
                "brand_overview.primary_metric": "pass_rate",
            },
        }
    }
    values = page_config_values(legacy)

    assert values["brand_overview.card.show_operational_data"] is False
    assert values["brand_detail.yesterday.show"] is False
    assert values["brand_overview.card.metrics"] == [
        "pending_count",
        "ai_processed_count",
        "query_count",
    ]
    assert values["brand_overview.card.primary_metric"] == ["pass_rate"]
    assert validate_page_config_values({"brand_overview.card.metrics": ["query_count", "pass_rate"]}) == {
        "brand_overview.card.metrics": ["query_count", "pass_rate"]
    }

    try:
        validate_page_config_values({"brand_overview.global_metrics": ["query_count", "pass_rate"]})
    except ValueError as exc:
        assert "at least 3" in str(exc)
    else:
        raise AssertionError("expected metric list lower bound validation")


def test_page_config_v2_updates_lists_without_dropping_unsubmitted_defaults():
    candidate = apply_page_config_update(
        {},
        {"brand_overview.drawer.summary_metrics": ["query_count", "pass_rate"]},
        version=1,
    )
    values = page_config_values(candidate)

    assert values["brand_overview.drawer.summary_metrics"] == ["query_count", "pass_rate"]
    assert values["brand_overview.card.show_runtime"] is True


def test_page_config_v2_rejects_invalid_metric_lists():
    invalid_values = (
        {"brand_overview.card.primary_metric": []},
        {"brand_overview.card.primary_metric": ["query_count", "pass_rate"]},
        {"brand_overview.global_metrics": "query_count"},
        {"brand_overview.global_metrics": ["query_count", "pass_rate"]},
        {"brand_overview.card.metrics": ["query_count", "query_count"]},
        {"brand_overview.drawer.summary_metrics": ["not_a_metric"]},
        {
            "brand_overview.drawer.summary_metrics": [
                item["value"]
                for item in next(
                    definition
                    for definition in page_config_payload({})["definitions"]
                    if definition["key"] == "brand_overview.drawer.summary_metrics"
                )["options"]
            ] * 2
        },
    )
    for values in invalid_values:
        try:
            validate_page_config_values(values)
        except ValueError:
            continue
        raise AssertionError(f"expected metric list rejection: {values}")
