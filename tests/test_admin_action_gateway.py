import json
import datetime
from copy import deepcopy

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Query, Session
from sqlalchemy.pool import StaticPool

from importlib import import_module
api_module = import_module("src.api.app")
admin_module = import_module("src.api.application.admin")
from src.api import app, get_current_user
from src.persistence import (
    ActionAttempt,
    ActionIntent,
    ActionReadback,
    ActionTerminalFact,
    AssistantAgentBinding,
    AssistantBrandSpace,
    AssistantSessionMeta,
    AssistantUserBinding,
    Base,
    SessionLocal,
    User,
)


@pytest.fixture
def admin_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    previous_session_options = dict(SessionLocal.kw)
    SessionLocal.configure(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    admin = User(
        id=1,
        username="platform-admin",
        password_hash="hash",
        is_admin=1,
        role="platform_admin",
        is_active=1,
        page_permissions="[]",
        brand_permissions=json.dumps(["atlas", "meadow"]),
        action_permissions="[]",
    )
    db = SessionLocal()
    db.add(admin)
    db.commit()
    db.close()

    async def override_get_user():
        return admin

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = override_get_user
    client = TestClient(app)
    try:
        yield client, engine
    finally:
        client.close()
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous
        SessionLocal.configure(**previous_session_options)
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _facts():
    db = SessionLocal()
    try:
        return (
            db.query(ActionIntent).all(),
            db.query(ActionAttempt).all(),
            db.query(ActionReadback).all(),
            db.query(ActionTerminalFact).all(),
        )
    finally:
        db.close()


def _create_payload(key, username="managed-user"):
    return {
        "idempotencyKey": key,
        "username": username,
        "pagePermissions": ["/ops-dashboard"],
        "brandPermissions": ["atlas"],
        "role": "brand_member",
        "actionPermissions": [],
    }


def _assistant_agent_payload(binding_id, brand_space_id, *, name="assistant-agent"):
    return {
        "id": binding_id,
        "brandSpaceId": brand_space_id,
        "displayName": name,
        "modelDisplayName": name,
        "modelName": name,
        "availableModels": [],
        "supportsModelSwitch": False,
        "bindingType": "agent",
        "enabled": True,
    }


def test_create_user_is_idempotent_and_records_authoritative_facts(admin_client):
    client, _engine = admin_client
    payload = _create_payload("admin-create-user-1")

    first = client.post("/api/admin/users", json=payload)
    replay = client.post("/api/admin/users", json=payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["temporaryPassword"]
    intents, attempts, readbacks, terminals = _facts()
    assert len(intents) == len(attempts) == len(readbacks) == len(terminals) == 1
    assert attempts[0].external_request_id == "admin-create-user-1"
    assert readbacks[0].authoritative == 1
    assert terminals[0].status == "success"
    assert "[REDACTED]" in terminals[0].replay_result_json
    assert first.json()["temporaryPassword"] not in terminals[0].replay_result_json


def test_create_user_same_key_different_payload_conflicts_without_side_effect(admin_client):
    client, _engine = admin_client
    first_payload = _create_payload("admin-create-conflict", username="first-user")
    second_payload = _create_payload("admin-create-conflict", username="second-user")

    assert client.post("/api/admin/users", json=first_payload).status_code == 200
    conflict = client.post("/api/admin/users", json=second_payload)

    assert conflict.status_code == 409
    db = SessionLocal()
    try:
        assert db.query(User).filter(User.username == "first-user").count() == 1
        assert db.query(User).filter(User.username == "second-user").count() == 0
        assert db.query(ActionTerminalFact).count() == 1
    finally:
        db.close()


def test_create_user_rejects_existing_row_with_wrong_scope_facts(admin_client):
    client, _engine = admin_client
    payload = _create_payload("admin-create-wrong-scope", username="wrong-scope-user")
    payload.update({
        "pagePermissions": ["/config"],
        "brandPermissions": ["atlas", "meadow"],
        "role": "platform_admin",
        "actionPermissions": ["manage_brand_config"],
    })
    original_init = User.__init__

    def corrupt_user_init(instance, *args, **kwargs):
        if kwargs.get("username") == "wrong-scope-user":
            kwargs.update({
                "page_permissions": "[]",
                "brand_permissions": "[]",
                "action_permissions": "[]",
                "role": "brand_member",
                "is_admin": 0,
                "is_active": 0,
            })
        original_init(instance, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(User, "__init__", corrupt_user_init)
        response = client.post("/api/admin/users", json=payload)

    assert response.status_code == 500
    db = SessionLocal()
    try:
        assert db.query(User).filter(User.username == "wrong-scope-user").count() == 0
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


def test_create_user_rejects_password_hash_for_wrong_temporary_credential(admin_client):
    client, _engine = admin_client
    payload = _create_payload("admin-create-wrong-password", username="wrong-password-user")
    wrong_hash = bcrypt.hashpw(b"different-password", bcrypt.gensalt()).decode()

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(admin_module, "get_password_hash", lambda _password: wrong_hash)
        response = client.post("/api/admin/users", json=payload)

    assert response.status_code == 500
    db = SessionLocal()
    try:
        assert db.query(User).filter(User.username == "wrong-password-user").count() == 0
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("attribute_name", "payload"),
    [
        ("page_permissions", {"pagePermissions": ["/config"]}),
        ("brand_permissions", {"brandPermissions": ["meadow"]}),
        ("action_permissions", {"actionPermissions": ["manage_brand_config"]}),
        ("is_active", {"isActive": False}),
        ("role", {"role": "platform_admin"}),
    ],
)
def test_update_user_rejects_requested_field_that_was_not_changed(
    admin_client,
    attribute_name,
    payload,
):
    client, _engine = admin_client
    db = SessionLocal()
    target = User(
        id=30,
        username="update-mismatch-user",
        password_hash=bcrypt.hashpw(b"old-password", bcrypt.gensalt()).decode(),
        is_admin=0,
        role="brand_member",
        is_active=1,
        page_permissions="[]",
        brand_permissions='["atlas"]',
        action_permissions="[]",
    )
    db.add(target)
    db.commit()
    db.close()
    payload = {"requestId": f"admin-update-mismatch-{attribute_name}", **payload}
    attribute = getattr(User, attribute_name)

    def ignore_requested_change(target_user, value, oldvalue, _initiator):
        if target_user.id == 30:
            return oldvalue
        return value

    event.listen(attribute, "set", ignore_requested_change, retval=True)
    try:
        response = client.put("/api/admin/users/30", json=payload)
    finally:
        event.remove(attribute, "set", ignore_requested_change)

    assert response.status_code == 500
    db = SessionLocal()
    try:
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("attribute_name", "drift_value"),
    [
        ("username", "tampered-username"),
        ("password_hash", "tampered-password-hash"),
        ("updated_at", datetime.datetime(2040, 1, 1, 0, 0, 0)),
    ],
)
def test_update_user_rejects_unrequested_critical_fact_drift(
    admin_client,
    attribute_name,
    drift_value,
):
    client, _engine = admin_client
    original_password_hash = bcrypt.hashpw(b"old-password", bcrypt.gensalt()).decode()
    db = SessionLocal()
    target = User(
        id=31,
        username="stable-username",
        password_hash=original_password_hash,
        is_admin=0,
        role="brand_member",
        is_active=1,
        page_permissions="[]",
        brand_permissions='["atlas"]',
        action_permissions="[]",
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    original_updated_at = target.updated_at
    db.close()
    original_flush = Session.flush
    corrupted = False

    def corrupt_unrequested_fact(session, *args, **kwargs):
        nonlocal corrupted
        target_user = next((
            item
            for item in session.identity_map.values()
            if isinstance(item, User) and item.id == 31
        ), None)
        if target_user is not None and target_user in session.dirty and not corrupted:
            setattr(target_user, attribute_name, drift_value)
            corrupted = True
        return original_flush(session, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Session, "flush", corrupt_unrequested_fact)
        response = client.put("/api/admin/users/31", json={
            "requestId": f"admin-update-unrequested-drift-{attribute_name}",
            "pagePermissions": ["/config"],
        })

    assert response.status_code == 500
    db = SessionLocal()
    try:
        persisted = db.get(User, 31)
        assert persisted.username == "stable-username"
        assert persisted.password_hash == original_password_hash
        assert persisted.updated_at == original_updated_at
        assert persisted.page_permissions == "[]"
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


def test_update_reset_revoke_and_delete_user_use_gateway_replay_contract(admin_client):
    client, _engine = admin_client
    db = SessionLocal()
    target = User(
        id=20,
        username="target-user",
        password_hash=bcrypt.hashpw(b"old-password", bcrypt.gensalt()).decode(),
        is_admin=0,
        role="brand_member",
        is_active=1,
        page_permissions="[]",
        brand_permissions=json.dumps(["atlas"]),
        action_permissions="[]",
    )
    db.add(target)
    db.commit()
    db.close()

    updated = client.put(
        "/api/admin/users/20",
        json={
            "idempotencyKey": "admin-update-user-20",
            "isActive": False,
            "pagePermissions": ["/config"],
        },
    )
    assert updated.status_code == 200

    reset_payload = {"requestId": "admin-reset-user-20"}
    reset = client.post("/api/admin/users/20/reset-password", json=reset_payload)
    reset_replay = client.post("/api/admin/users/20/reset-password", json=reset_payload)
    assert reset.status_code == reset_replay.status_code == 200
    assert reset.json() == reset_replay.json()

    revoke_payload = {"idempotencyKey": "admin-revoke-user-20"}
    revoke = client.post("/api/admin/users/20/revoke-sessions", json=revoke_payload)
    revoke_replay = client.post("/api/admin/users/20/revoke-sessions", json=revoke_payload)
    assert revoke.status_code == revoke_replay.status_code == 200
    assert revoke.json() == revoke_replay.json()

    delete_payload = {"idempotencyKey": "admin-delete-user-20"}
    deleted = client.request("DELETE", "/api/admin/users/20", json=delete_payload)
    deleted_replay = client.request("DELETE", "/api/admin/users/20", json=delete_payload)
    assert deleted.status_code == deleted_replay.status_code == 200
    assert deleted.json() == deleted_replay.json()

    db = SessionLocal()
    try:
        assert db.get(User, 20) is None
        assert db.query(ActionIntent).count() == 4
        assert db.query(ActionReadback).count() == 4
        assert db.query(ActionTerminalFact).count() == 4
    finally:
        db.close()


def test_assistant_access_config_is_idempotent_and_authoritatively_read_back(admin_client):
    client, _engine = admin_client
    payload = {
        "idempotencyKey": "assistant-access-config-1",
        "brandSpaces": [
            {"brand": "atlas", "displayName": "ATLAS", "enabled": True},
        ],
        "agentBindings": [],
        "userBindings": [],
    }

    first = client.post("/api/admin/assistant/access-config", json=payload)
    replay = client.post("/api/admin/assistant/access-config", json=payload)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    intents, attempts, readbacks, terminals = _facts()
    assert len(intents) == len(attempts) == len(readbacks) == len(terminals) == 1
    assert attempts[0].external_request_id == "assistant-access-config-1"
    assert readbacks[0].authoritative == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"requestId": "assistant-access-missing-all"},
        {
            "requestId": "assistant-access-missing-brand-spaces",
            "agentBindings": [],
            "userBindings": [],
        },
        {
            "requestId": "assistant-access-missing-agent-bindings",
            "brandSpaces": [],
            "userBindings": [],
        },
        {
            "requestId": "assistant-access-missing-user-bindings",
            "brandSpaces": [],
            "agentBindings": [],
        },
        {
            "requestId": "assistant-access-invalid-brand-spaces",
            "brandSpaces": {},
            "agentBindings": [],
            "userBindings": [],
        },
        {
            "requestId": "assistant-access-invalid-agent-bindings",
            "brandSpaces": [],
            "agentBindings": None,
            "userBindings": [],
        },
        {
            "requestId": "assistant-access-invalid-user-bindings",
            "brandSpaces": [],
            "agentBindings": [],
            "userBindings": "all",
        },
    ],
    ids=[
        "missing-all",
        "missing-brand-spaces",
        "missing-agent-bindings",
        "missing-user-bindings",
        "invalid-brand-spaces",
        "invalid-agent-bindings",
        "invalid-user-bindings",
    ],
)
def test_assistant_access_config_requires_all_complete_replacement_lists_before_gateway(
    admin_client,
    payload,
):
    client, _engine = admin_client
    db = SessionLocal()
    db.add(AssistantBrandSpace(id=9, brand="atlas", display_name="ATLAS", enabled=1))
    db.commit()
    db.close()

    response = client.post("/api/admin/assistant/access-config", json=payload)

    assert response.status_code == 422
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 9) is not None
        assert db.query(ActionIntent).count() == 0
        assert db.query(ActionAttempt).count() == 0
        assert db.query(ActionTerminalFact).count() == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("collection_name", "invalid_item"),
    [
        ("brandSpaces", "not-an-object"),
        ("agentBindings", None),
        ("userBindings", []),
    ],
    ids=["brand-space", "agent-binding", "user-binding"],
)
def test_assistant_access_config_rejects_non_object_collection_items_before_gateway(
    admin_client,
    collection_name,
    invalid_item,
):
    client, _engine = admin_client
    payload = {
        "requestId": f"assistant-access-invalid-{collection_name}",
        "brandSpaces": [],
        "agentBindings": [],
        "userBindings": [],
    }
    payload[collection_name] = [invalid_item]

    response = client.post("/api/admin/assistant/access-config", json=payload)

    assert response.status_code == 422
    db = SessionLocal()
    try:
        assert db.query(ActionIntent).count() == 0
        assert db.query(ActionAttempt).count() == 0
        assert db.query(ActionTerminalFact).count() == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("case_name", "invalid_value"),
    [
        ("missing-brand-space", 999),
        ("invalid-user-id", "not-an-id"),
        ("boolean-user-id", True),
        ("zero-user-id", 0),
        ("missing-user", 999),
    ],
    ids=["missing-brand-space", "invalid-user-id", "boolean-user-id", "zero-user-id", "missing-user"],
)
def test_assistant_access_config_rejects_invalid_object_references_with_stable_400(
    admin_client,
    case_name,
    invalid_value,
):
    client, _engine = admin_client
    db = SessionLocal()
    db.add(AssistantBrandSpace(id=170, brand="atlas", display_name="ATLAS", enabled=1))
    db.add(AssistantAgentBinding(id=171, brand_space_id=170, model_name="atlas-agent"))
    db.commit()
    db.close()
    brand_space_id = invalid_value if case_name == "missing-brand-space" else 170
    user_bindings = [] if case_name == "missing-brand-space" else [{
        "userId": invalid_value,
        "brandSpaceId": 170,
        "agentBindingId": 171,
    }]

    response = client.post("/api/admin/assistant/access-config", json={
        "requestId": f"assistant-access-{case_name}",
        "brandSpaces": [{
            "id": brand_space_id,
            "brand": "atlas",
            "displayName": "Changed",
            "enabled": True,
        }],
        "agentBindings": (
            []
            if case_name == "missing-brand-space"
            else [_assistant_agent_payload(171, 170, name="atlas-agent")]
        ),
        "userBindings": user_bindings,
    })

    assert response.status_code == 400
    assert response.json()["detail"] == "Assistant config reference is invalid"
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 170).display_name == "ATLAS"
        assert db.get(AssistantAgentBinding, 171) is not None
        assert db.query(AssistantUserBinding).count() == 0
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("reference_field", "invalid_value"),
    [
        ("brand-space-id", 170.9),
        ("agent-brand-space-id", 170.9),
        ("user-id", 1.9),
        ("user-agent-id", 171.9),
        ("brand-space-id", "170.9"),
        ("brand-space-id", "1e2"),
        ("brand-space-id", " 170"),
        ("brand-space-id", "170 "),
        ("brand-space-id", "+170"),
    ],
    ids=[
        "brand-space-float",
        "agent-brand-space-float",
        "user-float",
        "user-agent-float",
        "decimal-string",
        "scientific-string",
        "leading-whitespace",
        "trailing-whitespace",
        "plus-sign",
    ],
)
def test_assistant_access_config_rejects_noncanonical_reference_ids(
    admin_client,
    reference_field,
    invalid_value,
):
    client, _engine = admin_client
    db = SessionLocal()
    db.add(AssistantBrandSpace(id=170, brand="atlas", display_name="ATLAS", enabled=1))
    db.add(AssistantAgentBinding(id=171, brand_space_id=170, model_name="atlas-agent"))
    db.commit()
    db.close()
    brand_space = {
        "id": 170,
        "brand": "atlas",
        "displayName": "Changed",
        "enabled": True,
    }
    agent_binding = _assistant_agent_payload(171, 170, name="atlas-agent")
    user_binding = {
        "userId": 1,
        "brandSpaceId": 170,
        "agentBindingId": 171,
    }
    if reference_field == "brand-space-id":
        brand_space["id"] = invalid_value
    elif reference_field == "agent-brand-space-id":
        agent_binding["brandSpaceId"] = invalid_value
    elif reference_field == "user-id":
        user_binding["userId"] = invalid_value
    else:
        user_binding["agentBindingId"] = invalid_value

    response = client.post("/api/admin/assistant/access-config", json={
        "requestId": f"assistant-access-noncanonical-{reference_field}-{invalid_value}",
        "brandSpaces": [brand_space],
        "agentBindings": [agent_binding],
        "userBindings": [user_binding],
    })

    assert response.status_code == 400
    assert response.json()["detail"] == "Assistant config reference is invalid"
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 170).display_name == "ATLAS"
        assert db.get(AssistantAgentBinding, 171) is not None
        assert db.query(AssistantUserBinding).count() == 0
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


