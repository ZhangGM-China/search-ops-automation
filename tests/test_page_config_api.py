import copy
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api import app, get_current_user
from importlib import import_module
from src.persistence import User

api_module = import_module("src.api.app")


def _persist_and_reload_config(raw_config):
    def persist(candidate):
        raw_config.clear()
        raw_config.update(copy.deepcopy(candidate))

    return persist


class TestPageConfigApi:
    def setup_method(self):
        self.previous_override = app.dependency_overrides.get(get_current_user)
        self.raw_config = {"brand_configs": {"atlas": {"enabled": True}}}
        self.client = TestClient(app)

        async def override_admin():
            return User(
                id=1,
                username="admin",
                is_admin=1,
                role="platform_admin",
                page_permissions=json.dumps(["/config"]),
                brand_permissions=json.dumps(["atlas"]),
                action_permissions=json.dumps(["manage_platform_config"]),
            )

        app.dependency_overrides[get_current_user] = override_admin

    def teardown_method(self):
        self.client.close()
        if self.previous_override is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = self.previous_override

    def test_reads_defaults_and_updates_known_values_with_version(self):
        with patch.object(api_module, "_raw_config", self.raw_config), \
             patch.object(api_module, "reload_config"), \
             patch.object(api_module, "save_config", side_effect=_persist_and_reload_config(self.raw_config)) as save_config:
            initial = self.client.get("/api/admin/page-config")

            assert initial.status_code == 200
            payload = initial.json()
            assert payload["schemaVersion"] == 2
            assert payload["version"] == 0
            assert payload["values"]["brand_overview.card.show_operational_data"] is True
            assert payload["values"]["brand_detail.yesterday.show"] is True
            assert payload["values"]["brand_overview.show_global_metrics"] is True
            assert payload["values"]["brand_overview.global_metrics"] == ["query_count", "pass_rate", "pending_count"]
            assert {item["key"] for item in payload["definitions"]} == set(payload["values"])

            updated = self.client.post(
                "/api/admin/page-config",
                json={
                    "expectedVersion": 0,
                    "values": {
                        "brand_overview.card.show_operational_data": False,
                        "brand_overview.show_global_metrics": False,
                    },
                },
            )

            assert updated.status_code == 200
            assert updated.json()["version"] == 1
            assert self.raw_config["page_config"]["values"]["brand_overview.card.show_operational_data"] is False
            assert self.raw_config["page_config"]["values"]["brand_overview.show_global_metrics"] is False
            assert self.raw_config["page_config"]["values"]["brand_detail.yesterday.show"] is True
            assert save_config.call_count == 1

            reread = self.client.get("/api/admin/page-config")
            assert reread.json()["values"]["brand_overview.card.show_operational_data"] is False
            assert reread.json()["values"]["brand_detail.yesterday.show"] is True

    def test_rejects_unknown_values_and_stale_versions_without_persisting(self):
        with patch.object(api_module, "_raw_config", self.raw_config), \
             patch.object(api_module, "reload_config"), \
             patch.object(api_module, "save_config", side_effect=_persist_and_reload_config(self.raw_config)) as save_config:
            unknown = self.client.post(
                "/api/admin/page-config",
                json={"values": {"future.unknown_key": True}},
            )
            assert unknown.status_code == 400
            assert save_config.call_count == 0

            first = self.client.post(
                "/api/admin/page-config",
                json={"expectedVersion": 0, "values": {"brand_detail.yesterday.show": False}},
            )
            assert first.status_code == 200

            stale = self.client.post(
                "/api/admin/page-config",
                json={"expectedVersion": 0, "values": {"brand_detail.yesterday.show": True}},
            )
            assert stale.status_code == 409
            assert save_config.call_count == 1

    def test_updates_metric_lists_with_bounds_and_readback(self):
        with patch.object(api_module, "_raw_config", self.raw_config), \
             patch.object(api_module, "reload_config"), \
             patch.object(api_module, "save_config", side_effect=_persist_and_reload_config(self.raw_config)) as save_config:
            update = self.client.post(
                "/api/admin/page-config",
                json={
                    "expectedVersion": 0,
                    "values": {"brand_overview.card.metrics": ["pass_rate", "pending_count"]},
                },
            )
            assert update.status_code == 200
            assert update.json()["values"]["brand_overview.card.metrics"] == ["pass_rate", "pending_count"]
            assert self.client.get("/api/admin/page-config").json()["values"]["brand_overview.card.metrics"] == [
                "pass_rate", "pending_count",
            ]

            invalid = self.client.post(
                "/api/admin/page-config",
                json={"expectedVersion": 1, "values": {"brand_overview.card.metrics": ["pass_rate"]}},
            )
            assert invalid.status_code == 400
            assert save_config.call_count == 1

    def test_brand_admin_can_read_but_cannot_update_page_config(self):
        async def override_brand_admin():
            return User(
                id=2,
                username="atlas-admin",
                is_admin=0,
                role="brand_admin",
                page_permissions=json.dumps(["/config"]),
                brand_permissions=json.dumps(["atlas"]),
                action_permissions=json.dumps(["manage_brand_config"]),
            )

        app.dependency_overrides[get_current_user] = override_brand_admin
        with patch.object(api_module, "_raw_config", self.raw_config):
            assert self.client.get("/api/admin/page-config").status_code == 200
            response = self.client.post(
                "/api/admin/page-config",
                json={"values": {"brand_overview.card.show_operational_data": False}},
            )
            assert response.status_code == 403


def test_page_config_defines_brand_overview_metric_slots():
    from src.page_config import page_config_payload

    payload = page_config_payload({})
    keys = {item["key"] for item in payload["definitions"]}
    assert {
        "brand_overview.global_metrics",
        "brand_overview.card.metrics",
    } <= keys
    assert payload["values"]["brand_overview.card.metrics"] == [
        "irrelevant_query_count",
        "preference_adjusted_query_count",
        "stock_bucket_reduction_product_count",
    ]
