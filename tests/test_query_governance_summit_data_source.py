from __future__ import annotations


def test_summit_query_governance_candidates_use_shared_rds(monkeypatch):
    from src.query_governance import scheduler as scheduler_module

    calls: list[str] = []

    class _Extractor:
        def __init__(self, *, brand_type: str) -> None:
            calls.append(brand_type)
            self.engine = object()

    monkeypatch.setattr(scheduler_module, "QueryExtractor", _Extractor)

    scheduler_module.QueryGovernanceCandidateSource()._source("summit")

    assert calls == ["rds"]


def test_summit_experiment_metrics_use_shared_rds(monkeypatch):
    from src.integrations import query_extractor as extractor_module
    from src.query_governance.experiments import (
        QueryExtractorExperimentMetricsProvider,
    )

    calls: list[str] = []

    class _Extractor:
        def __init__(self, *, brand_type: str) -> None:
            calls.append(brand_type)

    monkeypatch.setattr(extractor_module, "QueryExtractor", _Extractor)

    QueryExtractorExperimentMetricsProvider._default_extractor("summit")

    assert calls == ["rds"]