def test_assistant_access_config_accepts_decimal_string_ids_and_signed_temporary_ids(
    admin_client,
):
    client, _engine = admin_client

    response = client.post("/api/admin/assistant/access-config", json={
        "requestId": "assistant-access-decimal-string-ids",
        "brandSpaces": [{
            "id": "-130",
            "brand": "atlas",
            "displayName": "ATLAS",
            "defaultAgentBindingId": "-131",
            "enabled": True,
        }],
        "agentBindings": [_assistant_agent_payload("-131", "-130", name="new-agent")],
        "userBindings": [{
            "userId": "1",
            "brandSpaceId": "-130",
            "agentBindingId": "-131",
        }],
    })

    assert response.status_code == 200
    body = response.json()
    assert body["brandSpaces"][0]["id"] > 0
    assert body["agentBindings"][0]["brandSpaceId"] == body["brandSpaces"][0]["id"]
    assert body["userBindings"][0]["agentBindingId"] == body["agentBindings"][0]["id"]


@pytest.mark.parametrize(
    ("default_binding_id", "include_other_brand", "request_id"),
    [
        (999, False, "assistant-access-default-missing"),
        (111, True, "assistant-access-default-cross-brand"),
    ],
)
def test_assistant_access_config_rejects_invalid_default_agent_binding(
    admin_client,
    default_binding_id,
    include_other_brand,
    request_id,
):
    client, _engine = admin_client
    db = SessionLocal()
    db.add(AssistantBrandSpace(id=100, brand="atlas", display_name="ATLAS", enabled=1))
    db.add(AssistantAgentBinding(id=101, brand_space_id=100, model_name="atlas-agent"))
    if include_other_brand:
        db.add(AssistantBrandSpace(id=110, brand="meadow", display_name="MEADOW", enabled=1))
        db.add(AssistantAgentBinding(id=111, brand_space_id=110, model_name="meadow-agent"))
    db.commit()
    db.close()
    brand_spaces = [
        {
            "id": 100,
            "brand": "atlas",
            "displayName": "ATLAS changed",
            "defaultAgentBindingId": default_binding_id,
            "enabled": True,
        },
    ]
    agent_bindings = [_assistant_agent_payload(101, 100, name="atlas-agent")]
    if include_other_brand:
        brand_spaces.append({
            "id": 110,
            "brand": "meadow",
            "displayName": "MEADOW",
            "defaultAgentBindingId": 111,
            "enabled": True,
        })
        agent_bindings.append(_assistant_agent_payload(111, 110, name="meadow-agent"))

    response = client.post("/api/admin/assistant/access-config", json={
        "requestId": request_id,
        "brandSpaces": brand_spaces,
        "agentBindings": agent_bindings,
        "userBindings": [],
    })

    assert response.status_code == 400
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 100).display_name == "ATLAS"
        assert db.get(AssistantBrandSpace, 100).default_agent_binding_id is None
        assert db.get(AssistantAgentBinding, 101) is not None
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


