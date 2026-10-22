from types import SimpleNamespace

from sqlalchemy import create_engine

from src.api.shared import BRAND_CONFIG_EDITABLE_KEYS
from src.config import DEFAULT_BRAND_BUSINESS_CONFIG, get_brand_business_config


def test_kg_agent_timeout_is_brand_configurable_and_defaults_to_1200_seconds():
    assert DEFAULT_BRAND_BUSINESS_CONFIG["query_governance_kg_agent_timeout_seconds"] == 1200
    assert "query_governance_kg_agent_timeout_seconds" in BRAND_CONFIG_EDITABLE_KEYS


def test_legacy_exact_query_ranking_is_not_an_editable_runtime_key():
    assert "exact_query_ranking" not in BRAND_CONFIG_EDITABLE_KEYS


def test_retired_metric_drop_thresholds_are_not_part_of_brand_config_contract():
    assert "top_query_metric_drop_threshold_percent" not in BRAND_CONFIG_EDITABLE_KEYS
    assert "top_query_metric_drop_thresholds" not in BRAND_CONFIG_EDITABLE_KEYS
    effective = get_brand_business_config("atlas")
    assert "top_query_metric_drop_threshold_percent" not in effective
    assert "top_query_metric_drop_thresholds" not in effective


def test_effective_tool_modes_drop_retired_exact_ranking_key():
    config = get_brand_business_config("atlas")
    assert "exact_query_ranking" not in config["top_query_tool_modes"]
    assert config["top_query_tool_modes"]["query_reorder"] == "auto"


def test_atlas_enables_dynamic_relevance_bucket_without_changing_global_default():
    assert "Dynamic" not in DEFAULT_BRAND_BUSINESS_CONFIG[
        "query_governance_relevance_buckets"
    ]
    assert "Dynamic" in get_brand_business_config("atlas")[
        "query_governance_relevance_buckets"
    ]


def test_production_composition_passes_configured_kg_agent_timeout(monkeypatch):
    import src.query_governance.composition as composition

    calls = []

    class Runtime:
        def invoke(self, messages, **kwargs):  # pragma: no cover - construction only
            raise AssertionError("composition test must not invoke a model")

    def runtime_factory(node_key, snapshot, **kwargs):
        calls.append((node_key, kwargs))
        return Runtime()

    class Operational:
        kg_client = object()

        def search_products_with_total(self, brand, query, *, page_size=16, page_num=1):
            return {"success": True, "products": [], "total_count": 0, "result_complete": True}

    monkeypatch.setattr(
        composition,
        "get_brand_business_config",
        lambda brand: {"query_governance_kg_agent_timeout_seconds": 1200},
    )
    monkeypatch.setattr(composition, "KnowledgeGraphReadTools", lambda client, **kwargs: object())

    composition.build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=Operational(),
        model_config_snapshot=SimpleNamespace(version=17, nodes={}),
        model_runtime_factory=runtime_factory,
        behavior_engine=create_engine("sqlite:///:memory:"),
    )

    kg_call = next(kwargs for node_key, kwargs in calls if node_key == "query_governance.kg_agent")
    assert kg_call["timeout"] == 1200
    assert kg_call["max_completion_tokens"] is None


def test_query_reorder_mode_disables_new_mainline_reorder(monkeypatch):
    import src.query_governance.composition as composition

    class Operational:
        def search_products_with_total(self, brand, query, *, page_size=16, page_num=1):
            return {"success": True, "products": [], "total_count": 0, "result_complete": True}

    monkeypatch.setattr(
        composition,
        "get_brand_business_config",
        lambda brand: {
            "query_governance_reorder_enabled": True,
            "top_query_tool_modes": {"query_reorder": "disabled"},
        },
    )
    workflow = composition.build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=Operational(),
        model_config_snapshot=SimpleNamespace(version=17, nodes={}),
        model_runtime_factory=lambda *args, **kwargs: SimpleNamespace(),
        behavior_engine=create_engine("sqlite:///:memory:"),
    )

    assert workflow.reorder_enabled is False


def test_product_blacklist_mode_is_wired_into_relevance_workflow(monkeypatch):
    import src.query_governance.composition as composition

    class Operational:
        def search_products_with_total(self, brand, query, *, page_size=16, page_num=1):
            return {"success": True, "products": [], "total_count": 0, "result_complete": True}

    monkeypatch.setattr(
        composition,
        "get_brand_business_config",
        lambda brand: {"top_query_tool_modes": {"product_blacklist": "human_approval"}},
    )
    workflow = composition.build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=Operational(),
        model_config_snapshot=SimpleNamespace(version=17, nodes={}),
        model_runtime_factory=lambda *args, **kwargs: SimpleNamespace(),
        behavior_engine=create_engine("sqlite:///:memory:"),
    )

    assert workflow._relevance_blacklist_execution_mode == "human_approval"
