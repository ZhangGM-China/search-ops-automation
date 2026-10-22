import json

from fastapi.testclient import TestClient

from src.assistant.tools import AssistantToolService
from src.api import app
from src.persistence import SessionLocal, User


def test_assistant_tool_service_exposes_structured_pi_tool_execution(monkeypatch):
    service = AssistantToolService()
    monkeypatch.setattr(
        service,
        "_build_today_summary",
        lambda context: {
            "message": "今日已读取 2 条任务。",
            "toolCalls": [{"name": "read_today_context", "arguments": "{}"}],
            "toolResults": [{"toolName": "read_today_context", "summary": "已读取"}],
            "status": "ok",
        },
    )

    result = service.execute_tool(
        "read_today_context",
        {},
        allowed_brands=["atlas", "harbor"],
        context={"summary": {"taskCount": 2}},
    )

    assert result["status"] == "ok"
    assert result["message"] == "今日已读取 2 条任务。"


def test_assistant_tool_service_rejects_unknown_tool():
    service = AssistantToolService()

    result = service.execute_tool(
        "drop_database",
        {},
        allowed_brands=["atlas"],
        context={},
    )

    assert result["status"] == "failed"
    assert "不支持" in result["message"]


def test_internal_pi_tool_endpoint_requires_token_and_uses_server_account_scope(monkeypatch):
    monkeypatch.setenv("PI_AGENT_INTERNAL_TOKEN", "test-runtime-token")
    db = SessionLocal()
    db.add(User(
        id=901,
        username="pi-runtime-user",
        is_active=1,
        is_admin=0,
        brand_permissions=json.dumps(["atlas"]),
        page_permissions=json.dumps(["/smart-assistant"]),
    ))
    db.commit()
    db.close()

    with TestClient(app) as client:
        denied = client.post(
            "/api/internal/assistant/tools/read_today_context",
            json={"userId": 901, "arguments": {}},
        )
        assert denied.status_code == 401

        allowed = client.post(
            "/api/internal/assistant/tools/read_today_context",
            headers={"X-PI-Internal-Token": "test-runtime-token"},
            json={"userId": 901, "arguments": {}, "context": {"summary": {"taskCount": 3}}},
        )
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "ok"