def test_assistant_access_config_rejects_removing_retained_default_agent(admin_client):
    client, _engine = admin_client
    db = SessionLocal()
    db.add(AssistantBrandSpace(
        id=120,
        brand="atlas",
        display_name="ATLAS",
        default_agent_binding_id=121,
        enabled=1,
    ))
    db.add(AssistantAgentBinding(id=121, brand_space_id=120, model_name="default-agent"))
    db.commit()
    db.close()

    response = client.post("/api/admin/assistant/access-config", json={
        "requestId": "assistant-access-remove-default-agent",
        "brandSpaces": [
            {"id": 120, "brand": "atlas", "displayName": "Changed", "enabled": True},
        ],
        "agentBindings": [],
        "userBindings": [],
    })

    assert response.status_code == 400
    db = SessionLocal()
    try:
        brand_space = db.get(AssistantBrandSpace, 120)
        assert brand_space.display_name == "ATLAS"
        assert brand_space.default_agent_binding_id == 121
        assert db.get(AssistantAgentBinding, 121) is not None
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


def test_assistant_access_config_maps_new_temporary_ids_consistently(admin_client):
    client, _engine = admin_client
    response = client.post("/api/admin/assistant/access-config", json={
        "requestId": "assistant-access-temp-id-map",
        "brandSpaces": [{
            "id": -130,
            "brand": "atlas",
            "displayName": "ATLAS",
            "defaultAgentBindingId": -131,
            "enabled": True,
        }],
        "agentBindings": [_assistant_agent_payload(-131, -130, name="new-agent")],
        "userBindings": [{
            "userId": 1,
            "brandSpaceId": -130,
            "agentBindingId": -131,
        }],
    })

    assert response.status_code == 200
    body = response.json()
    assert len(body["brandSpaces"]) == len(body["agentBindings"]) == len(body["userBindings"]) == 1
    brand_space = body["brandSpaces"][0]
    agent_binding = body["agentBindings"][0]
    user_binding = body["userBindings"][0]
    assert brand_space["id"] > 0
    assert agent_binding["id"] > 0
    assert brand_space["defaultAgentBindingId"] == agent_binding["id"]
    assert agent_binding["brandSpaceId"] == brand_space["id"]
    assert user_binding["brandSpaceId"] == brand_space["id"]
    assert user_binding["agentBindingId"] == agent_binding["id"]


