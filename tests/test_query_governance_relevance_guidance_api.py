from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from src.api import app, get_current_user
from src.api.application import query_governance
from src.api.application import dashboard as dashboard_module
from src.persistence import (
    ACTION_CANCEL_GOVERNANCE_ACTIONS,
    ACTION_EXECUTE_GOVERNANCE_ACTIONS,
    ACTION_MANAGE_BRAND_CONFIG,
    Base,
    ActionLog,
    SessionLocal,
)
from src.persistence import session as persistence_session
from src.query_governance.relevance_guidance import RelevanceGuidanceService


class _SerialExecutor:
    """Run batch items serially: StaticPool shares one SQLite connection, so the
    production thread pool would corrupt the test harness instead of the code."""

    def __init__(self, *args, **kwargs):
        del args, kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def map(self, fn, iterable):
        return [fn(item) for item in iterable]


class _Runtime:
    def invoke(self, messages, **kwargs):
        del messages, kwargs
        return SimpleNamespace(response=SimpleNamespace(content='{"rules":"整理后的规则"}'))


@pytest.fixture()
def client(monkeypatch):
    previous_engine = persistence_session.engine
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    persistence_session.engine = engine
    SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    user = SimpleNamespace(
        id=11,
        username="alice",
        role="brand_admin",
        is_admin=False,
        is_active=True,
        brand_permissions='["atlas"]',
        action_permissions=json.dumps([ACTION_MANAGE_BRAND_CONFIG, ACTION_CANCEL_GOVERNANCE_ACTIONS, ACTION_EXECUTE_GOVERNANCE_ACTIONS]),
    )
    service = RelevanceGuidanceService(SessionLocal, organizer_runtime=_Runtime())
    monkeypatch.setattr(query_governance, "_guidance_service", lambda: service)
    monkeypatch.setattr(dashboard_module.service, "remove_from_blacklist", lambda *args, **kwargs: True)
    monkeypatch.setattr(dashboard_module.service, "add_to_blacklist", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        dashboard_module.service,
        "get_top_query_action_status",
        lambda *args, **kwargs: {
            "status": "completed" if kwargs.get("external_request_id") == "toggle-blacklist-1" else "not_found",
            "response": {},
        },
    )
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as test_client:
        yield test_client
    try:
        app.dependency_overrides.pop(get_current_user, None)
        Base.metadata.drop_all(bind=engine)
    finally:
        engine.dispose()
        persistence_session.engine = previous_engine
        SessionLocal.configure(bind=previous_engine)
        Base.metadata.create_all(bind=previous_engine)


