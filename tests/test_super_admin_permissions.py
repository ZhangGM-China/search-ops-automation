"""Super administrators inherit configured modules without permission backfills."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from src.api import app, get_current_user
from src.api import dependencies as auth
from src.api.application import assistant, no_result, query_governance
from src.config import BRAND_CONFIGS
from src.persistence import Base, SessionLocal, User, NoResultQuery
from src.persistence.schema import DEFAULT_ADMIN_PAGE_PERMISSIONS


def account(role="platform_admin", is_admin=1):
    return User(id=1, username="admin", role=role, is_admin=is_admin, is_active=1,
                password_hash="hash", page_permissions='["/ops-dashboard"]',
                brand_permissions='["atlas"]', action_permissions='["manage_brand_users"]')


@pytest.mark.parametrize("role,is_admin", [("platform_admin", 1), ("platform_admin", 0), (None, 1)])
def test_super_admin_inherits_new_brand_pages_and_actions(monkeypatch, role, is_admin):
    user = account(role, is_admin)
    before = (user.brand_permissions, user.page_permissions, user.action_permissions)
    monkeypatch.setitem(BRAND_CONFIGS, "new_brand", {})
    serialized = auth._serialize_user(user)
    assert "new_brand" in serialized["brandPermissions"]
    assert set(DEFAULT_ADMIN_PAGE_PERMISSIONS) <= set(serialized["pagePermissions"])
    assert "manage_platform_config" in serialized["actionPermissions"]
    assert serialized["isAdmin"] is True
    query_governance._ensure_brand("new_brand", user)
    assert asyncio.run(auth.check_page_permission("/future-module")(user)) is user
    assert before == (user.brand_permissions, user.page_permissions, user.action_permissions)


@pytest.mark.parametrize("role,is_admin", [("brand_member", 0), ("brand_admin", 0), ("brand_member", 1)])
def test_explicit_non_platform_role_never_inherits_global_permissions(monkeypatch, role, is_admin):
    monkeypatch.setitem(BRAND_CONFIGS, "new_brand", {})
    user = account(role, is_admin)
    assert auth._serialize_user(user)["brandPermissions"] == ["atlas"]
    assert auth._serialize_user(user)["isAdmin"] is False
    with pytest.raises(HTTPException) as exc:
        query_governance._ensure_brand("new_brand", user)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        asyncio.run(auth.check_page_permission("/future-module")(user))


def test_assistant_super_admin_scope_is_not_limited_to_existing_bindings(monkeypatch):
    monkeypatch.setitem(BRAND_CONFIGS, "new_brand", {})
    binding = SimpleNamespace(id=9, brand_space=SimpleNamespace(brand="atlas"))
    monkeypatch.setattr(assistant, "_list_accessible_assistant_bindings", lambda db, user: [binding])
    scope = assistant._resolve_assistant_scope(None, account())
    assert "new_brand" in scope["brands"]
    assert assistant._resolve_assistant_binding_for_request(None, account(), scope, "new_brand", None) == ("new_brand", None)


@pytest.fixture
def permission_client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    previous = dict(SessionLocal.kw)
    SessionLocal.configure(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    user = account()
    with SessionLocal() as db:
        db.add(user)
        db.add(NoResultQuery(brand="lumen", query_text="包", search_pv=10))
        db.commit()
    monkeypatch.setitem(BRAND_CONFIGS, "lumen", {})
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: user)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        SessionLocal.configure(**previous)
        engine.dispose()


def test_new_brand_is_visible_and_can_be_assigned_without_editing_admin(permission_client):
    client = permission_client
    me = client.get("/api/auth/me")
    assert "lumen" in me.json()["brandPermissions"]
    assert client.get("/api/dashboard/no-result-governance?brand=lumen").json()["total"] == 1
    assert client.get("/api/dashboard/blacklist-logs?brand=lumen").status_code == 200
    created = client.post("/api/admin/users", json={
        "username": "lumen-member", "role": "brand_member",
        "pagePermissions": ["/ops-dashboard"], "brandPermissions": ["lumen"],
    })
    assert created.status_code == 200, created.text
    users = client.get("/api/admin/users").json()
    assert next(u for u in users if u["username"] == "lumen-member")["brandPermissions"] == ["lumen"]