@pytest.mark.parametrize("duplicate_kind", ["brand-space", "agent-binding"])
def test_assistant_access_config_rejects_duplicate_temporary_ids(admin_client, duplicate_kind):
    client, _engine = admin_client
    if duplicate_kind == "brand-space":
        brand_spaces = [
            {"id": -180, "brand": "atlas", "displayName": "ATLAS", "enabled": True},
            {"id": -180, "brand": "meadow", "displayName": "MEADOW", "enabled": True},
        ]
        agent_bindings = []
    else:
        brand_spaces = [
            {"id": -180, "brand": "atlas", "displayName": "ATLAS", "enabled": True},
        ]
        agent_bindings = [
            _assistant_agent_payload(-181, -180, name="first-agent"),
            _assistant_agent_payload(-181, -180, name="second-agent"),
        ]

    response = client.post("/api/admin/assistant/access-config", json={
        "requestId": f"assistant-access-duplicate-temp-{duplicate_kind}",
        "brandSpaces": brand_spaces,
        "agentBindings": agent_bindings,
        "userBindings": [],
    })

    assert response.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(AssistantBrandSpace).count() == 0
        assert db.query(AssistantAgentBinding).count() == 0
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("agent_bindings", "user_bindings", "request_id"),
    [
        (
            [_assistant_agent_payload(141, 999, name="wrong-space")],
            [],
            "assistant-access-invalid-agent-space",
        ),
        (
            [_assistant_agent_payload(141, 140, name="valid-agent")],
            [{"userId": 1, "brandSpaceId": 999, "agentBindingId": 141}],
            "assistant-access-invalid-user-space",
        ),
        (
            [_assistant_agent_payload(141, 140, name="valid-agent")],
            [{"userId": 1, "brandSpaceId": 140, "agentBindingId": 999}],
            "assistant-access-invalid-user-agent",
        ),
    ],
    ids=["agent-brand-space", "user-brand-space", "user-agent"],
)
def test_assistant_access_config_rejects_invalid_collection_references(
    admin_client,
    agent_bindings,
    user_bindings,
    request_id,
):
    client, _engine = admin_client
    db = SessionLocal()
    db.add(AssistantBrandSpace(id=140, brand="atlas", display_name="ATLAS", enabled=1))
    db.add(AssistantAgentBinding(id=141, brand_space_id=140, model_name="valid-agent"))
    db.commit()
    db.close()

    response = client.post("/api/admin/assistant/access-config", json={
        "requestId": request_id,
        "brandSpaces": [
            {"id": 140, "brand": "atlas", "displayName": "Changed", "enabled": True},
        ],
        "agentBindings": agent_bindings,
        "userBindings": user_bindings,
    })

    assert response.status_code == 400
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 140).display_name == "ATLAS"
        assert db.get(AssistantAgentBinding, 141) is not None
        assert db.query(AssistantUserBinding).count() == 0
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


