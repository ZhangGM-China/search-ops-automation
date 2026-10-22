from src.config import get_brand_business_config
from src.integrations.operational_service import OperationalService
from src.persistence.schema import DEFAULT_ADMIN_BRAND_PERMISSIONS


def test_lumen_uses_fixed_common_business_credentials(monkeypatch):
    monkeypatch.setenv("LUMEN_API_KEY", "lumen-test-secret")
    service = OperationalService.__new__(OperationalService)
    service.env = "test"

    config = service._get_hardcoded_config("lumen")

    assert config["api_alias"] == "alias_lumen"
    assert config["tenantId"] == 243
    assert config["apiKey"] == "lumen-test-secret"
    assert config["base_url"] == "http://staging.example.test"
    assert service._get_api_alias("lumen") == "alias_lumen"
    assert service._get_headers("lumen") == {
        "Authorization": "Bearer lumen-test-secret",
        "tenant-id": "243",
        "Content-Type": "application/json",
    }


def test_lumen_is_visible_but_governance_waits_for_data_source():
    config = get_brand_business_config("lumen")

    assert config["enabled"] is True
    assert config["api_alias"] == "alias_lumen"
    assert config["top_query_governance_enabled"] is False
    assert config["no_result_governance_enabled"] is False
    assert "lumen" in DEFAULT_ADMIN_BRAND_PERMISSIONS
