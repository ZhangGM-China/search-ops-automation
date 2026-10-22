import json
from copy import deepcopy
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api import app, get_current_user
from src.persistence import Base, PLATFORM_ADMIN_ROLE, SessionLocal, User, reset_engine_for_testing
from src.config import DEFAULT_BRAND_BUSINESS_CONFIG
from src.api.application import admin as admin_module


@pytest.fixture()
def client():
    engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=engine)
    admin = User(
        id=901,
        username="relevance_config_admin",
        role=PLATFORM_ADMIN_ROLE,
        is_admin=1,
        is_active=1,
        page_permissions="[]",
        brand_permissions="[]",
        action_permissions=json.dumps(["manage_platform_config"]),
    )
    db = SessionLocal()
    db.add(admin)
    db.commit()
    db.close()
    admin_fact = User(
        id=901,
        username="relevance_config_admin",
        role=PLATFORM_ADMIN_ROLE,
        is_admin=1,
        is_active=1,
        page_permissions="[]",
        brand_permissions="[]",
        action_permissions=json.dumps(["manage_platform_config"]),
    )
    app.dependency_overrides[get_current_user] = lambda: admin_fact
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_dashboard_config_exposes_relevance_runtime_defaults(client):
    raw_config = {"brand_configs": {"atlas": {"enabled": True}}}
    business_config = {**DEFAULT_BRAND_BUSINESS_CONFIG, **raw_config["brand_configs"]["atlas"]}
    with (
        patch.object(admin_module, "_raw_config", raw_config),
        patch.object(admin_module, "BRAND_CONFIGS", raw_config["brand_configs"]),
        patch.object(admin_module, "GLOBAL_SAFETY_CONFIG", {}),
        patch.object(admin_module, "reload_config"),
        patch.object(admin_module, "get_brand_business_config", return_value=business_config),
    ):
        response = client.get("/api/admin/config")
    assert response.status_code == 200
    config = response.json()["brand_configs"]["atlas"]
    assert config["query_governance_relevance_multimodal_concurrency"] == 5
    assert config["query_governance_relevance_batch_size"] == 240
    assert "query_governance_relevance_request_max_bytes" not in config
    assert config["query_governance_relevance_model_timeout_seconds"] == 1200
    assert config["query_governance_relevance_image_download_timeout_seconds"] == 30
    assert "query_governance_relevance_max_completion_tokens" not in config


def test_dashboard_config_exposes_vector_reorder_runtime_defaults(client):
    raw_config = {"brand_configs": {"atlas": {"enabled": True}}}
    business_config = {**DEFAULT_BRAND_BUSINESS_CONFIG, **raw_config["brand_configs"]["atlas"]}
    with (
        patch.object(admin_module, "_raw_config", raw_config),
        patch.object(admin_module, "BRAND_CONFIGS", raw_config["brand_configs"]),
        patch.object(admin_module, "GLOBAL_SAFETY_CONFIG", {}),
        patch.object(admin_module, "reload_config"),
        patch.object(admin_module, "get_brand_business_config", return_value=business_config),
    ):
        response = client.get("/api/admin/config")
    assert response.status_code == 200
    config = response.json()["brand_configs"]["atlas"]
    assert config["query_governance_vector_reorder_concurrency"] == 10
    assert "query_governance_vector_reorder_timeout_seconds" not in config


def test_dashboard_config_persists_relevance_runtime_values_and_rejects_invalid_values(client):
    raw_config = {"brand_configs": {"atlas": {"enabled": True}}}

    def persist(candidate):
        raw_config.clear()
        raw_config.update(deepcopy(candidate))

    payload = {
        "brand_configs": {
            "atlas": {
                "query_governance_relevance_multimodal_concurrency": 3,
                "query_governance_relevance_batch_size": 24,
                "query_governance_relevance_model_timeout_seconds": 30,
                "query_governance_relevance_image_download_timeout_seconds": 9,
            }
        }
    }
    with (
        patch.object(admin_module, "_raw_config", raw_config),
        patch.object(admin_module, "BRAND_CONFIGS", raw_config["brand_configs"]),
        patch.object(admin_module, "GLOBAL_SAFETY_CONFIG", {}),
        patch.object(admin_module, "reload_config"),
        patch.object(admin_module, "save_config", side_effect=persist),
        patch.object(
            admin_module,
            "get_brand_business_config",
            side_effect=lambda _brand: {
                **DEFAULT_BRAND_BUSINESS_CONFIG,
                **raw_config["brand_configs"]["atlas"],
            },
        ),
    ):
        response = client.post("/api/admin/config", json=payload)
        assert response.status_code == 200

        values = client.get("/api/admin/config").json()["brand_configs"]["atlas"]
        for key, value in payload["brand_configs"]["atlas"].items():
            assert values[key] == value
        assert "query_governance_relevance_request_max_bytes" not in values
        assert "query_governance_relevance_max_completion_tokens" not in values

        invalid = client.post(
            "/api/admin/config",
            json={"brand_configs": {"atlas": {"query_governance_relevance_batch_size": 0}}},
        )
        assert invalid.status_code == 400


