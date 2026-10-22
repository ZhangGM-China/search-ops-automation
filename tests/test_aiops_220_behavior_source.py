from __future__ import annotations

from types import SimpleNamespace


def test_production_composition_uses_external_behavior_engine_by_default(monkeypatch):
    import src.query_governance.composition as composition

    external_engine = object()
    monkeypatch.setattr(
        composition,
        "build_behavior_evidence_engine",
        lambda: external_engine,
    )

    workflow = composition.build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=SimpleNamespace(),
        model_config_snapshot=SimpleNamespace(version=1, nodes={}),
        model_runtime_factory=lambda *args, **kwargs: SimpleNamespace(),
    )

    assert workflow.preference_provider.evidence.source.engine is external_engine


def test_production_composition_maps_all_supported_brands_to_shared_table(monkeypatch):
    import src.query_governance.composition as composition

    monkeypatch.setattr(composition, "build_behavior_evidence_engine", lambda: object())
    workflow = composition.build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=SimpleNamespace(),
        model_config_snapshot=SimpleNamespace(version=1, nodes={}),
        model_runtime_factory=lambda *args, **kwargs: SimpleNamespace(),
    )

    specs = workflow.preference_provider.evidence.source.specs
    assert set(specs) == {"atlas", "harbor", "summit", "riviera"}
    assert {spec.table_name for spec in specs.values()} == {
        "industry_knowledge_base.dws_brand_query_product_funnel_daily"
    }
