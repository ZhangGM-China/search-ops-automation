"""Token accounting must survive aggregation without inventing missing usage."""

from types import SimpleNamespace

import pytest

from src.query_governance.model_runtime import _token_usage
from src.query_governance.workflow import QueryGovernanceWorkflow


@pytest.mark.parametrize("usage", [{}, {"input_tokens": 10}, {"total_tokens": -1}, {"total_tokens": True}])
def test_missing_or_invalid_usage_is_not_zero(usage):
    assert _token_usage(SimpleNamespace(usage_metadata=usage)) is None


def test_empty_normalized_usage_falls_back_to_provider_usage():
    response = SimpleNamespace(usage_metadata={}, response_metadata={"token_usage": {"total_tokens": 23}})
    assert _token_usage(response) == 23


def test_relevance_merge_preserves_all_unique_call_audits():
    aggregate = {"product_count": 0, **{key: [] for key in (
        "reused_product_ids", "requested_product_ids", "detected_product_ids",
        "relevant_product_ids", "blacklist_candidates", "warnings",
    )}}
    first = {"audit": {"requestId": "call-1", "tokenUsage": 100, "batchQueryCount": 2}}
    second = {"audit": {"requestId": "call-2", "tokenUsage": None, "batchQueryCount": 1}}
    for payload in (first, second, first):
        QueryGovernanceWorkflow._merge_relevance_result(aggregate, payload)
    assert aggregate["modelCallAudits"] == [first["audit"], second["audit"]]


def test_workflow_checkpoint_and_read_api_preserve_usage_on_resume():
    from datetime import date
    from sqlalchemy.orm import sessionmaker
    from test_query_governance_checkpoint import build_store, Search, Runner, Actions, MODEL_CONFIG_SNAPSHOT
    from src.query_governance.models import NodeResult, QuerySpec
    from src.query_governance.read_api import QueryGovernanceReadService

    class Relevance:
        def run(self, spec, context):
            return NodeResult.executed({"modelCallAudits": [
                {"requestId": "persisted", "tokenUsage": 37, "batchQueryCount": 1},
                {"requestId": "missing", "tokenUsage": None, "batchQueryCount": 1},
            ]})

    engine, store = build_store()
    try:
        workflow = QueryGovernanceWorkflow(search=Search(), store=store, checkpoint=store,
            runners={"recall": Runner("recall", []), "reorder": Runner("reorder", []), "relevance": Relevance()},
            action_gateway=Actions(), model_config_snapshot=MODEL_CONFIG_SNAPSHOT)
        for _ in range(2):
            workflow.run_batch([QuerySpec("q1", "atlas", "裙子")], batch_id="token-persist", data_date=date(2026, 9, 8))
            detail = QueryGovernanceReadService(sessionmaker(bind=engine)).brand_detail("atlas", data_date=date(2026, 9, 8))
            assert detail["batch"]["telemetry"]["relevanceTokenUsage"] == 37
            usage = detail["queries"][0]["telemetry"]
            assert usage["relevanceTokenUsage"] == 37
            assert usage["relevanceTokenUsageMissing"] is True
    finally:
        engine.dispose()
