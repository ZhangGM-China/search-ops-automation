from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.llm.agents import NoResultDiagnosisAgent, NoResultQueryIntentAgent
from src.persistence import Base, OperationLog, SessionLocal, reset_engine_for_testing
from src.llm.node_config import NODE_REGISTRY
from src.llm.config_store import (
    capture_active_snapshot,
    create_draft,
    publish_draft,
)
from src.no_result.governance import NoResultGovernanceService


def _entries(primary_suffix: str = "") -> dict[str, dict[str, object]]:
    return {
        node_key: {
            "channel": spec.channel,
            "primaryModel": (
                spec.default_primary
                if spec.required_capabilities
                else f"{spec.default_primary}{primary_suffix}"
            ),
            "fallbackModel": spec.default_fallback,
        }
        for node_key, spec in NODE_REGISTRY.items()
    }

def setup_function():
    engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=engine)


def teardown_function():
    from src.persistence import session

    Base.metadata.drop_all(bind=session.engine)
    session.engine.dispose()


def _publish_active_bundle():
    db = SessionLocal()
    try:
        draft = create_draft(db, _entries("-published"))
        publish_draft(db, draft.version, expected_version=None)
        return draft.version
    finally:
        db.close()


def test_runtime_snapshot_reads_active_published_overrides():
    _publish_active_bundle()

    db = SessionLocal()
    try:
        snapshot = capture_active_snapshot(
            db,
            node_keys=("no_result.intent", "no_result.diagnosis"),
        )
    finally:
        db.close()

    assert snapshot.nodes["no_result.intent"].primary_model == "qwen3.6-plus-published"
    assert snapshot.nodes["no_result.intent"].primary_source == "published"
    assert snapshot.nodes["no_result.diagnosis"].primary_model == "qwen3.6-plus-published"


def test_no_result_runtime_cycle_uses_active_published_snapshot():
    _publish_active_bundle()
    service = NoResultGovernanceService()
    service.run_due_retag_tasks = lambda **_kwargs: {"success": 0, "failed": 0}

    captured_snapshots = []
    with patch(
        "src.no_result.governance.capture_active_snapshot",
        side_effect=lambda db, **kwargs: captured_snapshots.append(
            capture_active_snapshot(db, **kwargs)
        ) or captured_snapshots[-1],
    ):
        result = service.run_governance_cycle(limit=0)

    assert result["processed"] == 0
    snapshot = captured_snapshots[0]
    assert snapshot.nodes["no_result.intent"].primary_model == "qwen3.6-plus-published"


def test_no_result_intent_persists_primary_model_audit_without_query():
    entries = _entries("-published")
    entries["no_result.intent"] = {
        "channel": "rd_llm",
        "primaryModel": "deepseek-v3.2",
        "fallbackModel": "qwen3.7-max",
    }
    db = SessionLocal()
    try:
        draft = create_draft(db, entries)
        publish_draft(db, draft.version, expected_version=None)
        snapshot = capture_active_snapshot(db, node_keys=("no_result.intent",))
    finally:
        db.close()

    primary = MagicMock()
    primary.invoke.return_value = MagicMock(
        content='{"decision":"process","reason":"shopping","confidence":0.9}'
    )
    fallback = MagicMock()
    with patch("src.llm.agents.build_rd_chat_model", side_effect=[primary, fallback]):
        result = NoResultQueryIntentAgent(model_config_snapshot=snapshot).classify(
            "red dress",
            "atlas",
        )

    assert result["decision"] == "process"
    fallback.invoke.assert_not_called()
    db = SessionLocal()
    try:
        operation = db.query(OperationLog).filter_by(
            module="NoResultQueryIntentAgent",
            action="No_Result_Query_Intent",
        ).one()
        response = json.loads(operation.response_payload)
    finally:
        db.close()

    assert operation.query_text is None
    assert operation.request_payload is None
    assert response == {
        "modelAudit": {
            "nodeKey": "no_result.intent",
            "channel": "rd_llm",
            "modelName": "deepseek-v3.2",
            "modelRole": "primary",
            "configSource": "published",
        },
    }