def test_dashboard_config_exposes_preference_generation_defaults(client):
    raw_config = {"brand_configs": {"atlas": {"enabled": True}}}
    business_config = {**DEFAULT_BRAND_BUSINESS_CONFIG, **raw_config["brand_configs"]["atlas"]}
    with (
        patch.object(admin_module, "_raw_config", raw_config),
        patch.object(admin_module, "BRAND_CONFIGS", raw_config["brand_configs"]),
        patch.object(admin_module, "GLOBAL_SAFETY_CONFIG", {}),
        patch.object(admin_module, "reload_config"),
        patch.object(admin_module, "get_brand_business_config", return_value=business_config),
    ):
        response = client.get("/api/admin/config")
    assert response.status_code == 200
    config = response.json()["brand_configs"]["atlas"]
    assert config["query_governance_preference_generation_batch_query_limit"] == 10
    assert config["query_governance_preference_generation_timeout_seconds"] == 1200


def test_dashboard_config_persists_kg_reference_and_audit_metadata(client):
    raw_config = {"brand_configs": {"atlas": {"enabled": True}}}

    def persist(candidate):
        raw_config.clear()
        raw_config.update(deepcopy(candidate))

    with (
        patch.object(admin_module, "_raw_config", raw_config),
        patch.object(admin_module, "BRAND_CONFIGS", raw_config["brand_configs"]),
        patch.object(admin_module, "GLOBAL_SAFETY_CONFIG", {}),
        patch.object(admin_module, "reload_config"),
        patch.object(admin_module, "save_config", side_effect=persist),
        patch.object(
            admin_module,
            "get_brand_business_config",
            side_effect=lambda _brand: {
                **DEFAULT_BRAND_BUSINESS_CONFIG,
                **raw_config["brand_configs"].get("atlas", {}),
            },
        ),
    ):
        response = client.post(
            "/api/admin/config",
            json={"brand_configs": {"atlas": {"query_governance_kg_reference": "花衬衫同义于印花衬衫"}}},
        )
        assert response.status_code == 200
        values = client.get("/api/admin/config").json()["brand_configs"]["atlas"]
        assert values["query_governance_kg_reference"] == "花衬衫同义于印花衬衫"
        assert values["query_governance_kg_reference_updated_by"] == "relevance_config_admin"
        assert values["query_governance_kg_reference_updated_at"]


def test_dashboard_config_persists_preference_generation_runtime_values(client):
    raw_config = {"brand_configs": {"atlas": {"enabled": True}}}

    def persist(candidate):
        raw_config.clear()
        raw_config.update(deepcopy(candidate))

    payload = {
        "brand_configs": {
            "atlas": {
                "query_governance_preference_generation_batch_query_limit": 10,
                "query_governance_preference_generation_timeout_seconds": 1200,
            }
        }
    }
    with (
        patch.object(admin_module, "_raw_config", raw_config),
        patch.object(admin_module, "BRAND_CONFIGS", raw_config["brand_configs"]),
        patch.object(admin_module, "GLOBAL_SAFETY_CONFIG", {}),
        patch.object(admin_module, "reload_config"),
        patch.object(admin_module, "save_config", side_effect=persist),
        patch.object(
            admin_module,
            "get_brand_business_config",
            side_effect=lambda _brand: {
                **DEFAULT_BRAND_BUSINESS_CONFIG,
                **raw_config["brand_configs"].get("atlas", {}),
            },
        ),
    ):
        response = client.post("/api/admin/config", json=payload)
        assert response.status_code == 200
        values = client.get("/api/admin/config").json()["brand_configs"]["atlas"]
        assert values["query_governance_preference_generation_batch_query_limit"] == 10
        assert values["query_governance_preference_generation_timeout_seconds"] == 1200

        invalid = client.post(
            "/api/admin/config",
            json={"brand_configs": {"atlas": {"query_governance_preference_generation_timeout_seconds": 0}}},
        )
        assert invalid.status_code == 400


def test_dashboard_config_persists_vector_reorder_runtime_values_and_rejects_invalid_values(client):
    raw_config = {"brand_configs": {"atlas": {"enabled": True}}}

    def persist(candidate):
        raw_config.clear()
        raw_config.update(deepcopy(candidate))

    payload = {
        "brand_configs": {
            "atlas": {
                "query_governance_vector_reorder_concurrency": 6,
            }
        }
    }
    with (
        patch.object(admin_module, "_raw_config", raw_config),
        patch.object(admin_module, "BRAND_CONFIGS", raw_config["brand_configs"]),
        patch.object(admin_module, "GLOBAL_SAFETY_CONFIG", {}),
        patch.object(admin_module, "reload_config"),
        patch.object(admin_module, "save_config", side_effect=persist),
        patch.object(
            admin_module,
            "get_brand_business_config",
            side_effect=lambda _brand: {
                **DEFAULT_BRAND_BUSINESS_CONFIG,
                **raw_config["brand_configs"]["atlas"],
            },
        ),
    ):
        response = client.post("/api/admin/config", json=payload)
        assert response.status_code == 200
        values = client.get("/api/admin/config").json()["brand_configs"]["atlas"]
        assert values["query_governance_vector_reorder_concurrency"] == 6
        assert "query_governance_vector_reorder_timeout_seconds" not in values

        invalid = client.post(
            "/api/admin/config",
            json={
                "brand_configs": {
                    "atlas": {"query_governance_vector_reorder_concurrency": 0}
                }
            },
        )
        assert invalid.status_code == 400
