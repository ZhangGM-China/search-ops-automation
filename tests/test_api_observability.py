import importlib
from pathlib import Path

from fastapi import Request
from fastapi.testclient import TestClient

from src.api import app, get_current_user
from src.api.application import query_governance
from src.query_governance.read_api import QueryGovernanceNotFoundError

app_module = importlib.import_module("src.api.app")


def _user():
    return type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()


def test_responses_include_opaque_request_id_and_server_timing():
    response = TestClient(app).get("/api/health/live")

    assert response.status_code == 200
    request_id = response.headers.get("x-request-id")
    assert request_id is not None
    assert len(request_id) == 32
    assert all(character in "0123456789abcdef" for character in request_id)
    assert response.headers["server-timing"].startswith("app;dur=")


def test_request_id_accepts_only_valid_proxy_shape():
    client = TestClient(app)
    accepted = client.get("/api/health/live", headers={"X-Request-ID": "A" * 32})
    rejected = client.get("/api/health/live", headers={"X-Request-ID": "invalid-value"})

    assert accepted.headers["x-request-id"] == "a" * 32
    assert rejected.headers["x-request-id"] != "invalid-value"
    assert len(rejected.headers["x-request-id"]) == 32


def test_nginx_api_access_log_uses_json_escaping_and_uri_only():
    template = (Path(__file__).parents[1] / "frontend" / "nginx.conf.template").read_text(encoding="utf-8")

    assert "log_format aiops_api escape=json" in template
    assert "$uri" in template
    assert "$request_uri" not in template
    assert "proxy_hide_header X-Request-ID;" in template
    assert "add_header X-Request-ID $request_id always;" in template


def test_api_startup_entries_disable_uvicorn_query_string_access_logs():
    project_root = Path(__file__).parents[1]
    compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
    kubernetes = (project_root / "deploy/kubernetes/base/backend.yaml").read_text(encoding="utf-8")
    start_local = (project_root / "scripts/start_local.sh").read_text(encoding="utf-8")
    e2e = (project_root / "scripts/ci/test_e2e.sh").read_text(encoding="utf-8")
    watchdog = (project_root / "scripts/ops/runtime_watchdog.py").read_text(encoding="utf-8")
    readme = (project_root / "README.md").read_text(encoding="utf-8")

    for source in (compose, kubernetes, start_local, e2e, watchdog, readme):
        assert "uvicorn" in source
        assert "--no-access-log" in source


def test_rejected_authentication_still_exposes_only_auth_timing():
    response = TestClient(app).get(
        "/api/auth/me",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    timing = response.headers["server-timing"]
    assert "auth;dur=" in timing
    assert "invalid-token" not in timing
    assert response.headers["x-request-id"]


def test_workspace_server_timing_contains_auth_and_service_segments(monkeypatch):
    class FakeReadService:
        def query_workspace(self, task_id, *, include_trend=True):
            return {"query": {"brand": "atlas", "queryText": "帽子"}, "trend": [], "events": []}

    def override_get_user(request: Request):
        request.state.auth_duration_ms = 1.25
        return _user()

    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = override_get_user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/queries/42/workspace?includeTrend=false")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    timing = response.headers["server-timing"]
    assert "app;dur=" in timing
    assert "auth;dur=1.2" in timing
    assert "workspace;dur=" in timing
    assert response.headers["x-request-id"]


def test_workspace_error_keeps_low_sensitivity_timing_headers(monkeypatch):
    class FakeReadService:
        def query_workspace(self, task_id, *, include_trend=True):
            raise QueryGovernanceNotFoundError("not found")

    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: _user()
    try:
        response = TestClient(app).get("/api/dashboard/top-query/queries/42/workspace")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404
    assert response.headers["x-request-id"]
    assert "workspace;dur=" in response.headers["server-timing"]
    assert "42" not in response.headers["server-timing"]


def test_middleware_logs_slow_requests_without_query_or_token(monkeypatch):
    warnings = []
    monkeypatch.setattr(app_module.request_logger, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(app_module, "SLOW_REQUEST_THRESHOLD_MS", 0.0)

    response = TestClient(app).get("/api/health/live?token=must-not-be-logged")

    assert response.status_code == 200
    assert warnings
    message_args = warnings[-1]
    assert message_args[0].startswith("slow api request")
    assert response.headers["x-request-id"] in message_args
    assert "/api/health/live" in message_args
    assert "token=must-not-be-logged" not in message_args


def test_middleware_slow_request_log_uses_route_template(monkeypatch):
    class FakeReadService:
        def query_workspace(self, task_id, *, include_trend=True):
            return {"query": {"brand": "atlas", "queryText": "帽子"}, "trend": [], "events": []}

    warnings = []
    monkeypatch.setattr(app_module.request_logger, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(app_module, "SLOW_REQUEST_THRESHOLD_MS", 0.0)
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: _user()
    try:
        response = TestClient(app).get("/api/dashboard/top-query/queries/3283/workspace")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    message_args = warnings[-1]
    assert "/api/dashboard/top-query/queries/{task_id}/workspace" in message_args
    assert "3283" not in message_args


def test_middleware_logs_uncaught_errors_without_exception_details(monkeypatch):
    class FakeReadService:
        def query_workspace(self, task_id, *, include_trend=True):
            raise RuntimeError("query text should not be logged")

    warnings = []
    monkeypatch.setattr(app_module.request_logger, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: _user()
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/api/dashboard/top-query/queries/42/workspace?query=secret"
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 500
    assert warnings
    message_args = warnings[-1]
    assert message_args[0].startswith("api request failed")
    assert len(message_args[1]) == 32
    assert "/api/dashboard/top-query/queries/{task_id}/workspace" in message_args
    assert "42" not in message_args
    assert "secret" not in message_args
    assert "query text should not be logged" not in message_args