def test_assistant_access_config_rejects_cross_scope_agent_reference(admin_client):
    client, _engine = admin_client
    db = SessionLocal()
    db.add_all([
        AssistantBrandSpace(id=150, brand="atlas", display_name="ATLAS", enabled=1),
        AssistantBrandSpace(id=151, brand="harbor", display_name="Harbor", enabled=1),
    ])
    db.add(AssistantAgentBinding(id=152, brand_space_id=151, model_name="harbor-agent"))
    db.commit()
    db.close()
    brand_admin = User(
        id=2,
        username="atlas-reference-admin",
        password_hash="hash",
        is_admin=0,
        role="brand_admin",
        is_active=1,
        page_permissions="[]",
        brand_permissions='["atlas"]',
        action_permissions='["manage_assistant_access"]',
    )

    async def override_get_user():
        return brand_admin

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = override_get_user
    try:
        response = client.post("/api/admin/assistant/access-config", json={
            "requestId": "assistant-access-cross-scope-reference",
            "brandSpaces": [
                {"id": 150, "brand": "atlas", "displayName": "Changed", "enabled": True},
            ],
            "agentBindings": [_assistant_agent_payload(152, 151, name="harbor-agent")],
            "userBindings": [],
        })
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    assert response.status_code == 403
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 150).display_name == "ATLAS"
        assert db.get(AssistantAgentBinding, 152) is not None
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("target_role", "target_brands", "target_active", "expected_status"),
    [
        ("brand_member", ["harbor"], 1, 403),
        ("platform_admin", ["atlas"], 1, 403),
        ("brand_member", ["atlas"], 0, 200),
    ],
    ids=["cross-brand-member", "platform-admin-target", "inactive-brand-member"],
)
def test_brand_admin_user_binding_respects_managed_user_scope(
    admin_client,
    target_role,
    target_brands,
    target_active,
    expected_status,
):
    client, _engine = admin_client
    db = SessionLocal()
    db.add_all([
        AssistantBrandSpace(id=180, brand="atlas", display_name="ATLAS", enabled=1),
        AssistantAgentBinding(id=181, brand_space_id=180, model_name="atlas-agent"),
        User(
            id=3,
            username=f"target-{target_role}-{target_active}",
            password_hash="hash",
            is_admin=1 if target_role == "platform_admin" else 0,
            role=target_role,
            is_active=target_active,
            page_permissions="[]",
            brand_permissions=json.dumps(target_brands),
            action_permissions="[]",
        ),
    ])
    db.commit()
    db.close()
    brand_admin = User(
        id=2,
        username="atlas-binding-admin",
        password_hash="hash",
        is_admin=0,
        role="brand_admin",
        is_active=1,
        page_permissions="[]",
        brand_permissions='["atlas"]',
        action_permissions='["manage_assistant_access", "manage_brand_users"]',
    )

    async def override_get_user():
        return brand_admin

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = override_get_user
    try:
        response = client.post("/api/admin/assistant/access-config", json={
            "requestId": f"assistant-access-user-scope-{target_role}-{target_active}",
            "brandSpaces": [{
                "id": 180,
                "brand": "atlas",
                "displayName": "Changed",
                "enabled": True,
            }],
            "agentBindings": [_assistant_agent_payload(181, 180, name="atlas-agent")],
            "userBindings": [{
                "userId": 3,
                "brandSpaceId": 180,
                "agentBindingId": 181,
            }],
        })
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    assert response.status_code == expected_status
    db = SessionLocal()
    try:
        if expected_status == 200:
            assert db.query(AssistantUserBinding).filter_by(user_id=3).count() == 1
            assert db.query(ActionTerminalFact).one().status == "success"
        else:
            assert db.get(AssistantBrandSpace, 180).display_name == "ATLAS"
            assert db.query(AssistantUserBinding).count() == 0
            assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


