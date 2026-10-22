import unittest
import os
import json
from copy import deepcopy
import datetime
import asyncio
import threading
import time
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from unittest.mock import ANY, patch, MagicMock
from fastapi import HTTPException
import bcrypt

from src.persistence import (
    SessionLocal,
    Base,
    User,
    DailyAbnormalQuery,
    ActionLog,
    InventoryRecoveryTask,
    TaskRunSession,
    AssistantBrandSpace,
    AssistantAgentBinding,
    AssistantUserBinding,
    AssistantSessionMeta,
    BrandDailyMetric,
    AssistantActionRequest,
    NoResultGovernanceBatch,
    NoResultGovernanceAction,
    NoResultQuery,
    TopQueryTask,
    TopQueryActionPlan,
    ActionAttempt,
    ActionIntent as ActionIntentFact,
    ActionReadback,
    ActionTerminalFact,
)
from src.api import app, get_current_user, service
from importlib import import_module
api_module = import_module("src.api.app")
dashboard_module = import_module("src.api.application.dashboard")
health_module = import_module("src.api.routers.health")
assistant_module = import_module("src.api.application.assistant")
from src.llm.node_config import NODE_REGISTRY
from src.llm.config_store import create_draft, publish_draft


def _persist_and_reload_config(raw_config):
    def persist(candidate):
        persisted = deepcopy(candidate)
        raw_config.clear()
        raw_config.update(persisted)

    return persist


def test_health_endpoint_does_not_block_event_loop_on_stuck_database(monkeypatch):
    monkeypatch.setenv("HEALTH_CHECK_DB_TIMEOUT_SECONDS", "0.01")
    release = threading.Event()
    finished = threading.Event()
    started = threading.Event()

    def blocked_database_check():
        started.set()
        try:
            release.wait(timeout=1)
            return True
        finally:
            finished.set()

    db_check = MagicMock(side_effect=blocked_database_check)
    with patch.object(api_module, "check_database_connection", db_check), patch.object(
        api_module, "get_latest_task_session", return_value=None
    ), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        return_value={"available": True, "is_circuit_broken": False},
    ):
        loop = asyncio.new_event_loop()
        try:
            started_at = time.perf_counter()
            payload = loop.run_until_complete(health_module.health_check())
            assert started.wait(timeout=1)
            second_payload = loop.run_until_complete(health_module.health_check())
            elapsed = time.perf_counter() - started_at
        finally:
            loop.close()

    try:
        assert elapsed < 0.1
        assert payload["database"] == {"connected": False}
        assert payload["releaseReadiness"]["ready"] is False
        assert payload["releaseReadiness"]["components"]["database"] == {"connected": False}
        assert payload["healthy"] is False
        assert second_payload["healthy"] is False
        assert db_check.call_count == 1
    finally:
        release.set()
        assert finished.wait(timeout=1)

class StubNoResultKGClient:
    """No Result 执行链路的 KG 桩：回读契约下 snapshot/build_rollback 由真实客户端承担。"""

    def snapshot(self, action):
        return {}

    def build_rollback(self, action, snapshot, response):
        return {}


