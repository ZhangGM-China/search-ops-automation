import json
from datetime import date

import pytest

from test_query_governance_read_api import _database
from src.query_governance.models import QueryGovernanceTask, QueryGovernanceStepStatus, QueryGovernanceWorkflowRun
from src.query_governance.read_api import QueryGovernanceReadService, _token_totals
from src.api.contracts import QueryGovernanceQueryTelemetry


def test_relevance_usage_multiround_shared_missing_cached_and_legacy():
    engine, factory = _database()
    shared = {"requestId": "shared", "tokenUsage": 201, "batchQueryCount": 2}
    payloads = {
        "q1": {"modelCallAudits": [shared, shared, {"requestId": "round2", "tokenUsage": 30, "batchQueryCount": 1}]},
        "q2": {"modelCallAudits": [shared, {"requestId": "failed", "tokenUsage": None, "batchQueryCount": 1}]},
        "cache": {"modelCallAudits": []},
        "legacy": {"detected_product_ids": ["p1"]},
        "fallback": {"modelCallAudits": [{"requestId": "fallback", "tokenUsage": 5, "batchQueryCount": 1, "tokenUsageMissing": True}]},
    }
    try:
        with factory() as session:
            session.add(QueryGovernanceWorkflowRun(batch_id="tokens", status="completed"))
            session.flush()
            for rank, (query, payload) in enumerate(payloads.items()):
                task = QueryGovernanceTask(batch_id="tokens", brand="atlas", data_date=date(2026, 9, 8), query_text=query, query_rank=rank, status="completed")
                session.add(task)
                session.flush()
                session.add(QueryGovernanceStepStatus(task_id=task.id, node_key="relevance", status="executed", result_json=json.dumps({"result": payload, "telemetry": {"executionSeconds": 5}})))
            session.commit()
        detail = QueryGovernanceReadService(factory).brand_detail("atlas", data_date=date(2026, 9, 8))
        batch = detail["batch"]["telemetry"]
        assert batch["tokenUsage"] == 236
        assert batch["relevanceTokenUsage"] == 236
        queries = {q["queryText"]: q["telemetry"] for q in detail["queries"]}
        assert queries["q1"]["relevanceTokenUsage"] == 130.5
        assert queries["q1"]["tokenUsageMissing"] is False
        assert queries["q2"]["relevanceTokenUsage"] == 100.5
        assert queries["q2"]["relevanceTokenUsageMissing"] is True
        assert queries["cache"]["relevanceTokenUsage"] == 0
        assert queries["cache"]["relevanceTokenUsageMissing"] is False
        assert queries["legacy"]["relevanceTokenUsage"] is None
        assert queries["legacy"]["relevanceTokenUsageMissing"] is True
        assert queries["fallback"]["relevanceTokenUsage"] == 5
        assert queries["fallback"]["relevanceTokenUsageMissing"] is True
    finally:
        engine.dispose()


@pytest.mark.parametrize("tokens", [None, True, -1, "100", float("nan"), float("inf"), 1.5])
def test_invalid_usage_is_missing_not_a_numeric_token(tokens):
    result = _token_totals([{"nodeKey": "relevance", "result": {"modelCallAudits": [{"requestId": "unknown", "tokenUsage": tokens}]}}], allocate=True)
    assert result == {"tokenUsage": 0, "tokenUsageMissing": True, "relevanceTokenUsage": None, "relevanceTokenUsageMissing": True}


def test_legacy_preference_and_nested_relevance_stay_compatible():
    nodes = [
        {"nodeKey": "reorder", "result": {"preference_audit": {"tokenUsage": 50}}},
        {"nodeKey": "relevance", "result": {"result": {"result": {"audit": {"tokenUsage": 200}}}}, "telemetry": {"batchKey": "old-shared", "batchQueryCount": 2}},
    ]
    assert _token_totals(nodes, allocate=True) == {"tokenUsage": 150, "tokenUsageMissing": False, "relevanceTokenUsage": 100, "relevanceTokenUsageMissing": False}


def test_serialization_preserves_fractional_allocation_and_missing_flags():
    payload = QueryGovernanceQueryTelemetry(tokenUsage=130.5, relevanceTokenUsage=100.5, tokenUsageMissing=True, relevanceTokenUsageMissing=True).model_dump()
    assert payload["tokenUsage"] == 130.5
    assert payload["relevanceTokenUsage"] == 100.5
    assert payload["tokenUsageMissing"] is True
    assert payload["relevanceTokenUsageMissing"] is True


@pytest.mark.parametrize("status", ["pending", "running", "skipped"])
def test_unexecuted_relevance_does_not_report_usage_missing(status):
    usage = _token_totals([{"nodeKey": "relevance", "status": status, "result": {}}], allocate=True)
    assert usage["relevanceTokenUsageMissing"] is False
    assert usage["tokenUsageMissing"] is False


@pytest.mark.parametrize("result", [{}, None])
def test_explicit_zero_model_calls_are_not_unknown_usage(result):
    usage = _token_totals([{"nodeKey": "relevance", "status": "executed", "result": result, "telemetry": {"modelCallCount": 0}}], allocate=True)
    assert usage["relevanceTokenUsage"] == 0
    assert usage["relevanceTokenUsageMissing"] is False


def test_nested_preference_call_ledger_retains_shared_allocation_and_missing_attempts():
    node = {"nodeKey": "reorder", "result": {"preference_audit": {
        "batchKey": "preference-shared", "batchQueryCount": 2, "tokenUsage": 200,
        "modelCallAudits": [{"requestId": "attempt1", "tokenUsage": None}, {"requestId": "attempt2", "tokenUsage": 200}],
    }}}
    usage = _token_totals([node], allocate=True)
    assert usage["tokenUsage"] == 100
    assert usage["tokenUsageMissing"] is True
    assert usage["relevanceTokenUsageMissing"] is False
    assert _token_totals([node, node], allocate=False)["tokenUsage"] == 200


def test_query_detail_exposes_stage_token_usage_from_node_audit():
    engine, factory = _database()
    try:
        with factory() as session:
            session.add(QueryGovernanceWorkflowRun(batch_id="node-token", status="completed"))
            session.flush()
            task = QueryGovernanceTask(
                batch_id="node-token", brand="atlas", data_date=date(2026, 9, 8),
                query_text="q", query_rank=1, status="completed",
            )
            session.add(task)
            session.flush()
            session.add(QueryGovernanceStepStatus(
                task_id=task.id,
                node_key="relevance",
                status="executed",
                duration_seconds=2.5,
                result_json=json.dumps({
                    "result": {"modelCallAudits": [{"requestId": "call-1", "tokenUsage": 42}]},
                    "telemetry": {"executionSeconds": 2.5},
                }),
            ))
            session.commit()
            task_id = task.id

        node = next(item for item in QueryGovernanceReadService(factory).query_detail(task_id)["nodes"] if item["nodeKey"] == "relevance")
        assert node["durationSeconds"] == 2.5
        assert node["tokenUsage"] == 42
        assert node["tokenUsageMissing"] is False
    finally:
        engine.dispose()