def test_no_result_diagnosis_persists_fallback_model_audit_without_query():
    entries = _entries("-published")
    entries["no_result.diagnosis"] = {
        "channel": "rd_llm",
        "primaryModel": "deepseek-v3.2",
        "fallbackModel": "qwen3.7-max",
    }
    db = SessionLocal()
    try:
        draft = create_draft(db, entries)
        publish_draft(db, draft.version, expected_version=None)
        snapshot = capture_active_snapshot(db, node_keys=("no_result.diagnosis",))
    finally:
        db.close()

    primary = MagicMock()
    primary.invoke.side_effect = RuntimeError("primary unavailable")
    fallback = MagicMock()
    fallback.invoke.return_value = MagicMock(
        content='{"outputType":"manual_required","reason":"review","confidence":0.5}'
    )
    with patch("src.llm.agents.build_rd_chat_model", side_effect=[primary, fallback]):
        result = NoResultDiagnosisAgent(model_config_snapshot=snapshot).diagnose(
            "red dress",
            ["dress"],
            {"tags": []},
        )

    assert result["outputType"] == "manual_required"
    db = SessionLocal()
    try:
        operation = db.query(OperationLog).filter_by(
            module="NoResultDiagnosisAgent",
            action="No_Result_Diagnosis",
        ).one()
        response = json.loads(operation.response_payload)
    finally:
        db.close()

    assert operation.query_text is None
    assert operation.request_payload is None
    assert response == {
        "modelAudit": {
            "nodeKey": "no_result.diagnosis",
            "channel": "rd_llm",
            "modelName": "qwen3.7-max",
            "modelRole": "fallback",
            "configSource": "published",
        },
    }


def test_no_result_intent_does_not_select_model_when_credentials_are_missing():
    entries = _entries("-published")
    entries["no_result.intent"] = {
        "channel": "rd_llm",
        "primaryModel": "deepseek-v3.2",
        "fallbackModel": "qwen3.7-max",
    }
    db = SessionLocal()
    try:
        draft = create_draft(db, entries)
        publish_draft(db, draft.version, expected_version=None)
        snapshot = capture_active_snapshot(db, node_keys=("no_result.intent",))
    finally:
        db.close()

    with patch("src.llm.agents.build_rd_chat_model", return_value=None):
        result = NoResultQueryIntentAgent(model_config_snapshot=snapshot).classify(
            "red dress",
            "atlas",
        )

    assert result["decision"] == "process"
    db = SessionLocal()
    try:
        operation = db.query(OperationLog).filter_by(
            module="NoResultQueryIntentAgent",
            action="No_Result_Query_Intent",
        ).one()
        response = json.loads(operation.response_payload)
    finally:
        db.close()

    assert operation.status == "failed"
    assert response == {
        "modelAudit": {
            "nodeKey": "no_result.intent",
            "channel": "rd_llm",
            "modelName": None,
            "modelRole": None,
            "configSource": None,
        },
    }


def test_no_result_diagnosis_does_not_select_model_when_all_attempts_fail():
    entries = _entries("-published")
    entries["no_result.diagnosis"] = {
        "channel": "rd_llm",
        "primaryModel": "deepseek-v3.2",
        "fallbackModel": "qwen3.7-max",
    }
    db = SessionLocal()
    try:
        draft = create_draft(db, entries)
        publish_draft(db, draft.version, expected_version=None)
        snapshot = capture_active_snapshot(db, node_keys=("no_result.diagnosis",))
    finally:
        db.close()

    primary = MagicMock()
    primary.invoke.side_effect = RuntimeError("primary unavailable")
    fallback = MagicMock()
    fallback.invoke.side_effect = RuntimeError("fallback unavailable")
    with patch("src.llm.agents.build_rd_chat_model", side_effect=[primary, fallback]):
        result = NoResultDiagnosisAgent(model_config_snapshot=snapshot).diagnose(
            "red dress",
            ["dress"],
            {"tags": []},
        )

    assert result["outputType"] == "manual_required"
    db = SessionLocal()
    try:
        operation = db.query(OperationLog).filter_by(
            module="NoResultDiagnosisAgent",
            action="No_Result_Diagnosis",
        ).one()
        response = json.loads(operation.response_payload)
    finally:
        db.close()

    assert operation.status == "failed"
    assert response == {
        "modelAudit": {
            "nodeKey": "no_result.diagnosis",
            "channel": "rd_llm",
            "modelName": None,
            "modelRole": None,
            "configSource": None,
        },
    }
