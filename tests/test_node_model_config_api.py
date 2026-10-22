from __future__ import annotations

import datetime
import json

import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient

from src.api import app, get_current_user
from src.persistence import (
    ACTION_MANAGE_PLATFORM_CONFIG,
    ActionIntent,
    ActionReadback,
    ActionTerminalFact,
    Base,
    NodeModelConfigEntry,
    NodeModelConfigVersion,
    PLATFORM_ADMIN_ROLE,
    SessionLocal,
    User,
    reset_engine_for_testing,
)
from src.llm.node_config import NODE_REGISTRY


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


@pytest.fixture()
def client():
    engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=engine)
    admin = User(
        id=101,
        username="model_config_admin",
        role=PLATFORM_ADMIN_ROLE,
        is_admin=1,
        is_active=1,
        page_permissions="[]",
        brand_permissions="[]",
        action_permissions=json.dumps([ACTION_MANAGE_PLATFORM_CONFIG]),
    )
    db = SessionLocal()
    db.add(admin)
    db.commit()
    db.close()
    admin_fact = User(
        id=101,
        username="model_config_admin",
        role=PLATFORM_ADMIN_ROLE,
        is_admin=1,
        is_active=1,
        page_permissions="[]",
        brand_permissions="[]",
        action_permissions=json.dumps([ACTION_MANAGE_PLATFORM_CONFIG]),
    )
    app.dependency_overrides[get_current_user] = lambda: admin_fact
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_platform_admin_reads_all_nodes_and_current_effective_models(client):
    response = client.get("/api/admin/node-model-config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["activeVersion"] is None
    assert {node["nodeKey"] for node in payload["nodes"]} == set(NODE_REGISTRY)
    relevance = next(
        node
        for node in payload["nodes"]
        if node["nodeKey"] == "query_governance.relevance_multimodal"
    )
    assert relevance["requiredCapabilities"] == ["image"]
    node = next(node for node in payload["nodes"] if node["nodeKey"] == "no_result.intent")
    assert node == {
        "nodeKey": "no_result.intent",
        "channel": "rd_llm",
        "primaryModel": "qwen3.6-plus",
        "fallbackModel": None,
        "primarySource": "default",
        "fallbackSource": "default",
        "requiredCapabilities": [],
    }


def test_platform_admin_rejects_legacy_fastclaw_assistant_bundle_after_pi_agent_migration(client):
    db = SessionLocal()
    try:
        version = NodeModelConfigVersion(
            version=1,
            status="published",
            active_slot="active",
            created_at=datetime.datetime(2026, 9, 1),
            published_at=datetime.datetime(2026, 9, 1),
        )
        db.add(version)
        db.flush()
        db.execute(text("PRAGMA ignore_check_constraints = ON"))
        db.add_all([
            NodeModelConfigEntry(
                version_id=version.id,
                node_key=node_key,
                channel="fastclaw" if node_key == "assistant.chat" else spec.channel,
                primary_model=spec.default_primary,
                fallback_model=spec.default_fallback,
            )
            for node_key, spec in NODE_REGISTRY.items()
        ])
        db.commit()
    finally:
        db.close()

    response = client.get("/api/admin/node-model-config")

    assert response.status_code == 409
    assert "retired runtime channel" in response.json()["detail"]


def test_non_platform_admin_cannot_read_or_mutate_node_model_config(client):
    restricted = User(
        id=102,
        username="brand_admin",
        role="brand_admin",
        is_admin=0,
        is_active=1,
        page_permissions="[]",
        brand_permissions='["atlas"]',
        action_permissions=json.dumps([ACTION_MANAGE_PLATFORM_CONFIG]),
    )
    app.dependency_overrides[get_current_user] = lambda: restricted

    assert client.get("/api/admin/node-model-config").status_code == 403
    assert client.post(
        "/api/admin/node-model-config/drafts",
        json={"requestId": "model-config-forbidden", "entries": _entries()},
    ).status_code == 403


def test_create_draft_is_atomic_audited_and_idempotently_replayed(client):
    request = {
        "requestId": "model-config-draft-1",
        "entries": _entries("-deepseek"),
        "changeNote": "Switch the next requests to the new model bundle",
    }

    first = client.post("/api/admin/node-model-config/drafts", json=request)
    replay = client.post("/api/admin/node-model-config/drafts", json=request)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["status"] == "success"
    assert first.json()["version"] == 1
    assert first.json()["state"] == "draft"
    assert len(first.json()["nodes"]) == len(NODE_REGISTRY)

    db = SessionLocal()
    try:
        assert db.query(NodeModelConfigVersion).count() == 1
        intent = db.query(ActionIntent).filter_by(idempotency_key=request["requestId"]).one()
        assert intent.action_type == "admin.node_model_config.create_draft"
        assert intent.permission == ACTION_MANAGE_PLATFORM_CONFIG
        assert db.query(ActionReadback).filter_by(intent_id=intent.id).count() == 1
        assert db.query(ActionTerminalFact).filter_by(intent_id=intent.id).count() == 1
    finally:
        db.close()


def test_same_request_id_with_different_model_bundle_conflicts(client):
    request = {"requestId": "model-config-draft-conflict", "entries": _entries("-one")}
    assert client.post("/api/admin/node-model-config/drafts", json=request).status_code == 200
    request["entries"] = _entries("-two")

    response = client.post("/api/admin/node-model-config/drafts", json=request)

    assert response.status_code == 409


def test_publish_accepts_current_rd_multimodal_model_configuration(client):
    entries = _entries()
    entries["query_governance.relevance_multimodal"].update(
        {
            "primaryModel": "qwen3.7-plus",
            "fallbackModel": "qwen3.6-plus",
        }
    )
    draft = client.post(
        "/api/admin/node-model-config/drafts",
        json={"requestId": "model-config-multimodal-current", "entries": entries},
    )
    assert draft.status_code == 200

    published = client.post(
        f"/api/admin/node-model-config/versions/{draft.json()['version']}/publish",
        json={
            "requestId": "model-config-multimodal-current-publish",
            "expectedVersion": None,
        },
    )

    assert published.status_code == 200
    assert published.json()["activeVersion"] == draft.json()["version"]


def test_custom_multimodal_models_can_be_published_and_read_back(client):
    entries = _entries()
    entries["query_governance.relevance_multimodal"].update(
        {
            "primaryModel": "deepseek-v4.1-flash-1017",
            "fallbackModel": "custom-vision-fallback",
        }
    )

    response = client.post(
        "/api/admin/node-model-config/drafts",
        json={"requestId": "model-config-custom-multimodal", "entries": entries},
    )

    assert response.status_code == 200
    relevance = next(
        node
        for node in response.json()["nodes"]
        if node["nodeKey"] == "query_governance.relevance_multimodal"
    )
    assert relevance["primaryModel"] == "deepseek-v4.1-flash-1017"
    assert relevance["fallbackModel"] == "custom-vision-fallback"

    version = response.json()["version"]
    published = client.post(
        f"/api/admin/node-model-config/versions/{version}/publish",
        json={"requestId": "model-config-custom-publish", "expectedVersion": None},
    )
    assert published.status_code == 200
    active = client.get("/api/admin/node-model-config")
    assert active.status_code == 200
    assert active.json()["activeVersion"] == version
    active_relevance = next(
        node for node in active.json()["nodes"]
        if node["nodeKey"] == "query_governance.relevance_multimodal"
    )
    assert active_relevance["primaryModel"] == "deepseek-v4.1-flash-1017"
    assert active_relevance["fallbackModel"] == "custom-vision-fallback"
    assert active_relevance["requiredCapabilities"] == ["image"]


def test_publish_accepts_current_rd_flash_models_for_all_nodes(client):
    entries = _entries()
    for entry in entries.values():
        entry["primaryModel"] = "deepseek-v4.1-flash"
        entry["fallbackModel"] = "qwen3.8-flash"

    draft = client.post(
        "/api/admin/node-model-config/drafts",
        json={"requestId": "model-config-flash-draft", "entries": entries},
    )
    assert draft.status_code == 200

    published = client.post(
        f"/api/admin/node-model-config/versions/{draft.json()['version']}/publish",
        json={
            "requestId": "model-config-flash-publish",
            "expectedVersion": None,
        },
    )

    assert published.status_code == 200
    assert published.json()["activeVersion"] == draft.json()["version"]


def test_publish_and_rollback_require_cas_and_return_authoritative_active_bundle(client):
    first = client.post(
        "/api/admin/node-model-config/drafts",
        json={"requestId": "model-config-draft-first", "entries": _entries()},
    ).json()
    publish_first = client.post(
        f"/api/admin/node-model-config/versions/{first['version']}/publish",
        json={"requestId": "model-config-publish-first", "expectedVersion": None},
    )
    second = client.post(
        "/api/admin/node-model-config/drafts",
        json={"requestId": "model-config-draft-second", "entries": _entries("-deepseek")},
    ).json()
    stale = client.post(
        f"/api/admin/node-model-config/versions/{second['version']}/publish",
        json={"requestId": "model-config-publish-stale", "expectedVersion": None},
    )
    publish_second = client.post(
        f"/api/admin/node-model-config/versions/{second['version']}/publish",
        json={
            "requestId": "model-config-publish-second",
            "expectedVersion": first["version"],
        },
    )
    rollback = client.post(
        "/api/admin/node-model-config/rollback",
        json={
            "requestId": "model-config-rollback-first",
            "targetVersion": first["version"],
            "expectedVersion": second["version"],
        },
    )

    assert publish_first.status_code == 200
    assert publish_first.json()["activeVersion"] == first["version"]
    assert stale.status_code == 409
    assert publish_second.status_code == 200
    assert publish_second.json()["activeVersion"] == second["version"]
    assert rollback.status_code == 200
    assert rollback.json()["activeVersion"] > second["version"]
    assert rollback.json()["rolledBackFromVersion"] == second["version"]
    assert rollback.json()["rolledBackToVersion"] == first["version"]

    db = SessionLocal()
    try:
        publish_intent = db.query(ActionIntent).filter_by(
            idempotency_key="model-config-publish-second"
        ).one()
        publish_readback = db.query(ActionReadback).filter_by(
            intent_id=publish_intent.id
        ).one()
        publish_fact = json.loads(publish_readback.observed_payload)
        assert publish_fact["activeVersion"] == second["version"]
        assert len(publish_fact["nodes"]) == len(NODE_REGISTRY)
        assert len(publish_readback.fact_hash) == 64

        rollback_intent = db.query(ActionIntent).filter_by(
            idempotency_key="model-config-rollback-first"
        ).one()
        rollback_readback = db.query(ActionReadback).filter_by(
            intent_id=rollback_intent.id
        ).one()
        rollback_fact = json.loads(rollback_readback.observed_payload)
        assert rollback_fact["activeVersion"] == rollback.json()["activeVersion"]
        assert len(rollback_fact["nodes"]) == len(NODE_REGISTRY)
        assert len(rollback_readback.fact_hash) == 64
    finally:
        db.close()

    current = client.get("/api/admin/node-model-config").json()
    assert current["activeVersion"] == rollback.json()["activeVersion"]
    node = next(node for node in current["nodes"] if node["nodeKey"] == "no_result.intent")
    assert node["primaryModel"] == "qwen3.6-plus"
    assert node["primarySource"] == "published"