def test_platform_admin_cannot_bind_user_without_target_brand_permission(admin_client):
    client, _engine = admin_client
    db = SessionLocal()
    db.add_all([
        AssistantBrandSpace(id=190, brand="atlas", display_name="ATLAS", enabled=1),
        AssistantAgentBinding(id=191, brand_space_id=190, model_name="atlas-agent"),
        User(
            id=3,
            username="harbor-only-member",
            password_hash="hash",
            is_admin=0,
            role="brand_member",
            is_active=1,
            page_permissions="[]",
            brand_permissions='["harbor"]',
            action_permissions="[]",
        ),
    ])
    db.commit()
    db.close()

    response = client.post("/api/admin/assistant/access-config", json={
        "requestId": "assistant-access-platform-cross-brand-user",
        "brandSpaces": [{
            "id": 190,
            "brand": "atlas",
            "displayName": "Changed",
            "enabled": True,
        }],
        "agentBindings": [_assistant_agent_payload(191, 190, name="atlas-agent")],
        "userBindings": [{
            "userId": 3,
            "brandSpaceId": 190,
            "agentBindingId": 191,
        }],
    })

    assert response.status_code == 403
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 190).display_name == "ATLAS"
        assert db.query(AssistantUserBinding).count() == 0
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


def test_assistant_access_config_requires_explicit_confirmation_to_clear_nonempty_scope(admin_client):
    client, _engine = admin_client
    db = SessionLocal()
    db.add(AssistantBrandSpace(id=160, brand="atlas", display_name="ATLAS", enabled=1))
    db.add(AssistantAgentBinding(id=161, brand_space_id=160, model_name="atlas-agent"))
    db.add(AssistantUserBinding(user_id=1, brand_space_id=160, agent_binding_id=161))
    db.commit()
    db.close()

    rejected = client.post("/api/admin/assistant/access-config", json={
        "requestId": "assistant-access-clear-without-confirmation",
        "brandSpaces": [],
        "agentBindings": [],
        "userBindings": [],
    })

    assert rejected.status_code == 409
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 160) is not None
        assert db.get(AssistantAgentBinding, 161) is not None
        assert db.query(AssistantUserBinding).count() == 1
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()

    confirmed = client.post("/api/admin/assistant/access-config", json={
        "requestId": "assistant-access-clear-with-confirmation",
        "confirmClear": True,
        "brandSpaces": [],
        "agentBindings": [],
        "userBindings": [],
    })

    assert confirmed.status_code == 200
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 160) is None
        assert db.get(AssistantAgentBinding, 161) is None
        assert db.query(AssistantUserBinding).count() == 0
    finally:
        db.close()