def test_relevance_guidance_api_supports_read_edit_deactivate_and_ai_confirm(client):
    empty = client.get(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance",
        params={"query": "裙子"},
    )
    assert empty.status_code == 200
    assert empty.json()["rules"] == ""

    saved = client.put(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance",
        json={"query": "裙子", "rules": "排除男装"},
    )
    assert saved.status_code == 200
    assert saved.json()["rules"] == "排除男装"

    preview = client.post(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance/organize",
        json={"query": "裙子"},
    )
    assert preview.status_code == 200
    assert preview.json()["status"] == "preview"
    assert client.get(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance",
        params={"query": "裙子"},
    ).json()["rules"] == "排除男装"

    confirmed = client.post(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance/confirm",
        json={"query": "裙子", "rules": preview.json()["rules"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["rules"] == "整理后的规则"

    deactivated = client.post(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance/deactivate",
        json={"query": "裙子"},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == "inactive"


def test_relevance_guidance_api_enforces_brand_scope(client):
    response = client.get(
        "/api/dashboard/top-query/brands/harbor/relevance-guidance",
        params={"query": "裙子"},
    )
    assert response.status_code == 403


def test_cancel_blacklist_reason_is_audited_without_touching_guidance_rules(client):
    response = client.post(
        "/api/dashboard/actions/cancel-blacklist",
        json={
            "brand": "atlas",
            "query": "裙子",
            "sku": "SKU-1",
            "reason": "商品是男装，不应被召回",
            "requestId": "cancel-guidance-1",
        },
    )
    assert response.status_code == 200
    guidance = client.get(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance",
        params={"query": "裙子"},
    ).json()
    assert guidance["rules"] == ""
    assert [item["reason"] for item in guidance["reasonHistory"]] == ["商品是男装，不应被召回"]
    assert guidance["reasonHistory"][0]["productId"] == "SKU-1"
    assert guidance["reasonHistory"][0]["operator"] == "alice"


def test_cancel_blacklist_correction_rule_is_appended_atomically(client):
    response = client.post(
        "/api/dashboard/actions/cancel-blacklist",
        json={
            "brand": "atlas",
            "query": "裙子",
            "sku": "SKU-2",
            "reason": "商品是男装",
            "correctionRule": "泡泡袖等同于灯笼袖",
            "requestId": "cancel-guidance-correction-1",
        },
    )
    assert response.status_code == 200
    guidance = client.get(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance",
        params={"query": "裙子"},
    ).json()
    assert guidance["rules"] == "1. 泡泡袖等同于灯笼袖"
    assert [item["reason"] for item in guidance["reasonHistory"]] == ["商品是男装"]


def test_cancel_blacklist_rejects_empty_correction_rule(client):
    response = client.post(
        "/api/dashboard/actions/cancel-blacklist",
        json={
            "brand": "atlas",
            "query": "裙子",
            "sku": "SKU-3",
            "correctionRule": "   ",
            "requestId": "cancel-guidance-correction-empty",
        },
    )
    assert response.status_code == 400
    assert client.get(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance",
        params={"query": "裙子"},
    ).json()["rules"] == ""


def test_cancel_blacklist_batch_never_appends_relevance_rules(client, monkeypatch):
    monkeypatch.setattr(
        dashboard_module.concurrent.futures,
        "ThreadPoolExecutor",
        _SerialExecutor,
    )
    response = client.post(
        "/api/dashboard/actions/cancel-blacklist/batch",
        json={
            "items": [
                {"brand": "atlas", "query": "裙子", "sku": "SKU-4", "reason": "商品是男装"},
                {"brand": "atlas", "query": "裙子", "sku": "SKU-5"},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["succeeded"] == 2
    guidance = client.get(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance",
        params={"query": "裙子"},
    ).json()
    assert guidance["rules"] == ""
    assert [item["productId"] for item in guidance["reasonHistory"]] == ["SKU-4"]


def test_append_relevance_guidance_rule_is_numbered_and_deduplicated(client):
    path = "/api/dashboard/top-query/brands/atlas/relevance-guidance/append"
    first = client.post(path, json={"query": "裙子", "rule": "排除男装"})
    second = client.post(path, json={"query": "裙子", "rule": "排除童装"})
    repeated = client.post(path, json={"query": "裙子", "rule": "排除童装"})
    assert first.status_code == second.status_code == repeated.status_code == 200
    assert repeated.json()["rules"] == "1. 排除男装\n2. 排除童装"
    assert client.post(path, json={"query": "裙子", "rule": " "}).status_code == 400


def test_append_relevance_guidance_filters_four_byte_characters_before_write(client):
    path = "/api/dashboard/top-query/brands/atlas/relevance-guidance/append"
    rule = "乐福鞋一脚蹬，穿脱方便。\n🥿 鞋子长啥样\n鞋底约 1-3 厘米。"

    response = client.post(path, json={"query": "乐福鞋男士", "rule": rule})

    assert response.status_code == 200
    assert response.json()["rules"] == (
        "1. 乐福鞋一脚蹬，穿脱方便。\n"
        "2. 鞋子长啥样\n"
        "3. 鞋底约 1-3 厘米。"
    )
    assert client.post(path, json={"query": "乐福鞋男士", "rule": rule}).json()["rules"] == response.json()["rules"]
    assert client.post(path, json={"query": "乐福鞋男士", "rule": "🥿"}).status_code == 400


def test_relevance_guidance_write_response_keeps_reason_history(client):
    recorded = client.post(
        "/api/dashboard/actions/cancel-blacklist",
        json={
            "brand": "atlas",
            "query": "裙子",
            "sku": "SKU-1",
            "reason": "商品是男装，不应被召回",
            "requestId": "cancel-guidance-history-1",
        },
    )
    assert recorded.status_code == 200

    saved = client.put(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance",
        json={"query": "裙子", "rules": "排除男装"},
    )
    appended = client.post(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance/append",
        json={"query": "裙子", "rule": "泡泡袖等同于灯笼袖"},
    )
    deactivated = client.post(
        "/api/dashboard/top-query/brands/atlas/relevance-guidance/deactivate",
        json={"query": "裙子"},
    )

    for response in (saved, appended, deactivated):
        assert response.status_code == 200
        history = response.json()["reasonHistory"]
        assert [item["productId"] for item in history] == ["SKU-1"]
        assert history[0]["operator"] == "alice"
        assert len(history[0]["createdAt"]) == 19


def test_relevance_rules_runtime_reserves_completion_budget_for_reasoning(monkeypatch):
    captured: dict = {}

    def fake_capture_runtime_snapshot(**kwargs):
        captured["snapshot_kwargs"] = kwargs
        return {"snapshot": True}

    class _FakeRuntime:
        @staticmethod
        def from_snapshot(node_key, snapshot, **kwargs):
            captured["node_key"] = node_key
            captured["snapshot"] = snapshot
            captured["from_snapshot_kwargs"] = kwargs
            return SimpleNamespace()

    monkeypatch.setattr(query_governance, "capture_runtime_snapshot", fake_capture_runtime_snapshot)
    monkeypatch.setattr(query_governance, "QueryGovernanceModelRuntime", _FakeRuntime)

    query_governance._relevance_rules_runtime()

    assert captured["snapshot_kwargs"] == {"node_keys": ("query_governance.relevance_rules",)}
    assert captured["node_key"] == "query_governance.relevance_rules"
    assert captured["snapshot"] == {"snapshot": True}
    assert captured["from_snapshot_kwargs"]["timeout"] == 120
    assert captured["from_snapshot_kwargs"]["max_completion_tokens"] == 20000


def test_product_blacklist_toggle_api_supports_reblacklist_after_cancel(client):
    canceled = client.post(
        "/api/dashboard/actions/cancel-blacklist",
        json={
            "brand": "atlas",
            "query": "皮衣 女士",
            "sku": "SKU-2",
            "reason": "商品是真皮夹克，符合 Query",
            "requestId": "toggle-cancel-1",
        },
    )
    assert canceled.status_code == 200

    restored = client.post(
        "/api/dashboard/actions/blacklist",
        json={
            "brand": "atlas",
            "query": "皮衣 女士",
            "sku": "SKU-2",
            "productName": "真皮夹克",
            "reason": "相关性检测判定不相关",
            "requestId": "toggle-blacklist-1",
        },
    )
    assert restored.status_code == 200
    assert restored.json()["requestId"] == "toggle-blacklist-1"
    with SessionLocal() as session:
        latest = session.query(ActionLog).filter(
            ActionLog.brand == "atlas",
            ActionLog.query_text == "皮衣 女士",
            ActionLog.product_id == "SKU-2",
        ).order_by(ActionLog.id.desc()).first()
        assert latest is not None
        assert latest.action_type == 1
        assert latest.product_name == "真皮夹克"
