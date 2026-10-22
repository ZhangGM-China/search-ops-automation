import hashlib
from unittest.mock import MagicMock, patch

import pytest

from src.integrations.business_http import CircuitRegistry
from src.integrations.knowledge_graph import KnowledgeGraphClient


def test_post_returns_permit_and_records_malformed_200_as_failure():
    response = MagicMock(status_code=200, headers={}, text="not-json")
    response.json.side_effect = ValueError("invalid json")
    http = MagicMock()
    http.post.return_value = response
    client = KnowledgeGraphClient(http_client=http, token="kg-token", environment="test")
    client.circuit_registry = CircuitRegistry(failure_threshold=2, cooldown_seconds=60)

    result = client.execute({
        "brand": "summit",
        "actionType": "add_relationship",
        "params": {"tagId": "child", "responseTagId": "parent"},
    })

    assert result == {"code": -1, "message": "not-json"}
    token_hash = hashlib.sha256(b"kg-token").hexdigest()[:16]
    status = client.circuit_registry.status(f"test|kg.example.test|kg:{token_hash}")
    assert status["consecutive_failures"] == 1


@patch("src.integrations.operational_service.OperationalService._refresh_brand_configs")
def test_get_scene_id_records_malformed_200_as_failure_and_preserves_parse_error(_refresh):
    response = MagicMock(status_code=200, headers={}, text="not-json")
    response.json.side_effect = ValueError("invalid json")
    http = MagicMock()
    http.get.return_value = response
    client = KnowledgeGraphClient(http_client=http, token="kg-token", environment="test")
    client.circuit_registry = CircuitRegistry(failure_threshold=2, cooldown_seconds=60)

    with pytest.raises(ValueError, match="invalid json"):
        client.get_scene_id("atlas")

    status = client.circuit_registry.status(
        "test|search.example.test|tenant:235"
    )
    assert status["consecutive_failures"] == 1


def test_post_returns_admission_permit_when_recording_success():
    permit = object()
    circuit = MagicMock()
    circuit.admit.return_value = permit
    http = MagicMock()
    http.post.return_value = MagicMock(
        status_code=200,
        headers={},
        text='{"code":1}',
        json=lambda: {"code": 1},
    )
    client = KnowledgeGraphClient(
        http_client=http,
        token="kg-token",
        environment="test",
        circuit_registry=circuit,
    )

    client.execute({
        "brand": "summit",
        "actionType": "add_relationship",
        "params": {"tagId": "child", "responseTagId": "parent"},
    })

    token_hash = hashlib.sha256(b"kg-token").hexdigest()[:16]
    circuit.record_success.assert_called_once_with(
        f"test|kg.example.test|kg:{token_hash}",
        permit,
    )


@patch("src.integrations.operational_service.OperationalService._refresh_brand_configs")
def test_get_scene_id_returns_admission_permit_when_recording_success(_refresh):
    permit = object()
    circuit = MagicMock()
    circuit.admit.return_value = permit
    http = MagicMock()
    http.get.return_value = MagicMock(
        status_code=200,
        headers={},
        text='{"data":"{\\"sceneId\\":\\"scene-1\\"}"}',
        json=lambda: {"data": '{"sceneId":"scene-1"}'},
    )
    client = KnowledgeGraphClient(
        http_client=http,
        token="kg-token",
        environment="test",
        circuit_registry=circuit,
    )

    assert client.get_scene_id("atlas") == "scene-1"
    assert http.get.call_args.args[0] == (
        "http://search.example.test/admin-api/config/new/word/get/cfg_kg_key"
    )
    circuit.record_success.assert_called_once_with(
        "test|search.example.test|tenant:235",
        permit,
    )


@patch.dict("os.environ", {"ENV": "test", "LUMEN_API_KEY": "lumen-key"})
@patch("src.integrations.operational_service.OperationalService._refresh_brand_configs")
def test_lumen_scene_id_uses_production_config_endpoint_in_test_environment(_refresh):
    http = MagicMock()
    http.get.return_value = MagicMock(
        status_code=200,
        headers={},
        text='{"data":"{\\"sceneId\\":\\"lumen-scene\\"}"}',
        json=lambda: {"data": '{"sceneId":"lumen-scene"}'},
    )
    client = KnowledgeGraphClient(http_client=http, token="kg-token", environment="test")

    assert client.get_scene_id("lumen") == "lumen-scene"
    request = http.get.call_args
    assert request.args[0] == (
        "http://search.example.test/admin-api/config/new/word/get/cfg_kg_key"
    )
    assert request.kwargs["headers"]["Authorization"] == "Bearer lumen-key"
    assert request.kwargs["headers"]["tenant-id"] == "243"
