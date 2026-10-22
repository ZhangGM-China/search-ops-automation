"""Extensible, presentation-only page configuration definitions and helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


PAGE_CONFIG_SCHEMA_VERSION = 2

_BRAND_OVERVIEW_METRICS = (
    {"value": "query_count", "label": "Query 数量"},
    {"value": "no_recall_rate", "label": "无召回率"},
    {"value": "pass_rate", "label": "通过率"},
    {"value": "ai_processed_count", "label": "AI 处理"},
    {"value": "pending_count", "label": "待处理"},
    {"value": "irrelevant_query_count", "label": "不相关 Query 数"},
    {"value": "preference_adjusted_query_count", "label": "重排序 Query 数"},
    {"value": "stock_bucket_reduction_product_count", "label": "断码商品数"},
    {"value": "relevance_accuracy", "label": "商品准确率"},
    {"value": "relevance_detection_accuracy", "label": "相关性检测准确率"},
    {"value": "avg_search_recall_rate", "label": "平均召回率"},
    {"value": "token_usage", "label": "Token 消耗"},
    {"value": "duration_seconds", "label": "耗时"},
)
_BRAND_CARD_METRICS = tuple(item for item in _BRAND_OVERVIEW_METRICS if item["value"] not in {"token_usage", "duration_seconds"})

_DRAWER_METRICS = _BRAND_OVERVIEW_METRICS + (
    {"value": "started_at", "label": "开始时间"},
    {"value": "completed_at", "label": "完成时间"},
    {"value": "error_products", "label": "错误商品"},
    {"value": "top_query_limit", "label": "Top Query"},
    {"value": "rapid_pv_growth_query_count", "label": "快速增长"},
    {"value": "new_query_count", "label": "新词 Query"},
    {"value": "ctr_decline_query_count", "label": "CTR 下降 Query"},
    {"value": "cvr_decline_query_count", "label": "CVR 下降 Query"},
    {"value": "model_call_count", "label": "模型调用次数"},
)
_QUERY_METRICS = tuple(
    {"value": key, "label": label}
    for key, label in (
        ("pv", "PV"), ("uv", "UV"), ("ctr", "CTR"), ("cvr", "CVR"),
        ("click_uv", "点击 UV"), ("add_to_cart_uv", "加购 UV"),
        ("purchase_uv", "购买 UV"), ("atc", "ATC"),
    )
)
_QUERY_CARD_METRICS = (
    {"value": "pv", "label": "PV"}, {"value": "uv", "label": "UV"},
    {"value": "ctr", "label": "CTR"}, {"value": "cvr", "label": "CVR"},
    {"value": "duration_seconds", "label": "处理耗时"}, {"value": "token_usage", "label": "Token 消耗"},
)
_YESTERDAY_METRICS = tuple(item for item in _QUERY_METRICS if item["value"] not in {"click_uv", "add_to_cart_uv", "purchase_uv"}) + (
    {"value": "clickUv", "label": "点击 UV"}, {"value": "addToCartUv", "label": "加购 UV"},
    {"value": "purchaseUv", "label": "购买 UV"},
)
_TREND_METRICS = tuple(
    {"value": key, "label": label}
    for key, label in (
        ("uv", "搜索 UV"), ("pv", "PV"), ("clickUv", "点击 UV"),
        ("addToCartUv", "加购 UV"), ("purchaseUv", "购买 UV"),
        ("ctr", "CTR"), ("atc", "ATC"), ("cvr", "CVR"),
    )
)


def _definition(key: str, group: str, group_label: str, label: str, description: str,
                kind: str, default: Any, order: int, *, options: tuple = (),
                minimum: int = 0, maximum: int = 0) -> dict[str, Any]:
    result = {
        "key": key, "groupKey": group, "groupLabel": group_label,
        "label": label, "description": description, "type": kind,
        "scope": "global", "defaultValue": default, "order": order,
    }
    if options:
        result["options"] = [dict(item) for item in options]
    if kind == "metric_list":
        result.update(minItems=minimum, maxItems=maximum)
    return result


PAGE_CONFIG_DEFINITIONS: tuple[dict[str, Any], ...] = (
    _definition("brand_overview.show_global_metrics", "overview_global", "数据看板", "数据看板", "控制品牌总览顶部数据看板是否展示。", "boolean", True, 10),
    _definition("brand_overview.global_metrics", "overview_global", "数据看板", "看板指标", "所选日期全部品牌的合计数据；比率按合计分子与分母计算。", "metric_list", ["query_count", "pass_rate", "pending_count"], 11, options=_BRAND_OVERVIEW_METRICS, minimum=3, maximum=6),
    _definition("brand_overview.card.primary_metric", "overview_card", "品牌卡片", "主指标", "配置品牌卡片右上角主指标。", "metric_list", ["query_count"], 19, options=_BRAND_CARD_METRICS, minimum=1, maximum=1),
    _definition("brand_overview.card.metrics", "overview_card", "品牌卡片", "卡片指标", "配置品牌卡片下方指标，可增减与排序。", "metric_list", ["irrelevant_query_count", "preference_adjusted_query_count", "stock_bucket_reduction_product_count"], 20, options=_BRAND_CARD_METRICS, minimum=2, maximum=4),
    _definition("brand_overview.card.show_runtime", "overview_card", "品牌卡片", "耗时/Token", "显示卡片底部运行数据。", "boolean", True, 21),
    _definition("brand_overview.card.show_operational_data", "overview_card", "品牌卡片", "运营数据", "控制运营数据入口。", "boolean", True, 22),
    _definition("brand_overview.drawer.summary_metrics", "overview_drawer", "运营数据", "基础数据", "可为空，最多展示 20 项。", "metric_list", [], 30, options=_DRAWER_METRICS, maximum=20),
    _definition("brand_overview.drawer.trend_metrics", "overview_drawer", "运营数据", "转化率", "选择图表与表格指标。", "metric_list", [item["value"] for item in _TREND_METRICS], 31, options=_TREND_METRICS, minimum=1, maximum=8),
    _definition("brand_overview.drawer.show_chart", "overview_drawer", "运营数据", "转化率图表", "控制趋势图表。", "boolean", True, 32),
    _definition("brand_overview.drawer.show_table", "overview_drawer", "运营数据", "转化率表单", "控制趋势表格。", "boolean", True, 33),
    _definition("brand_detail.query_card.show_metrics", "detail_list", "Query 列表", "转化指标", "控制 Query 卡片上的统计数据。", "boolean", True, 40),
    _definition("brand_detail.query_card.metrics", "detail_list", "Query 列表", "卡片指标", "选择 2～6 个指标。", "metric_list", ["pv", "uv", "ctr", "cvr"], 41, options=_QUERY_CARD_METRICS, minimum=2, maximum=6),
    _definition("brand_detail.show_execution_overview", "detail_right", "Query 详情", "执行概览", "控制执行概览板块。", "boolean", True, 50),
    _definition("brand_detail.yesterday.show", "detail_right", "Query 详情", "昨日数据", "控制昨日数据及其数据明细入口。", "boolean", True, 51),
    _definition("brand_detail.yesterday.metrics", "detail_right", "Query 详情", "昨日数据指标", "选择 2～6 个指标。", "metric_list", ["pv", "uv", "ctr", "cvr", "atc"], 52, options=_YESTERDAY_METRICS, minimum=2, maximum=6),
    _definition("brand_detail.tabs.show_governance_plans", "detail_right", "Query 详情", "治理方案", "控制治理方案 Tab。", "boolean", True, 53),
    _definition("brand_detail.tabs.show_result_comparison", "detail_right", "Query 详情", "效果对比", "控制效果对比 Tab。", "boolean", True, 54),
    _definition("brand_detail.tabs.show_relevance_rules", "detail_right", "Query 详情", "相关性规则", "控制相关性规则 Tab。", "boolean", True, 55),
)

_DEFINITION_BY_KEY = {item["key"]: item for item in PAGE_CONFIG_DEFINITIONS}


def page_config_values(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    stored = raw_config.get("page_config")
    stored_values = stored.get("values") if isinstance(stored, Mapping) else {}
    if not isinstance(stored_values, Mapping):
        stored_values = {}
    source = dict(stored_values)
    if "brand_overview.card.show_operational_data" not in source and "brand_overview.show_operational_data" in source:
        source["brand_overview.card.show_operational_data"] = source["brand_overview.show_operational_data"]
    if "brand_detail.yesterday.show" not in source and "brand_detail.show_query_metrics" in source:
        source["brand_detail.yesterday.show"] = source["brand_detail.show_query_metrics"]
    if "brand_overview.card.metrics" not in source:
        legacy = [source[key] for key in ("brand_overview.action_metric_1", "brand_overview.action_metric_2", "brand_overview.action_metric_3") if key in source]
        if legacy:
            source["brand_overview.card.metrics"] = legacy
    if "brand_overview.card.primary_metric" not in source and isinstance(source.get("brand_overview.primary_metric"), str):
        source["brand_overview.card.primary_metric"] = [source["brand_overview.primary_metric"]]
    return {definition["key"]: deepcopy(source.get(definition["key"], definition["defaultValue"])) for definition in PAGE_CONFIG_DEFINITIONS}


def page_config_version(raw_config: Mapping[str, Any]) -> int:
    stored = raw_config.get("page_config")
    raw_version = stored.get("version", 0) if isinstance(stored, Mapping) else 0
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        return 0
    return max(0, version)


def page_config_payload(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": PAGE_CONFIG_SCHEMA_VERSION,
        "version": page_config_version(raw_config),
        "values": page_config_values(raw_config),
        "definitions": [deepcopy(item) for item in PAGE_CONFIG_DEFINITIONS],
    }


def validate_page_config_values(values: Any) -> dict[str, Any]:
    if not isinstance(values, Mapping):
        raise ValueError("values must be an object")
    unknown = sorted(set(values) - set(_DEFINITION_BY_KEY))
    if unknown:
        raise ValueError(f"Unknown page config keys: {', '.join(unknown)}")
    normalized: dict[str, Any] = {}
    for key, value in values.items():
        definition = _DEFINITION_BY_KEY[key]
        if definition["type"] == "boolean" and not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        if definition["type"] == "metric_list":
            if not isinstance(value, list):
                raise ValueError(f"{key} must be a list")
            if len(value) < definition["minItems"]:
                raise ValueError(f"{key} requires at least {definition['minItems']} metrics")
            if len(value) > definition["maxItems"]:
                raise ValueError(f"{key} allows at most {definition['maxItems']} metrics")
            options = {item["value"] for item in definition["options"]}
            if any(not isinstance(item, str) or item not in options for item in value):
                raise ValueError(f"{key} has an unsupported option")
            if len(set(value)) != len(value):
                raise ValueError(f"{key} contains duplicate metrics")
        normalized[key] = deepcopy(value)
    return normalized


def apply_page_config_update(raw_config: Mapping[str, Any], values: Mapping[str, Any], *, version: int) -> dict[str, Any]:
    candidate = deepcopy(dict(raw_config))
    current_values = page_config_values(raw_config)
    merged_values = {**current_values, **dict(values)}
    candidate["page_config"] = {
        "schemaVersion": PAGE_CONFIG_SCHEMA_VERSION,
        "version": version,
        "values": merged_values,
    }
    return candidate


__all__ = [
    "PAGE_CONFIG_DEFINITIONS",
    "PAGE_CONFIG_SCHEMA_VERSION",
    "apply_page_config_update",
    "page_config_payload",
    "page_config_values",
    "page_config_version",
    "validate_page_config_values",
]