class TestAPIExtended(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_session_bind = SessionLocal.kw.get("bind")
        cls.engine = create_engine(
            "sqlite://", 
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        SessionLocal.configure(bind=cls.engine)
        
        def override_get_user():
            return User(
                id=1, 
                username="test_admin", 
                is_admin=1, 
                page_permissions=json.dumps(["/", "/task-board", "/ops-dashboard", "/smart-assistant", "/blacklist", "/demotion", "/stock-tracking", "/no-result-governance", "/account-management", "/config"]),
                brand_permissions=json.dumps(["atlas", "summit", "harbor", "meadow", "riviera"])
            )
        cls.admin_override = override_get_user
        app.dependency_overrides[get_current_user] = override_get_user
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        SessionLocal.configure(bind=cls.previous_session_bind)
        cls.engine.dispose()
        app.dependency_overrides.clear()

    def setUp(self):
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.add(User(
            id=1,
            username="test_admin",
            password_hash="hash",
            is_admin=1,
            role="platform_admin",
            is_active=1,
            page_permissions=json.dumps([
                "/", "/task-board", "/ops-dashboard", "/smart-assistant",
                "/blacklist", "/demotion", "/stock-tracking",
                "/no-result-governance", "/account-management", "/config",
            ]),
            brand_permissions=json.dumps(
                ["atlas", "summit", "harbor", "meadow", "riviera"]
            ),
            action_permissions="[]",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_login_success_and_fail(self):
        h = bcrypt.hashpw("p123".encode(), bcrypt.gensalt()).decode()
        u = User(username="admin_user", password_hash=h, is_active=1, page_permissions="[]", brand_permissions="[]")
        self.db.add(u)
        self.db.commit()
        
        # 1. Success
        resp = self.client.post("/api/auth/login", json={"username": "admin_user", "password": "p123"})
        self.assertEqual(resp.status_code, 200)
        
        # 2. Bad password
        resp = self.client.post("/api/auth/login", json={"username": "admin_user", "password": "bad"})
        self.assertEqual(resp.status_code, 400)

    def test_old_token_is_rejected_after_security_state_changes(self):
        security_changes = {
            "account_disabled": ("is_active", 0),
            "password_changed": ("password_hash", bcrypt.hashpw("new-pass".encode(), bcrypt.gensalt()).decode()),
            "role_changed": ("role", "brand_admin"),
            "page_permissions_changed": ("page_permissions", '["/smart-assistant"]'),
            "brand_permissions_changed": ("brand_permissions", '["harbor"]'),
            "action_permissions_changed": ("action_permissions", '["execute_governance_actions"]'),
        }
        previous_override = app.dependency_overrides.pop(get_current_user, None)
        try:
            for index, (case_name, (field, value)) in enumerate(security_changes.items(), start=1):
                with self.subTest(case=case_name):
                    username = f"revocation_user_{index}"
                    user = User(
                        username=username,
                        password_hash=bcrypt.hashpw("old-pass".encode(), bcrypt.gensalt()).decode(),
                        is_active=1,
                        is_admin=0,
                        role="brand_member",
                        page_permissions='["/ops-dashboard"]',
                        brand_permissions='["atlas"]',
                        action_permissions="[]",
                    )
                    self.db.add(user)
                    self.db.commit()
                    login_response = self.client.post(
                        "/api/auth/login",
                        json={"username": username, "password": "old-pass"},
                    )
                    self.assertEqual(login_response.status_code, 200)
                    token = login_response.json()["access_token"]

                    setattr(user, field, value)
                    self.db.commit()
                    response = self.client.get(
                        "/api/auth/me",
                        headers={"Authorization": f"Bearer {token}"},
                    )

                    self.assertEqual(response.status_code, 401)
        finally:
            if previous_override is not None:
                app.dependency_overrides[get_current_user] = previous_override

    def test_legacy_token_without_authorization_version_is_rejected_without_token_logging(self):
        user = User(
            username="legacy_token_user",
            password_hash=bcrypt.hashpw("pass".encode(), bcrypt.gensalt()).decode(),
            is_active=1,
            role="brand_member",
            page_permissions='["/ops-dashboard"]',
            brand_permissions='["atlas"]',
            action_permissions="[]",
        )
        self.db.add(user)
        self.db.commit()
        legacy_token = api_module.jwt.encode(
            {
                "sub": user.username,
                "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
            },
            api_module.SECRET_KEY,
            algorithm=api_module.ALGORITHM,
        )

        previous_override = app.dependency_overrides.pop(get_current_user, None)
        try:
            with patch.object(api_module.logger, "warning") as mock_warning:
                response = self.client.get(
                    "/api/auth/me",
                    headers={"Authorization": f"Bearer {legacy_token}"},
                )
        finally:
            if previous_override is not None:
                app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 401)
        self.assertTrue(mock_warning.called)
        self.assertNotIn(legacy_token, repr(mock_warning.call_args_list))

    def test_logout_revokes_preexisting_tokens(self):
        user = User(
            username="logout_user",
            password_hash=bcrypt.hashpw("pass".encode(), bcrypt.gensalt()).decode(),
            is_active=1,
            role="brand_member",
            page_permissions='["/ops-dashboard"]',
            brand_permissions='["atlas"]',
            action_permissions="[]",
        )
        self.db.add(user)
        self.db.commit()
        previous_override = app.dependency_overrides.pop(get_current_user, None)
        try:
            login_response = self.client.post(
                "/api/auth/login",
                json={"username": user.username, "password": "pass"},
            )
            self.assertEqual(login_response.status_code, 200)
            token = login_response.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            logout_response = self.client.post("/api/auth/logout", headers=headers)
            self.assertEqual(logout_response.status_code, 200)
            self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 401)
        finally:
            if previous_override is not None:
                app.dependency_overrides[get_current_user] = previous_override

    def test_logout_all_is_not_exposed(self):
        response = self.client.post("/api/auth/logout-all")

        self.assertEqual(response.status_code, 404)

    def test_session_revocation_retries_after_concurrent_version_change(self):
        previous = datetime.datetime(2026, 7, 29, 9, 0, 0)
        concurrent_version = previous + datetime.timedelta(seconds=1)
        first_user = User(id=41, username="concurrent_revoke", updated_at=previous)
        second_user = User(id=41, username="concurrent_revoke", updated_at=concurrent_version)

        first_db = MagicMock()
        first_db.get.return_value = first_user
        first_update = first_db.query.return_value.filter.return_value.update
        first_update.return_value = 0

        second_db = MagicMock()
        second_db.get.return_value = second_user
        second_update = second_db.query.return_value.filter.return_value.update
        second_update.return_value = 1

        with patch.object(api_module, "SessionLocal", side_effect=[first_db, second_db]) as session_factory:
            result = api_module._revoke_all_user_sessions(41, event="logout")

        self.assertEqual(result, {"msg": "All sessions revoked"})
        self.assertEqual(session_factory.call_count, 2)
        first_db.rollback.assert_called_once_with()
        first_db.commit.assert_not_called()
        second_db.commit.assert_called_once_with()
        first_db.close.assert_called_once_with()
        second_db.close.assert_called_once_with()

        update_values = second_update.call_args.args[0]
        self.assertGreater(update_values[User.updated_at], concurrent_version)

    def test_platform_admin_can_force_revoke_user_sessions(self):
        admin = User(
            username="revocation_admin",
            password_hash=bcrypt.hashpw("admin-pass".encode(), bcrypt.gensalt()).decode(),
            is_active=1,
            is_admin=1,
            role="platform_admin",
            page_permissions='["/account-management"]',
            brand_permissions='["atlas"]',
            action_permissions="[]",
        )
        member = User(
            username="revoked_member",
            password_hash=bcrypt.hashpw("member-pass".encode(), bcrypt.gensalt()).decode(),
            is_active=1,
            role="brand_member",
            page_permissions='["/ops-dashboard"]',
            brand_permissions='["atlas"]',
            action_permissions="[]",
        )
        self.db.add_all([admin, member])
        self.db.commit()
        previous_override = app.dependency_overrides.pop(get_current_user, None)
        try:
            admin_token = self.client.post(
                "/api/auth/login",
                json={"username": admin.username, "password": "admin-pass"},
            ).json()["access_token"]
            member_token = self.client.post(
                "/api/auth/login",
                json={"username": member.username, "password": "member-pass"},
            ).json()["access_token"]

            revoke_response = self.client.post(
                f"/api/admin/users/{member.id}/revoke-sessions",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            member_response = self.client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {member_token}"},
            )
        finally:
            if previous_override is not None:
                app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(revoke_response.status_code, 200)
        self.assertEqual(member_response.status_code, 401)

    def test_user_management_exhaustive(self):
        # 1. Create
        payload = {"username": "u3", "isAdmin": True, "pagePermissions": ["/"], "brandPermissions": ["atlas"]}
        resp = self.client.post("/api/admin/users", json=payload)
        self.assertEqual(resp.status_code, 200)
        
        u3 = self.db.query(User).filter(User.username == "u3").one()
        
        # 2. Update
        update = {
            "isAdmin": False,
            "isActive": False,
            "pagePermissions": ["/config"],
            "brandPermissions": ["harbor"],
        }
        resp = self.client.put(f"/api/admin/users/{u3.id}", json=update)
        self.assertEqual(resp.status_code, 200)
        self.db.refresh(u3)
        self.assertEqual(u3.is_active, 0)
        
        # 3. Delete
        resp = self.client.delete(f"/api/admin/users/{u3.id}")
        self.assertEqual(resp.status_code, 200)
        
        # Verify gone
        u_after = self.db.query(User).filter(User.id == u3.id).first()
        self.assertIsNone(u_after)

    def test_dashboard_logs_and_tasks(self):
        l1 = ActionLog(brand="atlas", query_text="q1", product_id="S1", product_name="N1", action_type=1, created_at=datetime.datetime.now())
        t1 = InventoryRecoveryTask(brand="atlas", product_id="S1", original_query="Q1", status="monitoring", created_at=datetime.datetime.now(), updated_at=datetime.datetime.now())
        self.db.add_all([l1, t1])
        self.db.commit()
    
        self.assertEqual(len(self.client.get("/api/dashboard/blacklist-logs").json()["items"]), 1)
        self.assertEqual(len(self.client.get("/api/dashboard/stock-tasks").json()["items"]), 1)









    def test_get_me_and_change_password(self):
        password_hash = bcrypt.hashpw("old-pass".encode(), bcrypt.gensalt()).decode()
        user = User(
            id=9,
            username="password_user",
            password_hash=password_hash,
            is_active=1,
            is_admin=0,
            page_permissions='["/"]',
            brand_permissions='["atlas"]',
        )
        self.db.add(user)
        self.db.commit()

        async def override_get_user():
            return User(
                id=9,
                username="password_user",
                password_hash=password_hash,
                is_admin=0,
                page_permissions='["/"]',
                brand_permissions='["atlas"]',
            )

        app.dependency_overrides[get_current_user] = override_get_user
        try:
            me_resp = self.client.get("/api/auth/me")
            self.assertEqual(me_resp.status_code, 200)
            self.assertEqual(me_resp.json()["username"], "password_user")

            fail_resp = self.client.post("/api/auth/change-password", json={"oldPassword": "bad", "newPassword": "new-pass"})
            self.assertEqual(fail_resp.status_code, 400)

            success_resp = self.client.post("/api/auth/change-password", json={"oldPassword": "old-pass", "newPassword": "new-pass"})
            self.assertEqual(success_resp.status_code, 200)

            updated_user = self.db.get(User, 9)
            self.assertTrue(bcrypt.checkpw("new-pass".encode(), updated_user.password_hash.encode()))
        finally:
            app.dependency_overrides[get_current_user] = self.__class__.admin_override

    def test_non_admin_user_management_and_config_forbidden(self):
        async def override_non_admin():
            return User(
                id=2,
                username="operator",
                is_admin=0,
                page_permissions='["/"]',
                brand_permissions='["atlas"]',
            )

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_non_admin
        try:
            self.assertEqual(self.client.get("/api/admin/users").status_code, 403)
            self.assertEqual(self.client.post("/api/admin/users", json={"username": "xx"}).status_code, 403)
            self.assertEqual(self.client.put("/api/admin/users/1", json={}).status_code, 403)
            self.assertEqual(self.client.post("/api/admin/users/1/reset-password").status_code, 403)
            self.assertEqual(self.client.post("/api/admin/users/1/revoke-sessions").status_code, 403)
            self.assertEqual(self.client.delete("/api/admin/users/1").status_code, 403)
            self.assertEqual(self.client.post("/api/admin/config", json={}).status_code, 403)
        finally:
            app.dependency_overrides[get_current_user] = previous_override

    def test_brand_admin_can_only_manage_brand_members_within_owned_brands(self):
        atlas_member = User(
            username="atlas_member",
            password_hash="hash",
            role="brand_member",
            page_permissions='["/ops-dashboard"]',
            brand_permissions='["atlas"]',
            action_permissions='[]',
        )
        harbor_member = User(
            username="harbor_member",
            password_hash="hash",
            role="brand_member",
            page_permissions='["/ops-dashboard"]',
            brand_permissions='["harbor"]',
            action_permissions='[]',
        )
        self.db.add_all([atlas_member, harbor_member])
        self.db.commit()

        async def override_brand_admin():
            return User(
                id=12,
                username="atlas_admin",
                is_admin=0,
                role="brand_admin",
                page_permissions='["/account-management", "/config"]',
                brand_permissions='["atlas"]',
                action_permissions='["manage_brand_users", "manage_brand_config"]',
            )

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_brand_admin
        try:
            list_response = self.client.get("/api/admin/users")
            self.assertEqual(list_response.status_code, 200)
            listed_names = {item["username"] for item in list_response.json()}
            self.assertEqual(listed_names, {"atlas_member"})

            create_response = self.client.post("/api/admin/users", json={
                "username": "delegated_member",
                "role": "brand_admin",
                "pagePermissions": ["/ops-dashboard"],
                "brandPermissions": ["atlas", "harbor"],
                "actionPermissions": ["manage_brand_config"],
            })
            self.assertEqual(create_response.status_code, 200)

            created_user = self.db.query(User).filter(User.username == "delegated_member").one()
            self.assertEqual(created_user.role, "brand_member")
            self.assertEqual(json.loads(created_user.brand_permissions), ["atlas"])
            self.assertEqual(json.loads(created_user.action_permissions or "[]"), [])

            update_response = self.client.put(f"/api/admin/users/{harbor_member.id}", json={
                "brandPermissions": ["harbor"],
            })
            self.assertEqual(update_response.status_code, 403)
        finally:
            app.dependency_overrides[get_current_user] = previous_override

    def test_brand_admin_config_is_limited_to_owned_brands_even_with_platform_config_action(self):
        raw_config = {
            "safety_config": {"circuit_breaker_threshold": 10},
            "brand_configs": {
                "atlas": {"enabled": True, "query_governance_extract_limit": 150},
                "harbor": {"enabled": True, "query_governance_extract_limit": 80},
            },
        }

        async def override_brand_admin():
            return User(
                id=13,
                username="atlas_admin",
                is_admin=0,
                role="brand_admin",
                page_permissions='["/config"]',
                brand_permissions='["atlas"]',
                action_permissions='["manage_brand_config", "manage_platform_config"]',
            )

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_brand_admin
        try:
            with patch.object(api_module, "_raw_config", raw_config), \
                 patch.object(api_module, "GLOBAL_SAFETY_CONFIG", raw_config["safety_config"]), \
                 patch.object(api_module, "BRAND_CONFIGS", raw_config["brand_configs"]), \
                 patch.object(api_module, "get_brand_business_config", side_effect=lambda brand: raw_config["brand_configs"][brand]), \
                 patch.object(api_module, "reload_config"), \
                 patch.object(api_module, "save_config", side_effect=_persist_and_reload_config(raw_config)) as mock_save:
                get_response = self.client.get("/api/admin/config")
                self.assertEqual(get_response.status_code, 200)
                self.assertNotIn("safety_config", get_response.json())
                self.assertEqual(set(get_response.json()["brand_configs"].keys()), {"atlas"})

                update_response = self.client.post("/api/admin/config", json={
                    "safety_config": {"circuit_breaker_threshold": 99},
                    "brand_configs": {
                        "atlas": {"query_governance_extract_limit": 120},
                    },
                })
                self.assertEqual(update_response.status_code, 200)
                self.assertEqual(raw_config["safety_config"]["circuit_breaker_threshold"], 10)
                self.assertEqual(raw_config["brand_configs"]["atlas"]["query_governance_extract_limit"], 120)
                self.assertEqual(raw_config["brand_configs"]["harbor"]["query_governance_extract_limit"], 80)
                mock_save.assert_called_once()
        finally:
            app.dependency_overrides[get_current_user] = previous_override

    def test_query_selection_config_is_persisted_and_invalid_values_are_rejected(self):
        raw_config = {
            "brand_configs": {
                "atlas": {"enabled": True},
            },
        }
        selection_config = {
            "query_governance_extract_limit": 42,
            "query_governance_query_min_metric_type": "uv",
            "query_governance_query_min_metric_value": 8,
            "query_governance_preference_top_purchase_product_limit": 5,
            "query_governance_preference_top_click_product_limit": 6,
            "query_governance_candidate_high_pv_limit": 30,
            "query_governance_candidate_alert_ctr_gap_pp": 4.5,
            "query_governance_candidate_alert_cvr_gap_pp": 1.5,
            "query_governance_candidate_history_days": 14,
            "query_governance_candidate_new_query_pv_threshold": 6,
            "query_governance_candidate_pv_growth_ratio_floor": 1.3,
            "query_governance_candidate_pv_growth_sqrt_multiplier": 2.5,
            "query_governance_candidate_pv_growth_abs_floor": 7,
        }

        with patch.object(api_module, "_raw_config", raw_config), \
             patch.object(api_module, "BRAND_CONFIGS", raw_config["brand_configs"]), \
             patch.object(api_module, "reload_config"), \
             patch.object(api_module, "save_config", side_effect=_persist_and_reload_config(raw_config)) as mock_save:
            response = self.client.post(
                "/api/admin/config",
                json={"brand_configs": {"atlas": selection_config}},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                {key: raw_config["brand_configs"]["atlas"][key] for key in selection_config},
                selection_config,
            )
            mock_save.assert_called_once()

            invalid = self.client.post(
                "/api/admin/config",
                json={
                    "brand_configs": {
                        "atlas": {"query_governance_preference_top_click_product_limit": 0}
                    }
                },
            )
            self.assertEqual(invalid.status_code, 400)
            mock_save.assert_called_once()

    def test_platform_admin_cannot_grant_platform_config_action_to_non_platform_role(self):
        response = self.client.post("/api/admin/users", json={
            "username": "atlas_config_member",
            "role": "brand_admin",
            "pagePermissions": ["/config"],
            "brandPermissions": ["atlas"],
            "actionPermissions": ["manage_brand_config", "manage_platform_config"],
        })
        self.assertEqual(response.status_code, 200)

        created_user = self.db.query(User).filter(User.username == "atlas_config_member").one()
        self.assertEqual(created_user.role, "brand_admin")
        self.assertEqual(
            json.loads(created_user.action_permissions),
            ["manage_brand_config"],
        )

    def test_brand_member_keeps_brand_config_action_but_cannot_use_platform_config_action(self):
        raw_config = {
            "safety_config": {"circuit_breaker_threshold": 10},
            "brand_configs": {
                "atlas": {"enabled": True, "query_governance_extract_limit": 150},
                "harbor": {"enabled": True, "query_governance_extract_limit": 80},
            },
        }

        async def override_brand_member():
            return User(
                id=14,
                username="atlas_member",
                is_admin=0,
                role="brand_member",
                page_permissions='["/config"]',
                brand_permissions='["atlas"]',
                action_permissions='["manage_brand_config", "manage_platform_config"]',
            )

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_brand_member
        try:
            with patch.object(api_module, "_raw_config", raw_config), \
                 patch.object(api_module, "GLOBAL_SAFETY_CONFIG", raw_config["safety_config"]), \
                 patch.object(api_module, "BRAND_CONFIGS", raw_config["brand_configs"]), \
                 patch.object(api_module, "get_brand_business_config", side_effect=lambda brand: raw_config["brand_configs"][brand]), \
                 patch.object(api_module, "reload_config"), \
                 patch.object(api_module, "save_config", side_effect=_persist_and_reload_config(raw_config)) as mock_save:
                me_response = self.client.get("/api/auth/me")
                self.assertEqual(me_response.status_code, 200)
                self.assertEqual(me_response.json()["actionPermissions"], ["manage_brand_config"])

                get_response = self.client.get("/api/admin/config")
                self.assertEqual(get_response.status_code, 200)
                self.assertNotIn("safety_config", get_response.json())
                self.assertEqual(set(get_response.json()["brand_configs"].keys()), {"atlas"})

                update_response = self.client.post("/api/admin/config", json={
                    "safety_config": {"circuit_breaker_threshold": 99},
                    "brand_configs": {
                        "atlas": {"query_governance_extract_limit": 120},
                    },
                })
                self.assertEqual(update_response.status_code, 200)
                self.assertEqual(raw_config["safety_config"]["circuit_breaker_threshold"], 10)
                self.assertEqual(raw_config["brand_configs"]["atlas"]["query_governance_extract_limit"], 120)
                self.assertEqual(raw_config["brand_configs"]["harbor"]["query_governance_extract_limit"], 80)
                mock_save.assert_called_once()
        finally:
            app.dependency_overrides[get_current_user] = previous_override

    def test_single_brand_config_update_rejects_cross_brand_payload_atomically(self):
        raw_config = {
            "brand_configs": {
                "atlas": {"enabled": True, "query_governance_extract_limit": 150},
                "harbor": {"enabled": True, "query_governance_extract_limit": 80},
            },
        }

        async def override_brand_member():
            return User(
                id=15,
                username="atlas_config_member",
                is_admin=0,
                role="brand_member",
                page_permissions='["/config"]',
                brand_permissions='["atlas"]',
                action_permissions='["manage_brand_config"]',
            )

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_brand_member
        try:
            with patch.object(api_module, "_raw_config", raw_config), \
                 patch.object(api_module, "BRAND_CONFIGS", raw_config["brand_configs"]), \
                 patch.object(api_module, "save_config") as mock_save:
                response = self.client.post("/api/admin/config", json={
                    "brand_configs": {
                        "atlas": {"query_governance_extract_limit": 120},
                        "harbor": {"query_governance_extract_limit": 10},
                    },
                })

                self.assertEqual(response.status_code, 403)
                self.assertEqual(raw_config["brand_configs"]["atlas"]["query_governance_extract_limit"], 150)
                self.assertEqual(raw_config["brand_configs"]["harbor"]["query_governance_extract_limit"], 80)
                mock_save.assert_not_called()
        finally:
            app.dependency_overrides[get_current_user] = previous_override

    def test_multi_brand_operator_can_chat_without_client_brand_selection(self):
        operator = User(
            id=41,
            username="multi_brand_operator",
            password_hash="hash",
            is_admin=0,
            role="brand_member",
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas", "harbor"]',
            action_permissions='[]',
        )
        self.db.add(operator)
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            response = self.client.post("/api/assistant/chat", json={
                "message": "帮我看下今天的问题",
                "sessionId": "multi-brand-no-selection",
            })
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["runtime"]["channel"], "pi_agent")

    def test_admin_config_exposes_and_updates_no_result_governance_fields(self):
        from importlib import import_module
        api_module = import_module("src.api.app")

        raw_config = {
            "safety_config": {},
            "brand_configs": {
                "harbor": {
                    "enabled": True,
                    "no_result_governance_enabled": True,
                    "no_result_execution_mode": "dry_run",
                    "no_result_extract_limit": 25,
                    "no_result_search_pv_threshold": 5,
                    "no_result_search_uv_threshold": 3,
                    "no_result_excluded_keywords": ["积分", "门店"],
                    "no_result_auto_max_risk": "medium",
                    "no_result_retag_cooldown_hours": 2,
                    "no_result_review_wait_minutes": 30,
                    "no_result_top_n": 16,
                    "no_result_agent_max_rounds": 4,
                    "no_result_ignore_ttl_days": 14,
                    "top_query_inventory_max_cycles": 3,
                }
            },
        }

        with patch.object(api_module, "_raw_config", raw_config), \
             patch.object(api_module, "GLOBAL_SAFETY_CONFIG", raw_config["safety_config"]), \
             patch.object(api_module, "BRAND_CONFIGS", raw_config["brand_configs"]), \
             patch.object(api_module, "get_brand_business_config", side_effect=lambda brand: raw_config["brand_configs"][brand]), \
             patch.object(api_module, "reload_config"), \
             patch.object(api_module, "save_config", side_effect=_persist_and_reload_config(raw_config)) as mock_save:
            response = self.client.get("/api/admin/config")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("global_config", response.json())
            harbor_config = response.json()["brand_configs"]["harbor"]
            self.assertTrue(harbor_config["no_result_governance_enabled"])
            self.assertEqual(harbor_config["no_result_execution_mode"], "dry_run")
            self.assertEqual(harbor_config["no_result_auto_max_risk"], "medium")
            self.assertEqual(harbor_config["no_result_excluded_keywords"], ["积分", "门店"])
            self.assertEqual(harbor_config["top_query_inventory_max_cycles"], 3)

            update = self.client.post("/api/admin/config", json={
                "brand_configs": {
                    "harbor": {
                        "no_result_extract_limit": 40,
                        "no_result_execution_mode": "real_write",
                        "no_result_auto_max_risk": "high",
                        "no_result_review_wait_minutes": 45,
                        "no_result_excluded_keywords": ["抽奖"],
                        "top_query_inventory_max_cycles": 4,
                    }
                }
            })

            self.assertEqual(update.status_code, 200)
            self.assertEqual(raw_config["brand_configs"]["harbor"]["no_result_extract_limit"], 40)
            self.assertEqual(raw_config["brand_configs"]["harbor"]["no_result_execution_mode"], "real_write")
            self.assertEqual(raw_config["brand_configs"]["harbor"]["no_result_auto_max_risk"], "high")
            self.assertEqual(raw_config["brand_configs"]["harbor"]["no_result_review_wait_minutes"], 45)
            self.assertEqual(raw_config["brand_configs"]["harbor"]["no_result_excluded_keywords"], ["抽奖"])
            self.assertEqual(raw_config["brand_configs"]["harbor"]["top_query_inventory_max_cycles"], 4)
            mock_save.assert_called_once()

            invalid = self.client.post("/api/admin/config", json={
                "brand_configs": {"harbor": {"top_query_inventory_max_cycles": 0}}
            })
            self.assertEqual(invalid.status_code, 400)

    def test_user_management_not_found_and_admin_delete_guard(self):
        admin = User(
            username="admin",
            password_hash=bcrypt.hashpw("p".encode(), bcrypt.gensalt()).decode(),
            is_active=1,
            is_admin=1,
            page_permissions='["/"]',
            brand_permissions='["atlas"]',
        )
        self.db.add(admin)
        self.db.commit()

        self.assertEqual(self.client.put("/api/admin/users/999", json={"pagePermissions": ["/"]}).status_code, 404)
        self.assertEqual(self.client.post("/api/admin/users/999/reset-password").status_code, 404)
        self.assertEqual(self.client.delete("/api/admin/users/999").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/admin/users/{admin.id}").status_code, 400)

    def test_demoted_logs_and_manual_attention(self):
        demote_log = ActionLog(
            brand="atlas",
            query_text="dress",
            product_id="SKU1",
            product_name="Dress",
            action_type=2,
            reason="coverage low",
            created_at=datetime.datetime.now(),
        )
        recover_log = ActionLog(
            brand="atlas",
            query_text="dress",
            product_id="SKU1",
            product_name="Dress",
            action_type=3,
            reason="recovered",
            created_at=datetime.datetime.now() + datetime.timedelta(seconds=1),
        )
        q = DailyAbnormalQuery(
            brand="atlas",
            query_text="dress",
            anomaly_type="low_ctr",
            action_taken="blacklisted",
            ops_effect="failed",
            ctr=0.01,
            cvr=0.01,
            created_at=datetime.datetime.now(),
            data_date=datetime.date.today(),
        )
        self.db.add_all([demote_log, recover_log, q])
        self.db.commit()

        demoted_resp = self.client.get("/api/dashboard/demoted-logs")
        self.assertEqual(demoted_resp.status_code, 200)
        self.assertTrue(demoted_resp.json()["items"][0]["isCanceled"])

    def test_stock_task_toggle_status_branches(self):
        task = InventoryRecoveryTask(
            brand="harbor",
            product_id="SKU-HARBOR",
            product_name="HARBOR Product",
            original_query="harbor query",
            last_check_stock=20,
            status="monitoring",
            created_at=datetime.datetime.now(),
            updated_at=datetime.datetime.now(),
        )
        self.db.add(task)
        self.db.commit()

        invalid_resp = self.client.post(f"/api/dashboard/stock-tasks/{task.id}/toggle-status", json={"status": "bad"})
        self.assertEqual(invalid_resp.status_code, 400)

        not_found_resp = self.client.post("/api/dashboard/stock-tasks/9999/toggle-status", json={"status": "monitoring"})
        self.assertEqual(not_found_resp.status_code, 404)

        async def override_atlas_only():
            return User(
                id=3,
                username="atlas_operator",
                is_admin=0,
                page_permissions='["/stock-tracking"]',
                brand_permissions='["atlas"]',
            )

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_atlas_only
        try:
            forbidden_resp = self.client.post(
                f"/api/dashboard/stock-tasks/{task.id}/toggle-status",
                json={"status": "manually_stopped"},
            )
            self.assertEqual(forbidden_resp.status_code, 403)
        finally:
            app.dependency_overrides[get_current_user] = previous_override

    def test_dashboard_brand_filters_and_task_toggle_success(self):
        atlas_log = ActionLog(
            brand="atlas",
            query_text="tops",
            product_id="Z1",
            product_name="Atlas Top",
            action_type=1,
            created_at=datetime.datetime.now(),
        )
        harbor_log = ActionLog(
            brand="harbor",
            query_text="tops",
            product_id="H1",
            product_name="HARBOR Top",
            action_type=1,
            created_at=datetime.datetime.now(),
        )
        task = InventoryRecoveryTask(
            brand="atlas",
            product_id="SKU-ATLAS",
            product_name="Atlas Product",
            original_query="tops",
            last_check_stock=55,
            status="monitoring",
            created_at=datetime.datetime.now(),
            updated_at=datetime.datetime.now(),
        )
        stopped_task = InventoryRecoveryTask(
            brand="atlas",
            product_id="SKU-STOP",
            product_name="Stopped Product",
            original_query="stopped query",
            last_check_stock=15,
            status="auto_stopped",
            created_at=datetime.datetime.now(),
            updated_at=datetime.datetime.now(),
        )
        self.db.add_all([atlas_log, harbor_log, task, stopped_task])
        self.db.commit()

        blacklist_resp = self.client.get("/api/dashboard/blacklist-logs", params={"brand": "harbor"})
        self.assertEqual(blacklist_resp.status_code, 200)
        self.assertEqual(blacklist_resp.json()["total"], 1)

        async def override_atlas_only():
            return User(
                id=4,
                username="atlas_only",
                is_admin=0,
                page_permissions='["/blacklist", "/stock-tracking", "/task-board"]',
                brand_permissions='["atlas"]',
                action_permissions='["execute_governance_actions"]',
            )

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_atlas_only
        try:
            forbidden_brand_resp = self.client.get("/api/dashboard/blacklist-logs", params={"brand": "harbor"})
            self.assertEqual(forbidden_brand_resp.status_code, 200)
            self.assertEqual(forbidden_brand_resp.json()["total"], 0)

            stock_resp = self.client.get("/api/dashboard/stock-tasks", params={"status": "stopped"})
            self.assertEqual(stock_resp.status_code, 200)
            self.assertEqual(stock_resp.json()["total"], 1)

            toggle_resp = self.client.post(
                f"/api/dashboard/stock-tasks/{task.id}/toggle-status",
                json={"status": "manually_stopped"},
            )
            self.assertEqual(toggle_resp.status_code, 200)
            self.db.close()
            self.db = SessionLocal()
            updated_task = self.db.get(InventoryRecoveryTask, task.id)
            self.assertEqual(updated_task.status, "manually_stopped")
        finally:
            app.dependency_overrides[get_current_user] = previous_override

    def test_cancel_actions_branches(self):
        missing_resp = self.client.post("/api/dashboard/actions/cancel-blacklist", json={"brand": "atlas"})
        self.assertEqual(missing_resp.status_code, 400)

        forbidden_resp = self.client.post(
            "/api/dashboard/actions/cancel-demotion",
            json={"brand": "other", "query": "q", "sku": "s"},
        )
        self.assertEqual(forbidden_resp.status_code, 403)

        with patch.object(service, "remove_from_blacklist", return_value=False):
            failed_blacklist = self.client.post(
                "/api/dashboard/actions/cancel-blacklist",
                json={"brand": "atlas", "query": "q", "sku": "s"},
            )
            self.assertEqual(failed_blacklist.status_code, 500)

        with patch.object(service, "sync_broken_code_to_redis", return_value=False):
            failed_demotion = self.client.post(
                "/api/dashboard/actions/cancel-demotion",
                json={"brand": "atlas", "query": "q", "sku": "s"},
            )
            self.assertEqual(failed_demotion.status_code, 500)

    def test_cancel_blacklist_batch_runs_independent_items_concurrently(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_cancel(*, sku, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return f"request-{sku}"

        with patch.object(dashboard_module, "_cancel_blacklist_via_gateway", side_effect=fake_cancel):
            response = self.client.post(
                "/api/dashboard/actions/cancel-blacklist/batch",
                json={
                    "items": [
                        {"brand": "atlas", "query": "q1", "sku": "s1"},
                        {"brand": "atlas", "query": "q2", "sku": "s2"},
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["succeeded"], 2)
        self.assertEqual(max_active, 2)

    def test_cancel_actions_success(self):
        task = InventoryRecoveryTask(
            brand="atlas",
            product_id="SKU1",
            product_name="Dress",
            original_query="dress",
            last_check_stock=25,
            status="monitoring",
            created_at=datetime.datetime.now(),
            updated_at=datetime.datetime.now(),
        )
        self.db.add(task)
        self.db.commit()

        with patch.object(
            service,
            "remove_from_blacklist",
            return_value=True,
        ), patch.object(
            service,
            "get_top_query_action_status",
            return_value={"status": "not_found", "response": {"matched_product_ids": []}},
        ):
            blacklist_resp = self.client.post(
                "/api/dashboard/actions/cancel-blacklist",
                json={"brand": "atlas", "query": "dress", "sku": "SKU1"},
            )
            self.assertEqual(blacklist_resp.status_code, 200)

        with patch.object(
            service,
            "sync_broken_code_to_redis",
            return_value=True,
        ) as mock_sync, patch.object(
            service,
            "get_ops_low_weight_config",
            return_value={"success": True, "product_ids": [], "version": "v1"},
        ):
            demotion_resp = self.client.post(
                "/api/dashboard/actions/cancel-demotion",
                json={
                    "brand": "atlas",
                    "query": "dress",
                    "sku": "SKU1",
                    "taskId": task.id,
                    "requestId": "dashboard-cancel-demotion-success",
                },
            )
            self.assertEqual(demotion_resp.status_code, 200)
            mock_sync.assert_called_once_with(
                "atlas",
                remove_items={"dress": ["SKU1"]},
                external_request_id="dashboard-cancel-demotion-success",
                allow_prod_recovery=True,
            )

        logs = self.db.query(ActionLog).filter(ActionLog.product_id == "SKU1").order_by(ActionLog.action_type.asc()).all()
        self.assertEqual([log.action_type for log in logs], [3, 5])
        self.db.close()
        self.db = SessionLocal()
        updated_task = self.db.get(InventoryRecoveryTask, task.id)
        self.assertEqual(updated_task.status, "recovered")
        self.assertEqual(updated_task.last_check_stock, -1)

    def test_dashboard_cancel_demotion_gateway_replays_without_duplicate_write(self):
        task = InventoryRecoveryTask(
            brand="atlas",
            product_id="SKU-demotion-1",
            original_query="dress",
            last_check_stock=25,
            status="monitoring",
        )
        self.db.add(task)
        self.db.commit()
        payload = {
            "brand": "atlas",
            "query": "dress",
            "sku": "SKU-demotion-1",
            "taskId": task.id,
            "requestId": "dashboard-cancel-demotion-test-1",
        }

        with patch.object(
            api_module.service,
            "sync_broken_code_to_redis",
            return_value=True,
        ) as mock_execute, patch.object(
            api_module.service,
            "get_ops_low_weight_config",
            return_value={"success": True, "product_ids": [], "version": "v2"},
        ) as mock_readback:
            first = self.client.post("/api/dashboard/actions/cancel-demotion", json=payload)
            replay = self.client.post("/api/dashboard/actions/cancel-demotion", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        mock_execute.assert_called_once_with(
            "atlas",
            remove_items={"dress": ["SKU-demotion-1"]},
            external_request_id="dashboard-cancel-demotion-test-1",
            allow_prod_recovery=True,
        )
        mock_readback.assert_called_once_with("atlas", "dress")
        intent = self.db.query(ActionIntentFact).one()
        self.assertEqual(intent.idempotency_key, "dashboard-cancel-demotion-test-1")
        self.assertEqual(intent.status, "success")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "success")
        self.assertEqual(self.db.query(ActionReadback).one().authoritative, 1)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")
        self.assertEqual(self.db.query(ActionLog).count(), 1)
        self.db.refresh(task)
        self.assertEqual(task.status, "recovered")
        self.assertEqual(task.last_check_stock, -1)

    def test_dashboard_cancel_demotion_readback_failure_is_terminal_and_does_not_project(self):
        task = InventoryRecoveryTask(
            brand="atlas",
            product_id="SKU-demotion-2",
            original_query="dress",
            last_check_stock=25,
            status="monitoring",
        )
        self.db.add(task)
        self.db.commit()
        payload = {
            "brand": "atlas",
            "query": "dress",
            "sku": "SKU-demotion-2",
            "taskId": task.id,
            "requestId": "dashboard-cancel-demotion-test-2",
        }

        with patch.object(
            api_module.service,
            "sync_broken_code_to_redis",
            return_value=True,
        ) as mock_execute, patch.object(
            api_module.service,
            "get_ops_low_weight_config",
            return_value={
                "success": True,
                "product_ids": ["SKU-demotion-2"],
                "version": "v3",
            },
        ) as mock_readback:
            first = self.client.post("/api/dashboard/actions/cancel-demotion", json=payload)
            replay = self.client.post("/api/dashboard/actions/cancel-demotion", json=payload)

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_execute.assert_called_once()
        mock_readback.assert_called_once()
        self.assertEqual(self.db.query(ActionLog).count(), 0)
        self.db.refresh(task)
        self.assertEqual(task.status, "monitoring")
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "failed")
        self.assertEqual(self.db.query(ActionReadback).one().authoritative, 1)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "failed")

    def test_dashboard_cancel_demotion_projection_failure_rolls_back_local_success(self):
        task = InventoryRecoveryTask(
            brand="atlas",
            product_id="SKU-demotion-3",
            original_query="dress",
            last_check_stock=25,
            status="monitoring",
        )
        self.db.add(task)
        self.db.commit()
        payload = {
            "brand": "atlas",
            "query": "dress",
            "sku": "SKU-demotion-3",
            "taskId": task.id,
            "requestId": "dashboard-cancel-demotion-test-3",
        }

        with patch.object(
            api_module.service,
            "sync_broken_code_to_redis",
            return_value=True,
        ) as mock_execute, patch.object(
            api_module.service,
            "get_ops_low_weight_config",
            return_value={"success": True, "product_ids": [], "version": "v4"},
        ) as mock_readback, patch.object(
            api_module,
            "ActionLog",
            side_effect=RuntimeError("projection unavailable"),
        ) as mock_projection:
            with TestClient(app, raise_server_exceptions=False) as client:
                first = client.post("/api/dashboard/actions/cancel-demotion", json=payload)
                replay = client.post("/api/dashboard/actions/cancel-demotion", json=payload)

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_execute.assert_called_once()
        mock_readback.assert_called_once()
        mock_projection.assert_called_once()
        self.assertEqual(self.db.query(ActionLog).count(), 0)
        self.db.refresh(task)
        self.assertEqual(task.status, "monitoring")
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionReadback).one().authoritative, 0)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_dashboard_cancel_demotion_rejects_brand_without_authoritative_write(self):
        with patch.object(
            api_module.service,
            "sync_broken_code_to_redis",
            return_value=True,
        ) as mock_execute:
            response = self.client.post(
                "/api/dashboard/actions/cancel-demotion",
                json={
                    "brand": "summit",
                    "query": "dress",
                    "sku": "SKU-demotion-summit",
                    "requestId": "dashboard-cancel-demotion-summit",
                },
            )

        self.assertEqual(response.status_code, 409)
        mock_execute.assert_not_called()
        self.assertEqual(self.db.query(ActionIntentFact).count(), 0)

    def test_no_result_rejection_gateway_replays_conflicts_and_records_terminal_facts(self):
        query = NoResultQuery(
            brand="atlas",
            query_text="dress",
            data_date=datetime.datetime.now(),
            status="manual_required",
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            brand="atlas",
            query_text="dress",
            action_type="delete_relationship",
            status="manual_review",
            rollback_status="not_required",
            request_payload="{}",
        )
        self.db.add(action)
        self.db.commit()

        payload = {
            "reason": "evidence is insufficient",
            "requestId": "no-result-rejection-gateway-test-1",
        }
        endpoint = f"/api/dashboard/no-result-governance/actions/{action.id}/reject"
        first = self.client.post(endpoint, json=payload)
        replay = self.client.post(endpoint, json=payload)
        conflict = self.client.post(
            endpoint,
            json={**payload, "reason": "different rejection reason"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(first.json(), {"actionId": action.id, "status": "rejected"})
        self.assertEqual(conflict.status_code, 409)

        self.db.refresh(action)
        self.db.refresh(query)
        self.assertEqual(action.status, "rejected")
        self.assertEqual(action.rollback_status, "not_required")
        self.assertEqual(
            action.error_message,
            "rejected by test_admin | evidence is insufficient",
        )
        self.assertEqual(query.status, "manual_required")
        intent = self.db.query(ActionIntentFact).one()
        self.assertEqual(intent.action_type, "no_result_action_reject")
        self.assertEqual(intent.idempotency_key, payload["requestId"])
        self.assertEqual(intent.status, "success")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "success")
        self.assertEqual(self.db.query(ActionReadback).one().authoritative, 1)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")

    def test_no_result_rejection_gateway_rolls_back_when_operator_note_fails(self):
        query = NoResultQuery(
            brand="atlas",
            query_text="dress",
            data_date=datetime.datetime.now(),
            status="manual_required",
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            brand="atlas",
            query_text="dress",
            action_type="delete_relationship",
            status="manual_review",
            rollback_status="not_required",
            request_payload="{}",
        )
        self.db.add(action)
        self.db.commit()

        with patch.object(
            api_module.NoResultGovernanceService,
            "_operator_note",
            side_effect=RuntimeError("operator audit unavailable"),
        ):
            response = self.client.post(
                f"/api/dashboard/no-result-governance/actions/{action.id}/reject",
                json={
                    "reason": "evidence is insufficient",
                    "requestId": "no-result-rejection-audit-failure",
                },
            )

        self.assertEqual(response.status_code, 500)
        self.db.refresh(action)
        self.db.refresh(query)
        self.assertEqual(action.status, "manual_review")
        self.assertEqual(query.status, "manual_required")
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "failed")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "failed")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "failed")
        self.assertEqual(self.db.query(ActionReadback).count(), 0)

    def _create_no_result_approval_action(self):
        batch = NoResultGovernanceBatch(
            brand="atlas",
            batch_date=datetime.datetime.now(),
            status="manual_required",
            total_queries=1,
            changed_queries=0,
        )
        self.db.add(batch)
        self.db.flush()
        query = NoResultQuery(
            batch_id=batch.id,
            brand="atlas",
            query_text="dress",
            data_date=datetime.datetime.now(),
            status="manual_required",
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            batch_id=batch.id,
            brand="atlas",
            query_text="dress",
            action_type="delete_relationship",
            status="manual_review",
            rollback_status="not_required",
            request_payload=json.dumps({"actionType": "delete_relationship"}),
        )
        self.db.add(action)
        self.db.commit()
        return batch, query, action

    def test_no_result_approval_gateway_replays_conflicts_and_records_terminal_facts(self):
        batch, query, action = self._create_no_result_approval_action()
        payload = {
            "reason": "approved after review",
            "requestId": "no-result-approval-gateway-test-1",
        }

        def approve_once(action_id, operator, reason="", external_request_id=None):
            db = SessionLocal()
            try:
                current = db.get(NoResultGovernanceAction, action_id)
                request_payload = json.loads(current.request_payload)
                request_payload["externalRequestId"] = external_request_id
                current.request_payload = json.dumps(request_payload)
                current.status = "success"
                current.rollback_status = "pending"
                current.error_message = api_module.NoResultGovernanceService._operator_note(
                    "approved", operator, reason
                )
                current_query = db.get(NoResultQuery, current.query_id)
                current_query.status = "awaiting_retag"
                current_batch = db.get(NoResultGovernanceBatch, current.batch_id)
                current_batch.status = "awaiting_retag"
                current_batch.changed_queries = 1
                db.commit()
                return {"actionId": action_id, "status": "success"}
            finally:
                db.close()

        endpoint = f"/api/dashboard/no-result-governance/actions/{action.id}/approve"
        with patch.object(
            api_module.NoResultGovernanceService,
            "approve_manual_action",
            side_effect=approve_once,
        ) as mock_execute:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)
            conflict = self.client.post(
                endpoint,
                json={**payload, "reason": "different approval reason"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(first.json(), {"actionId": action.id, "status": "success"})
        self.assertEqual(conflict.status_code, 409)
        mock_execute.assert_called_once_with(
            action.id,
            operator="test_admin",
            reason=payload["reason"],
            external_request_id=payload["requestId"],
        )

        self.db.refresh(action)
        self.db.refresh(query)
        self.db.refresh(batch)
        self.assertEqual(action.status, "success")
        self.assertEqual(action.rollback_status, "pending")
        self.assertEqual(query.status, "awaiting_retag")
        self.assertEqual(batch.status, "awaiting_retag")
        self.assertEqual(
            json.loads(action.request_payload)["externalRequestId"],
            payload["requestId"],
        )
        intent = self.db.query(ActionIntentFact).one()
        self.assertEqual(intent.action_type, "no_result_action_approve")
        self.assertEqual(intent.idempotency_key, payload["requestId"])
        self.assertEqual(intent.status, "success")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "success")
        readback = self.db.query(ActionReadback).one()
        self.assertEqual(readback.authoritative, 1)
        observed = json.loads(readback.observed_payload)
        self.assertEqual(observed["externalRequestId"], payload["requestId"])
        self.assertEqual(observed["actionStatus"], "success")
        self.assertEqual(observed["queryStatus"], "awaiting_retag")
        self.assertEqual(observed["batchStatus"], "awaiting_retag")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")

    def test_no_result_approval_gateway_fails_closed_without_authoritative_readback(self):
        _batch, _query, action = self._create_no_result_approval_action()
        payload = {
            "reason": "approved after review",
            "requestId": "no-result-approval-readback-missing",
        }
        endpoint = f"/api/dashboard/no-result-governance/actions/{action.id}/approve"
        with patch.object(
            api_module.NoResultGovernanceService,
            "approve_manual_action",
            return_value={"actionId": action.id, "status": "success"},
        ) as mock_execute:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_execute.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionReadback).one().authoritative, 0)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_no_result_approval_gateway_does_not_replay_ambiguous_external_failure(self):
        _batch, _query, action = self._create_no_result_approval_action()
        payload = {
            "reason": "approved after review",
            "requestId": "no-result-approval-timeout",
        }
        endpoint = f"/api/dashboard/no-result-governance/actions/{action.id}/approve"
        with patch.object(
            api_module.NoResultGovernanceService,
            "approve_manual_action",
            side_effect=TimeoutError("external write timed out"),
        ) as mock_execute, TestClient(app, raise_server_exceptions=False) as client:
            first = client.post(endpoint, json=payload)
            replay = client.post(endpoint, json=payload)

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_execute.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def _create_no_result_execution_action(self):
        batch = NoResultGovernanceBatch(
            brand="atlas",
            batch_date=datetime.datetime.now(),
            status="pending_execute",
            total_queries=1,
            changed_queries=0,
        )
        self.db.add(batch)
        self.db.flush()
        query = NoResultQuery(
            batch_id=batch.id,
            brand="atlas",
            query_text="dress",
            data_date=datetime.datetime.now(),
            status="pending_execute",
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            batch_id=batch.id,
            brand="atlas",
            query_text="dress",
            action_type="add_relationship",
            status="pending_execute",
            rollback_status="not_required",
            risk_level="low",
            request_payload=json.dumps({"actionType": "add_relationship"}),
        )
        self.db.add(action)
        self.db.commit()
        return batch, query, action

    def test_no_result_execution_gateway_replays_conflicts_and_records_external_facts(self):
        batch, query, action = self._create_no_result_execution_action()
        payload = {
            "reason": "execute after review",
            "requestId": "no-result-execute-gateway-test-1",
        }

        def execute_once(action_id, operator, reason="", external_request_id=None):
            db = SessionLocal()
            try:
                current = db.get(NoResultGovernanceAction, action_id)
                request_payload = json.loads(current.request_payload)
                request_payload["externalRequestId"] = external_request_id
                current.request_payload = json.dumps(request_payload)
                current.before_snapshot = json.dumps({"relationships": []})
                current.response_payload = json.dumps({"success": True})
                current.rollback_payload = json.dumps({"actionType": "delete_relationship"})
                current.status = "success"
                current.rollback_status = "pending"
                current.error_message = api_module.NoResultGovernanceService._operator_note(
                    "executed", operator, reason
                )
                current_query = db.get(NoResultQuery, current.query_id)
                current_query.status = "awaiting_retag"
                current_batch = db.get(NoResultGovernanceBatch, current.batch_id)
                current_batch.status = "awaiting_retag"
                current_batch.changed_queries = 1
                db.commit()
                return {"actionId": action_id, "status": "success"}
            finally:
                db.close()

        endpoint = f"/api/dashboard/no-result-governance/actions/{action.id}/execute"
        with patch.object(
            api_module.NoResultGovernanceService,
            "execute_pending_action",
            side_effect=execute_once,
        ) as mock_execute:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)
            conflict = self.client.post(
                endpoint,
                json={**payload, "reason": "different execution reason"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(first.json(), {"actionId": action.id, "status": "success"})
        self.assertEqual(conflict.status_code, 409)
        mock_execute.assert_called_once_with(
            action.id,
            operator="test_admin",
            reason=payload["reason"],
            external_request_id=payload["requestId"],
        )

        self.db.refresh(action)
        self.db.refresh(query)
        self.db.refresh(batch)
        self.assertEqual(action.status, "success")
        self.assertEqual(action.rollback_status, "pending")
        self.assertEqual(query.status, "awaiting_retag")
        self.assertEqual(batch.status, "awaiting_retag")
        self.assertEqual(
            json.loads(action.request_payload)["externalRequestId"],
            payload["requestId"],
        )
        intent = self.db.query(ActionIntentFact).one()
        self.assertEqual(intent.action_type, "no_result_action_execute")
        self.assertEqual(intent.idempotency_key, payload["requestId"])
        self.assertEqual(intent.status, "success")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "success")
        readback = self.db.query(ActionReadback).one()
        self.assertEqual(readback.authoritative, 1)
        observed = json.loads(readback.observed_payload)
        self.assertEqual(observed["externalRequestId"], payload["requestId"])
        self.assertEqual(observed["actionStatus"], "success")
        self.assertEqual(observed["rollbackStatus"], "pending")
        self.assertEqual(observed["queryStatus"], "awaiting_retag")
        self.assertEqual(observed["batchStatus"], "awaiting_retag")
        self.assertEqual(observed["responseSucceeded"], True)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")

    def test_no_result_execution_gateway_fails_closed_without_authoritative_readback(self):
        _batch, _query, action = self._create_no_result_execution_action()
        payload = {
            "reason": "execute after review",
            "requestId": "no-result-execute-readback-missing",
        }
        endpoint = f"/api/dashboard/no-result-governance/actions/{action.id}/execute"
        with patch.object(
            api_module.NoResultGovernanceService,
            "execute_pending_action",
            return_value={"actionId": action.id, "status": "success"},
        ) as mock_execute:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_execute.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionReadback).one().authoritative, 0)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_no_result_execution_gateway_does_not_replay_ambiguous_external_failure(self):
        _batch, _query, action = self._create_no_result_execution_action()
        payload = {
            "reason": "execute after review",
            "requestId": "no-result-execute-timeout",
        }
        endpoint = f"/api/dashboard/no-result-governance/actions/{action.id}/execute"
        with patch.object(
            api_module.NoResultGovernanceService,
            "execute_pending_action",
            side_effect=TimeoutError("external write timed out"),
        ) as mock_execute, TestClient(app, raise_server_exceptions=False) as client:
            first = client.post(endpoint, json=payload)
            replay = client.post(endpoint, json=payload)

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_execute.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_no_result_execution_gateway_preserves_explicit_no_write_response_and_failed_fact(self):
        _batch, _query, action = self._create_no_result_execution_action()
        payload = {
            "reason": "execute after review",
            "requestId": "no-result-execute-not-authorized",
        }
        endpoint = f"/api/dashboard/no-result-governance/actions/{action.id}/execute"
        with patch.object(
            api_module.NoResultGovernanceService,
            "execute_pending_action",
            return_value={
                "actionId": action.id,
                "status": "pending_execute",
                "reason": "real_write_not_authorized",
            },
        ) as mock_execute:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.json(), {
            "actionId": action.id,
            "status": "pending_execute",
            "reason": "real_write_not_authorized",
        })
        self.assertEqual(replay.json(), first.json())
        mock_execute.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "failed")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "failed")
        self.assertEqual(self.db.query(ActionReadback).count(), 0)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "failed")

    def test_no_result_execution_pause_gateway_replays_and_allows_new_pause_after_resume(self):
        query = NoResultQuery(
            brand="atlas",
            query_text="dress",
            data_date=datetime.datetime.now(),
            status="execution_running",
        )
        self.db.add(query)
        self.db.commit()

        endpoint = f"/api/dashboard/no-result-governance/queries/{query.id}/execution/pause"
        first_payload = {
            "reason": "hold for review",
            "requestId": "no-result-pause-gateway-1",
        }
        first = self.client.post(endpoint, json=first_payload)
        replay = self.client.post(endpoint, json=first_payload)
        conflict = self.client.post(
            endpoint,
            json={**first_payload, "reason": "different reason"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(first.json()["status"], "paused")
        self.db.refresh(query)
        self.assertEqual(query.status, "execution_paused")
        self.assertEqual(query.ai_reason.count("execution_paused by test_admin"), 1)
        intent = self.db.query(ActionIntentFact).one()
        self.assertEqual(intent.action_type, "no_result_query_pause")
        self.assertEqual(intent.idempotency_key, first_payload["requestId"])
        self.assertEqual(intent.status, "success")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "success")
        self.assertEqual(self.db.query(ActionReadback).one().authoritative, 1)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")

        resumed = self.client.post(
            f"/api/dashboard/no-result-governance/queries/{query.id}/execution/resume",
            json={"reason": "continue"},
        )
        self.assertEqual(resumed.status_code, 200)
        second = self.client.post(
            endpoint,
            json={
                "reason": "hold after resume",
                "requestId": "no-result-pause-gateway-2",
            },
        )

        self.assertEqual(second.status_code, 200)
        self.db.refresh(query)
        self.assertEqual(query.status, "execution_paused")
        self.assertEqual(query.ai_reason.count("execution_paused by test_admin"), 2)
        intents = self.db.query(ActionIntentFact).order_by(ActionIntentFact.id).all()
        self.assertEqual(
            [intent.action_type for intent in intents],
            [
                "no_result_query_pause",
                "no_result_query_resume",
                "no_result_query_pause",
            ],
        )
        self.assertEqual(self.db.query(ActionTerminalFact).count(), 3)

    def test_no_result_execution_pause_gateway_denies_unauthorized_and_maps_state_conflict(self):
        query = NoResultQuery(
            brand="atlas",
            query_text="dress",
            data_date=datetime.datetime.now(),
            status="manual_required",
        )
        self.db.add(query)
        self.db.commit()
        endpoint = f"/api/dashboard/no-result-governance/queries/{query.id}/execution/pause"

        conflict = self.client.post(
            endpoint,
            json={"requestId": "no-result-pause-state-conflict"},
        )

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"], "No Result query is not running")
        self.db.refresh(query)
        self.assertEqual(query.status, "manual_required")
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "failed")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "failed")

        restricted_user = User(
            id=99,
            username="restricted",
            is_active=1,
            is_admin=0,
            role="brand_member",
            page_permissions='["/no-result-governance"]',
            brand_permissions='["atlas"]',
            action_permissions="[]",
        )
        previous_override = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: restricted_user
        try:
            denied = self.client.post(
                endpoint,
                json={"requestId": "no-result-pause-permission-denied"},
            )
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.db.query(ActionIntentFact).count(), 1)

    def test_no_result_execution_start_and_resume_gateways_replay_and_bind_scope(self):
        start_query = NoResultQuery(
            brand="atlas",
            query_text="start dress",
            data_date=datetime.datetime.now(),
            status="pending_execute",
        )
        resume_query = NoResultQuery(
            brand="atlas",
            query_text="resume dress",
            data_date=datetime.datetime.now(),
            status="execution_paused",
        )
        self.db.add_all([start_query, resume_query])
        self.db.commit()

        cases = (
            ("start", start_query, "no-result-start-gateway-1"),
            ("resume", resume_query, "no-result-resume-gateway-1"),
        )
        for operation, query, request_id in cases:
            with self.subTest(operation=operation):
                endpoint = (
                    f"/api/dashboard/no-result-governance/queries/{query.id}"
                    f"/execution/{operation}"
                )
                payload = {
                    "reason": f"{operation} after review",
                    "requestId": request_id,
                }
                first = self.client.post(endpoint, json=payload)
                replay = self.client.post(endpoint, json=payload)
                conflict = self.client.post(
                    endpoint,
                    json={**payload, "reason": "different reason"},
                )

                self.assertEqual(first.status_code, 200)
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json(), first.json())
                self.assertEqual(conflict.status_code, 409)
                self.assertEqual(first.json()["queryId"], query.id)
                self.db.refresh(query)
                self.assertEqual(query.status, "execution_running")
                self.assertEqual(
                    query.ai_reason.count("execution_started by test_admin"), 1
                )

        intents = self.db.query(ActionIntentFact).order_by(ActionIntentFact.id).all()
        self.assertEqual(
            [intent.action_type for intent in intents],
            ["no_result_query_start", "no_result_query_resume"],
        )
        self.assertEqual(
            [intent.idempotency_key for intent in intents],
            ["no-result-start-gateway-1", "no-result-resume-gateway-1"],
        )
        self.assertTrue(all(intent.operator == "test_admin" for intent in intents))
        self.assertTrue(all(intent.brand == "atlas" for intent in intents))
        self.assertTrue(
            all(intent.permission == "manage_no_result_execution" for intent in intents)
        )
        self.assertTrue(all(intent.release for intent in intents))
        self.assertEqual(
            [terminal.status for terminal in self.db.query(ActionTerminalFact).all()],
            ["success", "success"],
        )
        self.assertEqual(
            self.db.query(ActionReadback).filter(ActionReadback.authoritative == 1).count(),
            2,
        )

    def test_no_result_execution_start_gateway_maps_missing_and_denies_unauthorized(self):
        missing = self.client.post(
            "/api/dashboard/no-result-governance/queries/999999/execution/start",
            json={"requestId": "no-result-start-missing"},
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.db.query(ActionIntentFact).count(), 0)

        query = NoResultQuery(
            brand="atlas",
            query_text="restricted dress",
            data_date=datetime.datetime.now(),
            status="pending_execute",
        )
        self.db.add(query)
        self.db.commit()
        restricted_user = User(
            id=99,
            username="restricted",
            is_active=1,
            is_admin=0,
            role="brand_member",
            page_permissions='["/no-result-governance"]',
            brand_permissions='["atlas"]',
            action_permissions="[]",
        )
        previous_override = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: restricted_user
        try:
            denied = self.client.post(
                f"/api/dashboard/no-result-governance/queries/{query.id}/execution/start",
                json={"requestId": "no-result-start-permission-denied"},
            )
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(denied.status_code, 403)
        self.db.refresh(query)
        self.assertEqual(query.status, "pending_execute")
        self.assertEqual(self.db.query(ActionIntentFact).count(), 0)

    def test_no_result_execution_start_gateway_binds_pending_action_to_gateway_request(self):
        query = NoResultQuery(
            brand="atlas",
            query_text="gateway trace dress",
            data_date=datetime.datetime.now(),
            status="pending_execute",
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            brand="atlas",
            query_text="gateway trace dress",
            action_type="add_tag",
            status="pending_execute",
            rollback_status="pending",
            request_payload=json.dumps(
                {"actionType": "add_tag", "target": {}, "params": {}}
            ),
        )
        self.db.add(action)
        self.db.commit()

        request_id = "no-result-start-pending-action-gateway-1"
        with patch.dict(os.environ, {"NO_RESULT_KG_REAL_WRITE": "true"}), patch.object(
            api_module.NoResultGovernanceService,
            "_build_default_kg_client",
            return_value=StubNoResultKGClient(),
        ), patch.object(
            api_module.NoResultGovernanceService,
            "_execute_kg_write",
            return_value={"code": 0, "message": "success"},
        ) as execute_write:
            response = self.client.post(
                f"/api/dashboard/no-result-governance/queries/{query.id}/execution/start",
                json={"reason": "run approved action", "requestId": request_id},
            )

        self.assertEqual(response.status_code, 200)
        execute_write.assert_called_once()
        self.assertEqual(
            execute_write.call_args.kwargs["external_request_id"], request_id
        )
        self.db.refresh(action)
        self.db.refresh(query)
        self.assertEqual(action.status, "success")
        self.assertEqual(
            json.loads(action.request_payload)["externalRequestId"], request_id
        )
        self.assertIn(f"gatewayRequestId={request_id}", query.ai_reason)
        self.assertEqual(self.db.query(ActionReadback).one().status, "success")

    def test_no_result_execution_start_gateway_does_not_accept_historical_note(self):
        reason = "already recorded"
        query = NoResultQuery(
            brand="atlas",
            query_text="historical note dress",
            data_date=datetime.datetime.now(),
            status="execution_running",
            ai_reason=api_module.NoResultGovernanceService._operator_note(
                "execution_started", "test_admin", reason
            ),
        )
        self.db.add(query)
        self.db.commit()

        response = self.client.post(
            f"/api/dashboard/no-result-governance/queries/{query.id}/execution/start",
            json={
                "reason": reason,
                "requestId": "no-result-start-historical-note-gateway-1",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.db.query(ActionTerminalFact).one().status,
            "failed",
        )

    def test_no_result_execution_gateway_rechecks_brand_at_execution_point(self):
        query = NoResultQuery(
            brand="atlas",
            query_text="brand race dress",
            data_date=datetime.datetime.now(),
            status="pending_execute",
        )
        self.db.add(query)
        self.db.commit()
        revoked_user = User(
            id=901,
            username="revoked_brand_admin",
            is_active=1,
            is_admin=1,
            role="platform_admin",
            page_permissions='["/no-result-governance"]',
            brand_permissions='["harbor"]',
            action_permissions="[]",
        )
        previous_override = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: revoked_user
        try:
            with patch.object(api_module, "_ensure_no_result_query_permission"):
                response = self.client.post(
                    f"/api/dashboard/no-result-governance/queries/{query.id}/execution/start",
                    json={"requestId": "no-result-start-brand-race-gateway-1"},
                )
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 403)
        self.db.refresh(query)
        self.assertEqual(query.status, "pending_execute")
        self.assertEqual(self.db.query(ActionIntentFact).count(), 0)

    def test_no_result_execution_gateway_fails_closed_for_deleted_user(self):
        query = NoResultQuery(
            brand="atlas",
            query_text="deleted user dress",
            data_date=datetime.datetime.now(),
            status="pending_execute",
        )
        self.db.add(query)
        self.db.commit()
        deleted_user = User(
            id=902,
            username="deleted_brand_admin",
            is_active=1,
            is_admin=1,
            role="platform_admin",
            page_permissions='["/no-result-governance"]',
            brand_permissions='["atlas"]',
            action_permissions="[]",
        )
        previous_override = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: deleted_user
        try:
            response = self.client.post(
                f"/api/dashboard/no-result-governance/queries/{query.id}/execution/start",
                json={"requestId": "no-result-start-deleted-user-gateway-1"},
            )
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 403)
        self.db.refresh(query)
        self.assertEqual(query.status, "pending_execute")
        self.assertEqual(self.db.query(ActionIntentFact).count(), 0)

    def test_no_result_execution_gateway_rechecks_live_user_authorization(self):
        cases = (
            ("inactive", {"is_active": 0}),
            (
                "role_revoked",
                {
                    "is_admin": 0,
                    "role": "brand_member",
                    "action_permissions": '["manage_brand_config"]',
                },
            ),
            (
                "action_revoked",
                {
                    "is_admin": 0,
                    "role": "brand_admin",
                    "action_permissions": '["manage_brand_config"]',
                },
            ),
            (
                # 平台管理员按全局权限处理（PRD 账号与品牌权限隔离 §2.1/§8），
                # 品牌收窄须用非平台管理员验证，且动作权限保留以确保拒绝来自品牌校验。
                "brand_revoked",
                {
                    "is_admin": 0,
                    "role": "brand_admin",
                    "action_permissions": '["manage_no_result_execution"]',
                    "brand_permissions": '["harbor"]',
                },
            ),
        )
        gateway_factory = api_module.ActionGateway

        for case_name, changes in cases:
            with self.subTest(case=case_name):
                query = NoResultQuery(
                    brand="atlas",
                    query_text=f"{case_name} live user dress",
                    data_date=datetime.datetime.now(),
                    status="pending_execute",
                )
                self.db.add(query)
                self.db.commit()

                def mutate_user_then_create_gateway(*args, **kwargs):
                    db = SessionLocal()
                    try:
                        current_user = db.get(User, 1)
                        for field, value in changes.items():
                            setattr(current_user, field, value)
                        db.commit()
                    finally:
                        db.close()
                    return gateway_factory(*args, **kwargs)

                try:
                    with patch.object(
                        api_module,
                        "ActionGateway",
                        side_effect=mutate_user_then_create_gateway,
                    ):
                        response = self.client.post(
                            f"/api/dashboard/no-result-governance/queries/{query.id}/execution/start",
                            json={"requestId": f"no-result-live-user-{case_name}-1"},
                        )

                    self.assertEqual(response.status_code, 403)
                    self.db.refresh(query)
                    self.assertEqual(query.status, "pending_execute")
                    self.assertEqual(self.db.query(ActionIntentFact).count(), 0)
                finally:
                    current_user = self.db.get(User, 1)
                    current_user.is_active = 1
                    current_user.is_admin = 1
                    current_user.role = "platform_admin"
                    current_user.action_permissions = "[]"
                    current_user.brand_permissions = json.dumps(
                        ["atlas", "summit", "harbor", "meadow", "riviera"]
                    )
                    self.db.commit()

    def test_no_result_execution_gateway_fails_closed_when_kg_write_is_unknown(self):
        query = NoResultQuery(
            brand="atlas",
            query_text="unknown kg write dress",
            data_date=datetime.datetime.now(),
            status="pending_execute",
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            brand="atlas",
            query_text="unknown kg write dress",
            action_type="add_tag",
            status="pending_execute",
            rollback_status="pending",
            request_payload=json.dumps(
                {"actionType": "add_tag", "target": {}, "params": {}}
            ),
        )
        self.db.add(action)
        self.db.commit()

        with patch.dict(os.environ, {"NO_RESULT_KG_REAL_WRITE": "true"}), patch.object(
            api_module.NoResultGovernanceService,
            "_build_default_kg_client",
            return_value=StubNoResultKGClient(),
        ), patch.object(
            api_module.NoResultGovernanceService,
            "_execute_kg_write",
            side_effect=TimeoutError("knowledge graph timeout"),
        ):
            response = self.client.post(
                f"/api/dashboard/no-result-governance/queries/{query.id}/execution/start",
                json={"requestId": "no-result-start-unknown-kg-write-1"},
            )

        self.assertEqual(response.status_code, 409)
        self.db.refresh(action)
        self.db.refresh(query)
        self.assertEqual(action.status, "execution_unknown")
        self.assertEqual(query.status, "execution_unknown")
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "failed")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "failed")

    def test_no_result_execution_stale_fence_moves_unclaimed_action_to_manual_review(self):
        request_id = "no-result-start-stale-fence-1"
        query = NoResultQuery(
            brand="atlas",
            query_text="stale fence dress",
            data_date=datetime.datetime.now(),
            status="execution_running",
            ai_reason=api_module.NoResultGovernanceService._operator_note(
                "execution_started",
                "test_admin",
                "",
                f"gatewayRequestId={request_id}",
            ),
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            brand="atlas",
            query_text="stale fence dress",
            action_type="add_tag",
            status="pending_execute",
            rollback_status="pending",
            request_payload=json.dumps(
                {"actionType": "add_tag", "target": {}, "params": {}}
            ),
        )
        self.db.add(action)
        self.db.commit()

        fenced = api_module._fence_no_result_query_execution(
            query_id=query.id,
            brand="atlas",
            query_text="stale fence dress",
            operator="test_admin",
            reason="",
            external_request_id=request_id,
        )

        self.assertEqual(fenced["status"], "fenced")
        self.db.refresh(action)
        self.db.refresh(query)
        self.assertEqual(action.status, "manual_review")
        self.assertEqual(query.status, "manual_required")

    def test_dashboard_cancel_blacklist_gateway_replays_terminal_without_duplicate_write(self):
        payload = {
            "brand": "atlas",
            "query": "dress",
            "sku": "SKU-gateway-1",
            "requestId": "dashboard-cancel-blacklist-test-1",
        }
        with patch.object(
            api_module.service,
            "remove_from_blacklist",
            return_value=True,
        ) as mock_execute:
            first = self.client.post("/api/dashboard/actions/cancel-blacklist", json=payload)
            replay = self.client.post("/api/dashboard/actions/cancel-blacklist", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        mock_execute.assert_called_once_with(
            "atlas",
            "SKU-gateway-1",
            ["dress"],
            external_request_id="dashboard-cancel-blacklist-test-1",
        )
        intent = self.db.query(ActionIntentFact).one()
        self.assertEqual(intent.idempotency_key, "dashboard-cancel-blacklist-test-1")
        self.assertEqual(intent.status, "success")
        attempt = self.db.query(ActionAttempt).one()
        self.assertEqual(attempt.external_request_id, "dashboard-cancel-blacklist-test-1")
        self.assertEqual(attempt.status, "success")
        # 写接口业务成功即权威成功，不再产生 product/list 回读事实。
        self.assertEqual(self.db.query(ActionReadback).count(), 0)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")
        self.assertEqual(self.db.query(ActionLog).count(), 1)

    def test_dashboard_cancel_blacklist_executor_failure_is_terminal_and_does_not_log_success(self):
        with patch.object(
            api_module.service,
            "remove_from_blacklist",
            return_value=False,
        ) as mock_execute:
            first = self.client.post(
                "/api/dashboard/actions/cancel-blacklist",
                json={
                    "brand": "atlas",
                    "query": "dress",
                    "sku": "SKU-gateway-2",
                    "requestId": "dashboard-cancel-blacklist-test-2",
                },
            )
            replay = self.client.post(
                "/api/dashboard/actions/cancel-blacklist",
                json={
                    "brand": "atlas",
                    "query": "dress",
                    "sku": "SKU-gateway-2",
                    "requestId": "dashboard-cancel-blacklist-test-2",
                },
            )

        self.assertEqual(first.status_code, 500)
        self.assertEqual(replay.status_code, 500)
        mock_execute.assert_called_once()
        self.assertEqual(self.db.query(ActionLog).count(), 0)
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "failed")
        self.assertEqual(self.db.query(ActionReadback).count(), 0)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "failed")

    def test_dashboard_cancel_blacklist_projection_failure_rolls_back_local_success(self):
        payload = {
            "brand": "atlas",
            "query": "dress",
            "sku": "SKU-gateway-3",
            "requestId": "dashboard-cancel-blacklist-test-3",
        }
        with patch.object(
            api_module.service,
            "remove_from_blacklist",
            return_value=True,
        ) as mock_execute, patch.object(
            api_module,
            "ActionLog",
            side_effect=RuntimeError("projection unavailable"),
        ) as mock_projection:
            with TestClient(app, raise_server_exceptions=False) as client:
                first = client.post("/api/dashboard/actions/cancel-blacklist", json=payload)
                replay = client.post("/api/dashboard/actions/cancel-blacklist", json=payload)

        self.assertEqual(first.status_code, 500)
        self.assertEqual(replay.status_code, 500)
        mock_execute.assert_called_once()
        mock_projection.assert_called_once()
        self.assertEqual(self.db.query(ActionLog).count(), 0)
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "failed")
        self.assertEqual(self.db.query(ActionReadback).count(), 0)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "failed")

    def test_dashboard_cancel_blacklist_rejects_summit_without_authoritative_readback(self):
        with patch.object(
            api_module.service,
            "remove_from_blacklist",
            return_value=True,
        ) as mock_execute:
            response = self.client.post(
                "/api/dashboard/actions/cancel-blacklist",
                json={
                    "brand": "summit",
                    "query": "dress",
                    "sku": "SKU-gateway-summit",
                    "requestId": "dashboard-cancel-blacklist-summit",
                },
            )

        self.assertEqual(response.status_code, 409)
        mock_execute.assert_not_called()
        self.assertEqual(self.db.query(ActionIntentFact).count(), 0)

    def test_assistant_chat_uses_published_node_model_bundle_over_binding_model(self):
        brand_space = AssistantBrandSpace(brand="atlas", display_name="ATLAS", enabled=1)
        self.db.add(brand_space)
        self.db.flush()
        self.db.add(AssistantAgentBinding(
            brand_space_id=brand_space.id,
            display_name="ATLAS Agent",
            model_display_name="Legacy Binding",
            model_name="legacy-binding-model",
            available_models_json='["legacy-binding-model"]',
            supports_model_switch=0,
            binding_type="agent",
            enabled=1,
        ))
        entries = {
            node_key: {
                "channel": spec.channel,
                "primaryModel": (
                    spec.default_primary
                    if spec.required_capabilities
                    else f"{node_key}-primary"
                ),
                "fallbackModel": (
                    spec.default_fallback
                    if spec.required_capabilities
                    else (None if spec.default_fallback is None else f"{node_key}-fallback")
                ),
            }
            for node_key, spec in NODE_REGISTRY.items()
        }
        entries["assistant.chat"] = {
            "channel": "pi_agent",
            "primaryModel": "assistant-primary",
            "fallbackModel": "assistant-fallback",
        }
        draft = create_draft(self.db, entries)
        publish_draft(self.db, draft.version, expected_version=None)
        captured = {}

        class FakeAssistantService:
            def chat(self, **kwargs):
                return {
                    "sessionId": kwargs["session_id"],
                    "message": "configured",
                    "proposedActions": [],
                    "contextSummary": {},
                    "status": "ok",
                }

        def build_service(**kwargs):
            captured.update(kwargs)
            return FakeAssistantService()

        with patch("src.api.app.PiAgentAssistantService", side_effect=build_service):
            response = self.client.post(
                "/api/assistant/chat",
                json={"message": "分析", "sessionId": "bundle-session", "brand": "atlas"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["model_name"], "assistant-primary")
        self.assertNotIn("fallback_model_name", captured)
        self.assertEqual(captured["available_models"], ["assistant-primary", "assistant-fallback"])
        self.assertTrue(captured["supports_model_switch"])

    def test_cors_allows_remote_master_frontend_port_5176(self):
        response = self.client.options(
            "/api/auth/login",
            headers={
                "Origin": "http://127.0.0.1:5176",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://127.0.0.1:5176")

    def test_cors_allows_arbitrary_local_test_frontend_port(self):
        response = self.client.options(
            "/api/auth/login",
            headers={
                "Origin": "http://127.0.0.1:58431",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://127.0.0.1:58431")

    def test_assistant_chat_injects_current_user_brand_context(self):
        atlas_query = DailyAbnormalQuery(
            brand="atlas",
            query_text="衬衫 女士",
            anomaly_type="low_cvr",
            reason_analysis="高价商品干扰转化",
            action_suggestions="建议复核高价商品",
            data_date=datetime.date.today(),
            created_at=datetime.datetime.now(),
            updated_at=datetime.datetime.now(),
        )
        harbor_query = DailyAbnormalQuery(
            brand="harbor",
            query_text="裤子",
            anomaly_type="low_ctr",
            reason_analysis="HARBOR 不应进入 atlas 权限上下文",
            data_date=datetime.date.today(),
            created_at=datetime.datetime.now(),
            updated_at=datetime.datetime.now(),
        )
        self.db.add_all([atlas_query, harbor_query])
        self.db.commit()

        captured = {}

        class FakeAssistantService:
            def chat(self, **kwargs):
                captured.update(kwargs)
                return {
                    "sessionId": kwargs["session_id"],
                    "message": "建议先复核高价商品。",
                    "proposedActions": [{"actionType": "demote", "brand": "atlas", "query": "衬衫 女士"}],
                    "contextSummary": {"taskCount": 1},
                    "status": "ok",
                }

        async def override_atlas_only():
            return User(
                id=8,
                username="atlas_operator",
                is_admin=0,
                page_permissions='["/smart-assistant"]',
                brand_permissions='["atlas"]',
            )

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_atlas_only
        try:
            with patch("src.api.app.PiAgentAssistantService", return_value=FakeAssistantService()):
                response = self.client.post("/api/assistant/chat", json={"message": "分析", "sessionId": "s2"})
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["proposedActions"][0]["actionType"], "demote")
        self.assertEqual(captured["allowed_brands"], ["atlas"])
        self.assertEqual(captured["user_id"], 8)
        self.assertEqual(captured["username"], "atlas_operator")
        context_json = json.dumps(captured["context"], ensure_ascii=False)
        self.assertIn("衬衫 女士", context_json)
        self.assertNotIn("HARBOR 不应进入 atlas 权限上下文", context_json)

    def test_assistant_chat_uses_account_scope_without_client_brand(self):
        atlas_query = DailyAbnormalQuery(
            brand="atlas",
            query_text="衬衫 女士",
            anomaly_type="low_cvr",
            reason_analysis="Atlas 原因",
            data_date=datetime.date.today(),
            created_at=datetime.datetime.now(),
            updated_at=datetime.datetime.now(),
        )
        harbor_query = DailyAbnormalQuery(
            brand="harbor",
            query_text="裤子",
            anomaly_type="low_ctr",
            reason_analysis="HARBOR 原因",
            data_date=datetime.date.today(),
            created_at=datetime.datetime.now(),
            updated_at=datetime.datetime.now(),
        )
        self.db.add_all([atlas_query, harbor_query])
        self.db.commit()
        captured = {}

        class FakeAssistantService:
            def chat(self, **kwargs):
                captured.update(kwargs)
                return {
                    "sessionId": kwargs["session_id"],
                    "message": "已按账号权限汇总。",
                    "proposedActions": [],
                    "contextSummary": {"taskCount": 2},
                    "status": "ok",
                }

        account_user = User(
            id=18,
            username="multi_brand_operator",
            is_admin=0,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas", "harbor"]',
        )

        async def override_account_user():
            return account_user

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_account_user
        try:
            with patch("src.api.app.PiAgentAssistantService", return_value=FakeAssistantService()):
                response = self.client.post(
                    "/api/assistant/chat",
                    json={"message": "汇总昨天运营情况", "sessionId": "account-scope-session"},
                )
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["allowed_brands"], ["atlas", "harbor"])
        self.assertIsNone(captured["selected_brand"])
        context_json = json.dumps(captured["context"], ensure_ascii=False)
        self.assertIn("Atlas 原因", context_json)
        self.assertIn("HARBOR 原因", context_json)

    def test_assistant_scope_inherits_all_account_brands_even_with_legacy_user_binding(self):
        operator = User(
            id=181,
            username="legacy_binding_operator",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas", "meadow"]',
        )
        atlas_space = AssistantBrandSpace(brand="atlas", display_name="ATLAS", enabled=1)
        meadow_space = AssistantBrandSpace(brand="meadow", display_name="Meadow", enabled=1)
        self.db.add_all([operator, atlas_space, meadow_space])
        self.db.flush()
        atlas_binding = AssistantAgentBinding(
            brand_space_id=atlas_space.id,
            display_name="ATLAS Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        meadow_binding = AssistantAgentBinding(
            brand_space_id=meadow_space.id,
            display_name="MEADOW Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        self.db.add_all([atlas_binding, meadow_binding])
        self.db.flush()
        self.db.add(AssistantUserBinding(
            user_id=operator.id,
            brand_space_id=atlas_space.id,
            agent_binding_id=atlas_binding.id,
        ))
        self.db.commit()

        scope = assistant_module._resolve_assistant_scope(self.db, operator)

        self.assertEqual(scope["brands"], ["atlas", "meadow"])
        self.assertEqual(
            {binding.brand_space.brand for binding in scope["bindings"]},
            {"atlas", "meadow"},
        )

    def test_assistant_session_access_accepts_current_brand_without_legacy_user_binding(self):
        operator = User(
            id=182,
            username="inherited_scope_operator",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
        )
        atlas_space = AssistantBrandSpace(brand="atlas", display_name="ATLAS", enabled=1)
        self.db.add_all([operator, atlas_space])
        self.db.flush()
        atlas_binding = AssistantAgentBinding(
            brand_space_id=atlas_space.id,
            display_name="ATLAS Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        self.db.add(atlas_binding)
        self.db.flush()
        session = AssistantSessionMeta(
            session_id="inherited-scope-session",
            user_id=operator.id,
            brand="atlas",
            agent_binding_id=atlas_binding.id,
            message_log="[]",
        )
        self.db.add(session)
        self.db.commit()

        record = assistant_module._ensure_assistant_session_access(
            self.db,
            session_id=session.session_id,
            user=operator,
            expected_brand="atlas",
        )

        self.assertEqual(record.session_id, session.session_id)

    def test_assistant_sessions_hide_brands_removed_from_account_permissions(self):
        operator = User(
            id=183,
            username="revoked_brand_operator",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions="[]",
        )
        self.db.add(operator)
        self.db.flush()
        self.db.add(AssistantSessionMeta(
            session_id="revoked-brand-session",
            user_id=operator.id,
            brand="atlas",
            message_log="[]",
        ))
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            response = self.client.get("/api/assistant/sessions")
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])

    def test_assistant_chat_merges_page_context_into_service_context(self):
        task = TopQueryTask(
            brand="atlas",
            query_text="四折精选 女士",
            data_date=datetime.date.today(),
            query_source="top_query",
        )
        self.db.add(task)
        self.db.commit()
        captured = {}

        class FakeAssistantService:
            def chat(self, **kwargs):
                captured.update(kwargs)
                return {
                    "sessionId": kwargs["session_id"],
                    "message": "已读取页面上下文。",
                    "proposedActions": [],
                    "contextSummary": {"taskCount": 0},
                    "status": "ok",
                }

        with patch("src.api.app.PiAgentAssistantService", return_value=FakeAssistantService()):
            response = self.client.post("/api/assistant/chat", json={
                "message": "分析这个 Top 词",
                "sessionId": "page-context-session",
                "brand": "atlas",
                "pageContext": {
                    "pageKey": "brand_detail",
                    "selectedQuery": "四折精选 女士",
                    "selectedTaskId": task.id,
                },
            })

        self.assertEqual(response.status_code, 200)
        context_json = json.dumps(captured["context"], ensure_ascii=False)
        self.assertIn("brand_detail", context_json)
        self.assertIn("四折精选 女士", context_json)
        self.assertIn(str(task.id), context_json)

    def test_assistant_chat_does_not_load_cross_brand_selected_task_context(self):
        harbor_task = TopQueryTask(
            brand="harbor",
            query_text="HARBOR confidential query",
            data_date=datetime.date.today(),
            query_source="top_query",
        )
        self.db.add(harbor_task)
        self.db.commit()

        captured = {}

        class FakeAssistantService:
            def chat(self, **kwargs):
                captured.update(kwargs)
                return {
                    "sessionId": kwargs["session_id"],
                    "message": "已读取授权上下文。",
                    "proposedActions": [],
                    "contextSummary": {},
                    "status": "ok",
                }

        async def override_atlas_only():
            return User(
                id=81,
                username="atlas_context_operator",
                is_admin=0,
                page_permissions='["/smart-assistant"]',
                brand_permissions='["atlas"]',
            )

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_atlas_only
        try:
            with patch("src.api.app.PiAgentAssistantService", return_value=FakeAssistantService()):
                response = self.client.post("/api/assistant/chat", json={
                    "message": "分析当前任务",
                    "sessionId": "cross-brand-page-context",
                    "brand": "atlas",
                    "pageContext": {
                        "pageKey": "brand_detail",
                        "selectedTaskId": harbor_task.id,
                    },
                })
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 200)
        context_json = json.dumps(captured["context"], ensure_ascii=False)
        self.assertNotIn("HARBOR confidential query", context_json)

    def test_assistant_sessions_are_scoped_to_owner_and_account_brands(self):
        operator = User(
            id=21,
            username="atlas_operator",
            password_hash="hash",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas", "meadow"]',
        )
        other_user = User(
            id=22,
            username="other_operator",
            password_hash="hash",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
        )
        atlas_space = AssistantBrandSpace(brand="atlas", display_name="ATLAS")
        meadow_space = AssistantBrandSpace(brand="meadow", display_name="Meadow")
        self.db.add_all([operator, other_user, atlas_space, meadow_space])
        self.db.flush()

        atlas_binding = AssistantAgentBinding(
            brand_space_id=atlas_space.id,
            display_name="ATLAS Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        meadow_binding = AssistantAgentBinding(
            brand_space_id=meadow_space.id,
            display_name="MEADOW Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        self.db.add_all([atlas_binding, meadow_binding])
        self.db.flush()
        self.db.add(AssistantUserBinding(user_id=operator.id, brand_space_id=atlas_space.id, agent_binding_id=atlas_binding.id))
        self.db.flush()
        self.db.add_all([
            AssistantSessionMeta(
                session_id="sess-atlas-1",
                user_id=operator.id,
                brand="atlas",
                agent_binding_id=atlas_binding.id,
                generated_title="分析衬衫女士",
                last_message_preview="先复核高价商品",
                message_log=json.dumps([
                    {"id": "m1", "role": "operator", "content": "分析衬衫 女士"},
                    {"id": "m2", "role": "assistant", "content": "先复核高价商品"},
                ], ensure_ascii=False),
                pinned=1,
                last_opened_at=datetime.datetime(2026, 7, 3, 12, 0, 0),
            ),
            AssistantSessionMeta(
                session_id="sess-meadow-1",
                user_id=operator.id,
                brand="meadow",
                agent_binding_id=meadow_binding.id,
                generated_title="不应暴露的Meadow会话",
                last_message_preview="hidden",
                message_log="[]",
            ),
            AssistantSessionMeta(
                session_id="sess-atlas-2",
                user_id=other_user.id,
                brand="atlas",
                agent_binding_id=atlas_binding.id,
                generated_title="别人的会话",
                last_message_preview="hidden",
                message_log="[]",
            ),
        ])
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            response = self.client.get("/api/assistant/sessions")
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual({item["sessionId"] for item in data["items"]}, {"sess-atlas-1", "sess-meadow-1"})
        atlas_session = next(item for item in data["items"] if item["sessionId"] == "sess-atlas-1")
        self.assertEqual(atlas_session["title"], "分析衬衫女士")
        self.assertTrue(atlas_session["pinned"])

    def test_assistant_sessions_repair_mojibake_messages_on_read(self):
        operator = User(
            id=121,
            username="repair_operator",
            password_hash="hash",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
        )
        atlas_space = AssistantBrandSpace(brand="atlas", display_name="ATLAS")
        self.db.add_all([operator, atlas_space])
        self.db.flush()
        atlas_binding = AssistantAgentBinding(
            brand_space_id=atlas_space.id,
            display_name="ATLAS Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        self.db.add(atlas_binding)
        self.db.flush()
        self.db.add(AssistantUserBinding(user_id=operator.id, brand_space_id=atlas_space.id, agent_binding_id=atlas_binding.id))
        self.db.add(AssistantSessionMeta(
            session_id="sess-mojibake",
            user_id=operator.id,
            brand="atlas",
            agent_binding_id=atlas_binding.id,
            generated_title="乱码修复验证",
            last_message_preview="æ¨å¥½ï¼",
            message_log=json.dumps([
                {"id": "m1", "role": "operator", "title": "运营人员", "content": "你好", "createdAt": "2026-07-06 10:00"},
                {"id": "m2", "role": "assistant", "title": "qwen3.6-plus", "content": "æ¨å¥½ï¼", "createdAt": "2026-07-06 10:01"},
            ], ensure_ascii=False),
        ))
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            response = self.client.get("/api/assistant/sessions")
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 200)
        session = response.json()["items"][0]
        self.assertEqual(session["preview"], "您好！")
        self.assertEqual(session["messages"][1]["content"], "您好！")

    def test_assistant_chat_rejects_unauthorized_brand_and_persists_authorized_session_meta(self):
        operator = User(
            id=31,
            username="bound_operator",
            password_hash="hash",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
        )
        atlas_space = AssistantBrandSpace(brand="atlas", display_name="ATLAS")
        meadow_space = AssistantBrandSpace(brand="meadow", display_name="Meadow")
        self.db.add_all([operator, atlas_space, meadow_space])
        self.db.flush()
        atlas_binding = AssistantAgentBinding(
            brand_space_id=atlas_space.id,
            display_name="ATLAS Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        meadow_binding = AssistantAgentBinding(
            brand_space_id=meadow_space.id,
            display_name="MEADOW Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        self.db.add_all([atlas_binding, meadow_binding])
        self.db.flush()
        self.db.add(AssistantUserBinding(user_id=operator.id, brand_space_id=atlas_space.id, agent_binding_id=atlas_binding.id))
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            forbidden = self.client.post("/api/assistant/chat", json={
                "message": "分析Meadow",
                "sessionId": "sess-forbidden",
                "brand": "meadow",
                "agentBindingId": meadow_binding.id,
            })
            with patch("src.api.app.PiAgentAssistantService.chat", return_value={
                "sessionId": "sess-atlas-ok",
                "message": "建议先复核高价商品",
                "proposedActions": [],
                "contextSummary": {"taskCount": 1},
                "status": "ok",
                "runtime": {
                    "provider": "PI Agent",
                    "modelLabel": "gpt-4.1",
                    "agentId": "agt-atlas",
                },
            }) as mock_chat:
                response = self.client.post("/api/assistant/chat", json={
                    "message": "分析 Atlas 衬衫女士",
                    "sessionId": "sess-atlas-ok",
                    "brand": "atlas",
                    "agentBindingId": atlas_binding.id,
                })
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(response.status_code, 200)
        stored = self.db.query(AssistantSessionMeta).filter(
            AssistantSessionMeta.session_id == "sess-atlas-ok"
        ).one()
        self.assertEqual(stored.user_id, operator.id)
        self.assertEqual(stored.brand, "atlas")
        self.assertEqual(stored.agent_binding_id, atlas_binding.id)
        self.assertIn("分析 Atlas 衬衫女士", stored.message_log)
        self.assertEqual(mock_chat.call_args.kwargs["allowed_brands"], ["atlas"])

    def test_assistant_chat_rejects_switching_existing_session_to_different_brand(self):
        operator = User(
            id=33,
            username="session_brand_operator",
            password_hash="hash",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas", "meadow"]',
        )
        atlas_space = AssistantBrandSpace(brand="atlas", display_name="ATLAS")
        meadow_space = AssistantBrandSpace(brand="meadow", display_name="Meadow")
        self.db.add_all([operator, atlas_space, meadow_space])
        self.db.flush()
        atlas_binding = AssistantAgentBinding(
            brand_space_id=atlas_space.id,
            display_name="ATLAS Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        meadow_binding = AssistantAgentBinding(
            brand_space_id=meadow_space.id,
            display_name="MEADOW Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        self.db.add_all([atlas_binding, meadow_binding])
        self.db.flush()
        self.db.add_all([
            AssistantUserBinding(user_id=operator.id, brand_space_id=atlas_space.id, agent_binding_id=atlas_binding.id),
            AssistantUserBinding(user_id=operator.id, brand_space_id=meadow_space.id, agent_binding_id=meadow_binding.id),
        ])
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            with patch("src.api.app.PiAgentAssistantService.chat", return_value={
                "sessionId": "locked-session",
                "message": "先看 atlas",
                "proposedActions": [],
                "contextSummary": {"taskCount": 1},
                "status": "ok",
            }):
                first_response = self.client.post("/api/assistant/chat", json={
                    "message": "先看 atlas",
                    "sessionId": "locked-session",
                    "brand": "atlas",
                    "agentBindingId": atlas_binding.id,
                })
            second_response = self.client.post("/api/assistant/chat", json={
                "message": "再看 meadow",
                "sessionId": "locked-session",
                "brand": "meadow",
                "agentBindingId": meadow_binding.id,
            })
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 409)
        self.assertIn("session brand", second_response.json()["detail"])
        stored = self.db.query(AssistantSessionMeta).filter(
            AssistantSessionMeta.session_id == "locked-session"
        ).one()
        self.assertEqual(stored.brand, "atlas")

    def test_assistant_chat_retry_overwrites_assistant_node_with_version_history(self):
        operator = User(
            id=131,
            username="retry_operator",
            password_hash="hash",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
        )
        atlas_space = AssistantBrandSpace(brand="atlas", display_name="ATLAS")
        self.db.add_all([operator, atlas_space])
        self.db.flush()
        atlas_binding = AssistantAgentBinding(
            brand_space_id=atlas_space.id,
            display_name="ATLAS Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        self.db.add(atlas_binding)
        self.db.flush()
        self.db.add(AssistantUserBinding(user_id=operator.id, brand_space_id=atlas_space.id, agent_binding_id=atlas_binding.id))
        self.db.add(AssistantSessionMeta(
            session_id="sess-retry",
            user_id=operator.id,
            brand="atlas",
            agent_binding_id=atlas_binding.id,
            generated_title="重试验证",
            last_message_preview="初版回答",
            message_log=json.dumps([
                {"id": "op-1", "role": "operator", "title": "运营人员", "content": "帮我再看一次", "createdAt": "2026-07-06 10:00"},
                {"id": "as-1", "role": "assistant", "title": "qwen3.6-plus", "content": "初版回答", "createdAt": "2026-07-06 10:01"},
            ], ensure_ascii=False),
        ))
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            with patch("src.api.app.PiAgentAssistantService.chat", return_value={
                "sessionId": "sess-retry",
                "message": "重试后的回答",
                "proposedActions": [],
                "contextSummary": {"taskCount": 1},
                "status": "ok",
                "runtime": {
                    "provider": "PI Agent",
                    "modelLabel": "gpt-4.1",
                    "agentId": "agt-atlas",
                },
            }):
                response = self.client.post("/api/assistant/chat", json={
                    "message": "帮我再看一次",
                    "sessionId": "sess-retry",
                    "brand": "atlas",
                    "agentBindingId": atlas_binding.id,
                    "retryOfMessageId": "as-1",
                })
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 200)
        stored = self.db.query(AssistantSessionMeta).filter(
            AssistantSessionMeta.session_id == "sess-retry"
        ).one()
        messages = json.loads(stored.message_log)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["content"], "重试后的回答")
        self.assertEqual(len(messages[1]["versions"]), 2)
        self.assertEqual(messages[1]["versions"][0]["content"], "初版回答")
        self.assertEqual(messages[1]["versions"][1]["content"], "重试后的回答")

    def test_admin_can_manage_assistant_access_config(self):
        self.db.merge(User(
            id=1,
            username="test_admin",
            password_hash="hash",
            is_admin=1,
            role="platform_admin",
            is_active=1,
            page_permissions="[]",
            brand_permissions='["atlas"]',
            action_permissions="[]",
        ))
        self.db.commit()
        create_response = self.client.post("/api/admin/assistant/access-config", json={
            "brandSpaces": [
                {"brand": "atlas", "displayName": "ATLAS", "enabled": True},
            ],
            "agentBindings": [],
            "userBindings": [],
        })
        self.assertEqual(create_response.status_code, 200)
        brand_space_id = create_response.json()["brandSpaces"][0]["id"]

        update_response = self.client.post("/api/admin/assistant/access-config", json={
            "brandSpaces": [
                {"id": brand_space_id, "brand": "atlas", "displayName": "ATLAS", "enabled": True},
            ],
            "agentBindings": [
                {
                    "brandSpaceId": brand_space_id,
                    "displayName": "ATLAS Agent",
                    "modelDisplayName": "gpt-4.1",
                    "bindingType": "team",
                    "enabled": True,
                },
            ],
            "userBindings": [
                {
                    "userId": 1,
                    "brandSpaceId": brand_space_id,
                    "agentBindingId": None,
                },
            ],
        })
        self.assertEqual(update_response.status_code, 200)

        response = self.client.get("/api/admin/assistant/access-config")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["brandSpaces"][0]["brand"], "atlas")
        self.assertEqual(payload["agentBindings"][0]["bindingType"], "team")
        self.assertEqual(payload["userBindings"][0]["userId"], 1)

    def test_api_startup_initializes_database_tables(self):
        with patch.object(api_module, "init_db") as mock_init_db:
            with TestClient(app) as startup_client:
                response = startup_client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_init_db.called)

    def test_assistant_context_returns_real_model_metadata(self):
        operator = User(
            id=41,
            username="model_operator",
            password_hash="hash",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
        )
        atlas_space = AssistantBrandSpace(brand="atlas", display_name="ATLAS")
        self.db.add_all([operator, atlas_space])
        self.db.flush()
        atlas_binding = AssistantAgentBinding(
            brand_space_id=atlas_space.id,
            display_name="ATLAS Agent",
            model_display_name="ATLAS Agent",
            model_name="gpt-4.1",
            available_models_json='["gpt-4.1","gpt-4o"]',
            supports_model_switch=1,
            binding_type="agent",
            enabled=1,
        )
        self.db.add(atlas_binding)
        self.db.flush()
        self.db.add(AssistantUserBinding(user_id=operator.id, brand_space_id=atlas_space.id, agent_binding_id=atlas_binding.id))
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            response = self.client.get("/api/dashboard/assistant-context")
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["availableBindings"][0]["modelName"], "gpt-4.1")
        self.assertEqual(payload["availableBindings"][0]["availableModels"], ["gpt-4.1", "gpt-4o"])
        self.assertTrue(payload["availableBindings"][0]["supportsModelSwitch"])

    def test_prepare_assistant_action_rejects_brand_mismatch_with_session_brand(self):
        operator = User(
            id=52,
            username="session_action_operator",
            password_hash="hash",
            is_admin=0,
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas", "meadow"]',
        )
        self.db.add(operator)
        self.db.flush()
        self.db.add(AssistantSessionMeta(
            session_id="assistant-session-locked",
            user_id=operator.id,
            brand="atlas",
            generated_title="已绑定会话",
            last_message_preview="先看 atlas",
            message_log='[]',
            proposed_actions_snapshot='[]',
        ))
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            response = self.client.post("/api/assistant/actions/prepare", json={
                "sessionId": "assistant-session-locked",
                "action": {
                    "actionType": "cancel_blacklist",
                    "brand": "meadow",
                    "query": "女装",
                    "target": "SKU-2",
                    "reason": "误伤恢复",
                    "riskLevel": "medium",
                },
            })
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 409)
        self.assertIn("session brand", response.json()["detail"])

    def test_prepare_and_confirm_assistant_action_executes_cancel_blacklist(self):
        operator = User(
            id=51,
            username="action_operator",
            password_hash="hash",
            is_admin=0,
            role="brand_member",
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
            action_permissions='["cancel_governance_actions"]',
        )
        self.db.add(operator)
        self.db.flush()
        self.db.add(AssistantSessionMeta(
            session_id="assistant-session-1",
            user_id=operator.id,
            brand="atlas",
            generated_title="黑名单恢复",
            last_message_preview="误伤恢复",
            message_log='[]',
            proposed_actions_snapshot='[]',
        ))
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        with patch.object(api_module.service, "remove_from_blacklist", return_value=True) as mock_remove:
            try:
                prepare_response = self.client.post("/api/assistant/actions/prepare", json={
                    "sessionId": "assistant-session-1",
                    "action": {
                        "actionType": "cancel_blacklist",
                        "brand": "atlas",
                        "query": "衬衫 女士",
                        "target": "SKU-1",
                        "reason": "误伤恢复",
                        "riskLevel": "medium",
                    },
                })
                self.assertEqual(prepare_response.status_code, 200)
                prepared_id = prepare_response.json()["id"]

                confirm_response = self.client.post(f"/api/assistant/actions/{prepared_id}/confirm", json={})
                retry_response = self.client.post(f"/api/assistant/actions/{prepared_id}/confirm", json={})
            finally:
                app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(confirm_response.status_code, 200)
        self.assertEqual(retry_response.status_code, 200)
        confirmed = confirm_response.json()
        self.assertEqual(confirmed["status"], "executed")
        mock_remove.assert_called_once_with(
            "atlas",
            "SKU-1",
            ["衬衫 女士"],
            external_request_id=ANY,
        )
        self.assertTrue(confirmed["payload"]["externalRequestId"])

    def test_confirm_cancel_demotion_persists_action_facts_and_replays(self):
        operator = User(
            id=97,
            username="demotion_action_operator",
            password_hash="hash",
            is_admin=0,
            role="brand_member",
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
            action_permissions='["cancel_governance_actions"]',
        )
        session = AssistantSessionMeta(
            session_id="assistant-demotion-gateway-session",
            user_id=operator.id,
            brand="atlas",
            message_log="[]",
            proposed_actions_snapshot="[]",
        )
        self.db.add_all([operator, session])
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            with patch.object(
                api_module.service,
                "sync_broken_code_to_redis",
                return_value=True,
            ) as mock_execute, patch.object(
                api_module.service,
                "get_ops_low_weight_config",
                return_value={"success": True, "product_ids": [], "version": "v1"},
            ) as mock_readback:
                prepared = self.client.post("/api/assistant/actions/prepare", json={
                    "sessionId": session.session_id,
                    "action": {
                        "actionType": "cancel_demotion",
                        "brand": "atlas",
                        "query": "dress",
                        "target": "SKU-97",
                        "reason": "误伤恢复",
                        "riskLevel": "medium",
                    },
                })
                self.assertEqual(prepared.status_code, 200)
                action_request_id = prepared.json()["id"]
                first = self.client.post(
                    f"/api/assistant/actions/{action_request_id}/confirm",
                    json={},
                )
                second = self.client.post(
                    f"/api/assistant/actions/{action_request_id}/confirm",
                    json={},
                )
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["status"], "executed")
        self.assertEqual(second.json(), first.json())
        mock_execute.assert_called_once()
        self.assertTrue(mock_execute.call_args.kwargs["external_request_id"])
        mock_readback.assert_called_once()
        intent = self.db.query(ActionIntentFact).one()
        self.assertEqual(intent.status, "success")
        attempt = self.db.query(ActionAttempt).one()
        self.assertEqual(attempt.status, "success")
        self.assertEqual(attempt.execution_generation, 1)
        self.assertEqual(
            attempt.external_request_id,
            f"assistant-confirm-cancel-demotion:{action_request_id}",
        )
        self.assertEqual(self.db.query(ActionReadback).one().authoritative, 1)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")

    def test_confirm_cancel_demotion_readback_unavailable_fails_closed(self):
        operator = User(
            id=98,
            username="demotion_unknown_operator",
            password_hash="hash",
            is_admin=0,
            role="brand_member",
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
            action_permissions='["cancel_governance_actions"]',
        )
        session = AssistantSessionMeta(
            session_id="assistant-demotion-unknown-session",
            user_id=operator.id,
            brand="atlas",
            message_log="[]",
            proposed_actions_snapshot="[]",
        )
        self.db.add_all([operator, session])
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            with patch.object(
                api_module.service,
                "sync_broken_code_to_redis",
                return_value=True,
            ) as mock_execute, patch.object(
                api_module.service,
                "get_ops_low_weight_config",
                return_value=None,
            ) as mock_readback:
                prepared = self.client.post("/api/assistant/actions/prepare", json={
                    "sessionId": session.session_id,
                    "action": {
                        "actionType": "cancel_demotion",
                        "brand": "atlas",
                        "query": "dress",
                        "target": "SKU-98",
                    },
                })
                self.assertEqual(prepared.status_code, 200)
                action_request_id = prepared.json()["id"]
                first = self.client.post(
                    f"/api/assistant/actions/{action_request_id}/confirm",
                    json={},
                )
                replay = self.client.post(
                    f"/api/assistant/actions/{action_request_id}/confirm",
                    json={},
                )
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_execute.assert_called_once()
        mock_readback.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionReadback).one().authoritative, 0)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_confirm_cancel_demotion_projection_failure_requires_reconcile(self):
        operator = User(
            id=99,
            username="demotion_projection_operator",
            password_hash="hash",
            is_admin=0,
            role="brand_member",
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
            action_permissions='["cancel_governance_actions"]',
        )
        session = AssistantSessionMeta(
            session_id="assistant-demotion-projection-session",
            user_id=operator.id,
            brand="atlas",
            message_log="[]",
            proposed_actions_snapshot="[]",
        )
        recovery_task = InventoryRecoveryTask(
            brand="atlas",
            original_query="dress",
            product_id="SKU-99",
            status="monitoring",
        )
        self.db.add_all([operator, session, recovery_task])
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            with patch.object(
                api_module.service,
                "sync_broken_code_to_redis",
                return_value=True,
            ) as mock_execute, patch.object(
                api_module.service,
                "get_ops_low_weight_config",
                return_value={"success": True, "product_ids": [], "version": "v1"},
            ) as mock_readback, patch.object(
                api_module,
                "ActionLog",
                side_effect=RuntimeError("projection unavailable"),
            ) as mock_projection:
                prepared = self.client.post("/api/assistant/actions/prepare", json={
                    "sessionId": session.session_id,
                    "action": {
                        "actionType": "cancel_demotion",
                        "brand": "atlas",
                        "query": "dress",
                        "target": "SKU-99",
                    },
                })
                self.assertEqual(prepared.status_code, 200)
                action_request_id = prepared.json()["id"]
                with TestClient(app, raise_server_exceptions=False) as client:
                    first = client.post(
                        f"/api/assistant/actions/{action_request_id}/confirm",
                        json={},
                    )
                    replay = client.post(
                        f"/api/assistant/actions/{action_request_id}/confirm",
                        json={},
                    )
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_execute.assert_called_once()
        mock_readback.assert_called_once()
        mock_projection.assert_called_once()
        self.assertEqual(self.db.query(ActionLog).count(), 0)
        self.assertEqual(self.db.get(InventoryRecoveryTask, recovery_task.id).status, "monitoring")
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionReadback).one().authoritative, 0)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_prepare_assistant_action_requires_current_action_permission(self):
        operator = User(
            id=86,
            username="read_only_assistant_operator",
            is_admin=0,
            role="brand_member",
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
            action_permissions="[]",
        )
        session = AssistantSessionMeta(
            session_id="read-only-assistant-session",
            user_id=operator.id,
            brand="atlas",
            message_log="[]",
            proposed_actions_snapshot="[]",
        )
        self.db.add_all([operator, session])
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            response = self.client.post("/api/assistant/actions/prepare", json={
                "sessionId": session.session_id,
                "action": {
                    "actionType": "cancel_blacklist",
                    "brand": "atlas",
                    "query": "dress",
                    "target": "SKU-1",
                },
            })
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 403)

    def test_prepare_assistant_action_rejects_cross_brand_no_result_object(self):
        operator = User(
            id=85,
            username="atlas_no_result_operator",
            is_admin=0,
            role="brand_member",
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
            action_permissions='["execute_governance_actions"]',
        )
        session = AssistantSessionMeta(
            session_id="cross-brand-no-result-session",
            user_id=operator.id,
            brand="atlas",
            message_log="[]",
            proposed_actions_snapshot="[]",
        )
        query = NoResultQuery(brand="harbor", query_text="linen trousers")
        self.db.add_all([operator, session, query])
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            brand="harbor",
            query_text="linen trousers",
            action_type="retag",
            status="pending_execute",
            request_payload="{}",
        )
        self.db.add(action)
        self.db.commit()

        async def override_operator():
            return operator

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            response = self.client.post("/api/assistant/actions/prepare", json={
                "sessionId": session.session_id,
                "action": {
                    "actionType": "execute_no_result_action",
                    "brand": "atlas",
                    "query": "linen trousers",
                    "payload": {"actionId": action.id, "queryId": query.id},
                },
            })
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        self.assertEqual(response.status_code, 403)

    def test_platform_admin_can_confirm_cross_brand_assistant_action(self):
        session = AssistantSessionMeta(
            session_id="platform-admin-harbor-session",
            user_id=1,
            brand="harbor",
            message_log="[]",
            proposed_actions_snapshot="[]",
        )
        task = TopQueryTask(
            brand="harbor",
            query_text="jeans",
            data_date=datetime.date.today(),
            query_source="top_query",
        )
        self.db.add_all([session, task])
        self.db.flush()
        action = TopQueryActionPlan(
            task_id=task.id,
            brand="harbor",
            query_text="jeans",
            action_type="query_demote",
            execution_mode="human_approval",
            status="approved",
            request_payload="{}",
        )
        self.db.add(action)
        self.db.commit()

        with patch.object(
            api_module.TopQueryGovernanceService,
            "execute_approved_action",
            return_value={"status": "executed"},
        ) as mock_execute:
            prepared = self.client.post("/api/assistant/actions/prepare", json={
                "sessionId": session.session_id,
                "action": {
                    "actionType": "execute_top_query_action",
                    "brand": "harbor",
                    "query": "jeans",
                    "payload": {"actionId": action.id, "taskId": task.id},
                },
            })
            self.assertEqual(prepared.status_code, 200)
            confirmed = self.client.post(
                f"/api/assistant/actions/{prepared.json()['id']}/confirm",
                json={},
            )

        self.assertEqual(confirmed.status_code, 200)
        mock_execute.assert_called_once()

    def test_confirm_assistant_action_rechecks_live_permission_state_and_expiry(self):
        operator = User(
            id=84,
            username="live_authorization_operator",
            is_admin=0,
            role="brand_member",
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
            action_permissions='["execute_governance_actions"]',
        )
        session = AssistantSessionMeta(
            session_id="live-authorization-session",
            user_id=operator.id,
            brand="atlas",
            message_log="[]",
            proposed_actions_snapshot="[]",
        )
        task = TopQueryTask(
            brand="atlas",
            query_text="dress",
            data_date=datetime.date.today(),
            query_source="top_query",
        )
        self.db.add_all([operator, session, task])
        self.db.flush()
        action = TopQueryActionPlan(
            task_id=task.id,
            brand="atlas",
            query_text="dress",
            action_type="query_demote",
            execution_mode="human_approval",
            status="approved",
            request_payload="{}",
        )
        self.db.add(action)
        self.db.commit()

        async def override_operator():
            return operator

        def prepare_action():
            response = self.client.post("/api/assistant/actions/prepare", json={
                "sessionId": session.session_id,
                "action": {
                    "actionType": "execute_top_query_action",
                    "brand": "atlas",
                    "query": "dress",
                    "payload": {"actionId": action.id, "taskId": task.id},
                },
            })
            self.assertEqual(response.status_code, 200)
            return response.json()["id"]

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            with patch.object(
                api_module.TopQueryGovernanceService,
                "execute_approved_action",
                return_value={"status": "executed"},
            ) as mock_execute:
                permission_revoked_id = prepare_action()
                operator.action_permissions = "[]"
                revoked_response = self.client.post(
                    f"/api/assistant/actions/{permission_revoked_id}/confirm",
                    json={},
                )
                self.assertEqual(revoked_response.status_code, 403)
                operator.action_permissions = '["execute_governance_actions"]'

                state_changed_id = prepare_action()
                action.status = "executed"
                self.db.commit()
                state_response = self.client.post(
                    f"/api/assistant/actions/{state_changed_id}/confirm",
                    json={},
                )
                self.assertEqual(state_response.status_code, 409)
                action.status = "approved"
                self.db.commit()

                expired_id = prepare_action()
                prepared = self.db.get(AssistantActionRequest, expired_id)
                prepared.created_at = datetime.datetime.now() - datetime.timedelta(hours=2)
                self.db.commit()
                with patch.dict(os.environ, {"ASSISTANT_ACTION_PREPARE_TTL_SECONDS": "60"}):
                    expired_response = self.client.post(
                        f"/api/assistant/actions/{expired_id}/confirm",
                        json={},
                    )
                self.assertEqual(expired_response.status_code, 409)
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        mock_execute.assert_not_called()

    def test_confirm_assistant_action_ignores_removed_legacy_binding_but_checks_binding_state(self):
        operator = User(
            id=87,
            username="binding_revocation_operator",
            is_admin=0,
            role="brand_member",
            is_active=1,
            page_permissions='["/smart-assistant"]',
            brand_permissions='["atlas"]',
            action_permissions='["execute_governance_actions"]',
        )
        space = AssistantBrandSpace(brand="atlas", display_name="ATLAS", enabled=1)
        self.db.add_all([operator, space])
        self.db.flush()
        binding = AssistantAgentBinding(
            brand_space_id=space.id,
            display_name="ATLAS Agent",
            model_display_name="gpt-4.1",
            binding_type="agent",
            enabled=1,
        )
        self.db.add(binding)
        self.db.flush()
        user_binding = AssistantUserBinding(
            user_id=operator.id,
            brand_space_id=space.id,
            agent_binding_id=binding.id,
        )
        session = AssistantSessionMeta(
            session_id="binding-revocation-session",
            user_id=operator.id,
            brand="atlas",
            agent_binding_id=binding.id,
            message_log="[]",
            proposed_actions_snapshot="[]",
        )
        task = TopQueryTask(
            brand="atlas",
            query_text="dress",
            data_date=datetime.date.today(),
            query_source="top_query",
        )
        self.db.add_all([user_binding, session, task])
        self.db.flush()
        action = TopQueryActionPlan(
            task_id=task.id,
            brand="atlas",
            query_text="dress",
            action_type="query_demote",
            execution_mode="human_approval",
            status="approved",
            request_payload="{}",
        )
        self.db.add(action)
        self.db.commit()

        async def override_operator():
            return operator

        def prepare_action():
            response = self.client.post("/api/assistant/actions/prepare", json={
                "sessionId": session.session_id,
                "action": {
                    "actionType": "execute_top_query_action",
                    "brand": "atlas",
                    "query": "dress",
                    "payload": {"actionId": action.id, "taskId": task.id},
                },
            })
            self.assertEqual(response.status_code, 200)
            return response.json()["id"]

        previous_override = app.dependency_overrides[get_current_user]
        app.dependency_overrides[get_current_user] = override_operator
        try:
            with patch.object(
                api_module.TopQueryGovernanceService,
                "execute_approved_action",
                return_value={"status": "executed"},
            ) as mock_execute:
                removed_binding_id = prepare_action()
                self.db.delete(user_binding)
                self.db.commit()
                removed_response = self.client.post(
                    f"/api/assistant/actions/{removed_binding_id}/confirm",
                    json={},
                )
                self.assertEqual(removed_response.status_code, 200)

                user_binding = AssistantUserBinding(
                    user_id=operator.id,
                    brand_space_id=space.id,
                    agent_binding_id=binding.id,
                )
                self.db.add(user_binding)
                self.db.commit()
                disabled_binding_id = prepare_action()
                binding.enabled = 0
                self.db.commit()
                disabled_binding_response = self.client.post(
                    f"/api/assistant/actions/{disabled_binding_id}/confirm",
                    json={},
                )
                self.assertEqual(disabled_binding_response.status_code, 403)

                binding.enabled = 1
                self.db.commit()
                disabled_space_id = prepare_action()
                space.enabled = 0
                self.db.commit()
                disabled_space_response = self.client.post(
                    f"/api/assistant/actions/{disabled_space_id}/confirm",
                    json={},
                )
                self.assertEqual(disabled_space_response.status_code, 403)
        finally:
            app.dependency_overrides[get_current_user] = previous_override

        mock_execute.assert_called_once()

if __name__ == '__main__':
    unittest.main()