def test_assistant_access_config_returns_conflict_when_history_references_removed_agent(admin_client):
    client, engine = admin_client
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    db = SessionLocal()
    db.add(AssistantBrandSpace(id=170, brand="atlas", display_name="ATLAS", enabled=1))
    db.add(AssistantAgentBinding(id=171, brand_space_id=170, model_name="historical-agent"))
    db.commit()
    db.add(AssistantSessionMeta(
        id=172,
        session_id="historical-assistant-session",
        user_id=1,
        brand="atlas",
        agent_binding_id=171,
    ))
    db.commit()
    db.close()

    response = client.post("/api/admin/assistant/access-config", json={
        "requestId": "assistant-access-historical-agent-conflict",
        "brandSpaces": [
            {"id": 170, "brand": "atlas", "displayName": "Changed", "enabled": True},
        ],
        "agentBindings": [],
        "userBindings": [],
    })

    assert response.status_code == 409
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 170).display_name == "ATLAS"
        assert db.get(AssistantAgentBinding, 171) is not None
        assert db.get(AssistantSessionMeta, 172).agent_binding_id == 171
        terminal = db.query(ActionTerminalFact).one()
        assert terminal.status == "failed"
        assert "assistant_config_historical_reference" in terminal.result_json
    finally:
        db.close()


def test_assistant_access_config_rejects_same_count_with_wrong_fields(admin_client):
    client, _engine = admin_client
    db = SessionLocal()
    db.add(AssistantBrandSpace(
        id=10,
        brand="atlas",
        display_name="Old display",
        enabled=1,
    ))
    db.commit()
    db.close()
    payload = {
        "requestId": "assistant-access-wrong-fields",
        "brandSpaces": [
            {"id": 10, "brand": "atlas", "displayName": "New display", "enabled": False},
        ],
        "agentBindings": [],
        "userBindings": [],
    }

    def ignore_display_name(_target, value, oldvalue, _initiator):
        return oldvalue if value == "New display" else value

    event.listen(AssistantBrandSpace.display_name, "set", ignore_display_name, retval=True)
    try:
        response = client.post("/api/admin/assistant/access-config", json=payload)
    finally:
        event.remove(AssistantBrandSpace.display_name, "set", ignore_display_name)

    assert response.status_code == 500
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 10).display_name == "Old display"
        terminal = db.query(ActionTerminalFact).one()
        assert terminal.status == "failed"
    finally:
        db.close()


def test_assistant_access_config_rejects_residual_deleted_bindings(admin_client):
    client, _engine = admin_client
    db = SessionLocal()
    db.add(AssistantBrandSpace(
        id=20,
        brand="atlas",
        display_name="ATLAS",
        enabled=1,
    ))
    db.add(User(
        id=2,
        username="assistant-target",
        password_hash="hash",
        is_admin=0,
        role="brand_member",
        is_active=1,
        page_permissions="[]",
        brand_permissions='["atlas"]',
        action_permissions="[]",
    ))
    db.add(AssistantUserBinding(user_id=1, brand_space_id=20, agent_binding_id=None))
    db.commit()
    db.close()
    payload = {
        "requestId": "assistant-access-residual-binding",
        "brandSpaces": [
            {"id": 20, "brand": "atlas", "displayName": "ATLAS", "enabled": True},
        ],
        "agentBindings": [],
        "userBindings": [
            {"userId": 2, "brandSpaceId": 20, "agentBindingId": None},
        ],
    }
    original_delete = Query.delete

    def skip_user_binding_delete(query, *args, **kwargs):
        entity = query.column_descriptions[0].get("entity")
        if entity is AssistantUserBinding:
            return 0
        return original_delete(query, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Query, "delete", skip_user_binding_delete)
        response = client.post("/api/admin/assistant/access-config", json=payload)

    assert response.status_code == 500
    db = SessionLocal()
    try:
        bindings = db.query(AssistantUserBinding).all()
        assert [(item.user_id, item.brand_space_id) for item in bindings] == [(1, 20)]
        terminal = db.query(ActionTerminalFact).one()
        assert terminal.status == "failed"
    finally:
        db.close()


def test_assistant_access_config_rejects_residual_brand_space(admin_client):
    client, _engine = admin_client
    db = SessionLocal()
    db.add_all([
        AssistantBrandSpace(id=30, brand="atlas", display_name="ATLAS", enabled=1),
        AssistantBrandSpace(id=31, brand="meadow", display_name="MEADOW", enabled=1),
    ])
    db.commit()
    db.close()
    payload = {
        "requestId": "assistant-access-residual-brand-space",
        "brandSpaces": [
            {"id": 30, "brand": "atlas", "displayName": "ATLAS", "enabled": True},
        ],
        "agentBindings": [],
        "userBindings": [],
    }
    original_delete = Query.delete

    def skip_brand_space_delete(query, *args, **kwargs):
        entity = query.column_descriptions[0].get("entity")
        if entity is AssistantBrandSpace:
            return 0
        return original_delete(query, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Query, "delete", skip_brand_space_delete)
        response = client.post("/api/admin/assistant/access-config", json=payload)

    assert response.status_code == 500
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 31) is not None
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


