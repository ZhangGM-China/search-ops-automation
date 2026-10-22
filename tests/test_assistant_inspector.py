from pathlib import Path

from fastapi.testclient import TestClient

from src.api import app, get_current_user
from src.api.application import assistant as assistant_application


def test_runtime_inspector_returns_account_scoped_tools_and_workspace(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(assistant_application, "configured_workspace_roots", lambda: [tmp_path])
    monkeypatch.setattr(assistant_application.AssistantToolService, "available_tools", lambda self: [
        {"name": "read_today_context", "description": "read context"},
    ])
    app.dependency_overrides[get_current_user] = lambda: type("UserStub", (), {
        "id": 1, "brand_permissions": '["atlas"]', "is_admin": 0,
    })()
    try:
        response = TestClient(app).get("/api/assistant/runtime-inspector")
        assert response.status_code == 200
        payload = response.json()
        assert payload["runtime"]["provider"] == "PI Agent"
        assert payload["workspace"]["roots"] == [str(tmp_path)]
        assert payload["permissions"]["accountId"] == 1
        assert payload["tools"][0]["name"] == "read_today_context"
        assert payload["events"] == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)