def test_assistant_access_config_rejects_residual_agent_binding(admin_client):
    client, _engine = admin_client
    db = SessionLocal()
    db.add(AssistantBrandSpace(id=40, brand="atlas", display_name="ATLAS", enabled=1))
    db.add(AssistantAgentBinding(
        id=41,
        brand_space_id=40,
        display_name="Residual",
        model_display_name="Residual",
        model_name="residual",
        available_models_json="[]",
        supports_model_switch=0,
        binding_type="agent",
        enabled=1,
    ))
    db.commit()
    db.close()
    payload = {
        "requestId": "assistant-access-residual-agent-binding",
        "brandSpaces": [
            {"id": 40, "brand": "atlas", "displayName": "ATLAS", "enabled": True},
        ],
        "agentBindings": [],
        "userBindings": [],
    }
    original_delete = Query.delete

    def skip_agent_binding_delete(query, *args, **kwargs):
        entity = query.column_descriptions[0].get("entity")
        if entity is AssistantAgentBinding:
            return 0
        return original_delete(query, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Query, "delete", skip_agent_binding_delete)
        response = client.post("/api/admin/assistant/access-config", json=payload)

    assert response.status_code == 500
    db = SessionLocal()
    try:
        assert db.get(AssistantAgentBinding, 41) is not None
        assert db.query(ActionTerminalFact).one().status == "failed"
    finally:
        db.close()


def test_assistant_access_config_replaces_only_authorized_brand_scope(admin_client):
    client, _engine = admin_client
    db = SessionLocal()
    db.add_all([
        AssistantBrandSpace(id=50, brand="atlas", display_name="ATLAS", enabled=1),
        AssistantBrandSpace(id=51, brand="harbor", display_name="Harbor", enabled=1),
    ])
    db.add_all([
        AssistantAgentBinding(id=52, brand_space_id=50, model_name="atlas-agent"),
        AssistantAgentBinding(id=53, brand_space_id=51, model_name="harbor-agent"),
    ])
    db.commit()
    db.close()
    brand_admin = User(
        id=2,
        username="atlas-admin",
        password_hash="hash",
        is_admin=0,
        role="brand_admin",
        is_active=1,
        page_permissions="[]",
        brand_permissions='["atlas"]',
        action_permissions='["manage_assistant_access"]',
    )

    async def override_get_user():
        return brand_admin

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = override_get_user
    try:
        response = client.post("/api/admin/assistant/access-config", json={
            "requestId": "assistant-access-scoped-replacement",
            "confirmClear": True,
            "brandSpaces": [],
            "agentBindings": [],
            "userBindings": [],
        })
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    assert response.status_code == 200
    db = SessionLocal()
    try:
        assert db.get(AssistantBrandSpace, 50) is None
        assert db.get(AssistantAgentBinding, 52) is None
        assert db.get(AssistantBrandSpace, 51) is not None
        assert db.get(AssistantAgentBinding, 53) is not None
    finally:
        db.close()


def test_admin_config_uses_stable_request_id_and_does_not_retry_save_on_replay(admin_client):
    client, _engine = admin_client
    payload = {
        "requestId": "admin-config-1",
        "brand_configs": {
            "atlas": {"top_query_token_limit": 100000},
        },
    }

    saved = []

    def fake_save(candidate):
        saved.append(candidate)
        admin_module._raw_config.clear()
        admin_module._raw_config.update(candidate)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(admin_module, "save_config", fake_save)
        monkeypatch.setattr(admin_module, "reload_config", lambda: None)
        first = client.post("/api/admin/config", json=payload)
        replay = client.post("/api/admin/config", json=payload)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert len(saved) == 1
    intents, attempts, readbacks, terminals = _facts()
    assert len(intents) == len(attempts) == len(readbacks) == len(terminals) == 1
    assert attempts[0].external_request_id == "admin-config-1"
    assert readbacks[0].authoritative == 1


@pytest.mark.parametrize("reload_mode", ["no_op", "mismatch"])
def test_admin_config_requires_persisted_source_readback(admin_client, reload_mode):
    client, _engine = admin_client
    persisted = {
        "safety_config": {},
        "brand_configs": {"atlas": {"top_query_token_limit": 50000}},
    }
    live_config = deepcopy(persisted)
    payload = {
        "requestId": f"admin-config-persisted-{reload_mode}",
        "brand_configs": {"atlas": {"top_query_token_limit": 50001}},
    }

    def fake_reload():
        if reload_mode == "mismatch":
            live_config.clear()
            live_config.update(deepcopy(persisted))

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(admin_module, "_raw_config", live_config)
        monkeypatch.setattr(admin_module, "BRAND_CONFIGS", live_config["brand_configs"])
        monkeypatch.setattr(admin_module, "save_config", lambda _candidate: None)
        monkeypatch.setattr(admin_module, "reload_config", fake_reload)
        response = client.post("/api/admin/config", json=payload)

    assert response.status_code == 500
    assert live_config["brand_configs"]["atlas"]["top_query_token_limit"] == 50000
    _intents, _attempts, readbacks, terminals = _facts()
    assert len(readbacks) == len(terminals) == 1
    assert readbacks[0].authoritative == 1
    assert readbacks[0].status == "failed"
    assert terminals[0].status == "failed"


def test_admin_config_save_failure_fails_closed_after_one_authoritative_readback(admin_client):
    client, _engine = admin_client
    payload = {
        "requestId": "admin-config-failure-1",
        "brand_configs": {
            "atlas": {"top_query_token_limit": 100001},
        },
    }

    save_calls = []

    def failing_save(_candidate):
        save_calls.append("save")
        raise OSError("config filesystem unavailable")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(admin_module, "save_config", failing_save)
        monkeypatch.setattr(admin_module, "reload_config", lambda: None)
        response = client.post("/api/admin/config", json=payload)

    assert response.status_code == 500
    assert save_calls == ["save"]
    _intents, _attempts, readbacks, terminals = _facts()
    assert len(readbacks) == len(terminals) == 1
    assert readbacks[0].authoritative == 1
    assert terminals[0].status == "failed"
