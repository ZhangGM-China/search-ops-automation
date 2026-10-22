from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.assistant.workspace import WorkspaceAccessError, list_workspace, preview_workspace_file, resolve_workspace_path
from src.api import app, get_current_user
from src.api.application import assistant as assistant_application


def test_workspace_rejects_path_traversal(tmp_path: Path):
    with pytest.raises(WorkspaceAccessError):
        resolve_workspace_path("../outside", roots=[tmp_path])


def test_workspace_lists_files_and_previews_utf8_content(tmp_path: Path):
    (tmp_path / "README.md").write_text("# PI Agent\n", encoding="utf-8")
    listing = list_workspace(".", roots=[tmp_path])
    assert any(item["name"] == "README.md" for item in listing["entries"])
    preview = preview_workspace_file("README.md", max_bytes=100, roots=[tmp_path])
    assert preview["content"] == "# PI Agent\n"
    assert preview["isBinary"] is False


def test_workspace_marks_binary_file_without_leaking_bytes(tmp_path: Path):
    (tmp_path / "image.bin").write_bytes(b"\x00\xff\x01")
    preview = preview_workspace_file("image.bin", roots=[tmp_path])
    assert preview["isBinary"] is True
    assert preview["content"] == ""


def test_workspace_api_requires_user_and_returns_listing(monkeypatch):
    monkeypatch.setattr(assistant_application, "list_workspace", lambda path: {
        "path": "/tmp/project", "relativePath": path, "entries": [],
    })
    app.dependency_overrides[get_current_user] = lambda: type("UserStub", (), {"id": 1})()
    try:
        response = TestClient(app).get("/api/assistant/workspace?path=src")
        assert response.status_code == 200
        assert response.json()["relativePath"] == "src"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_workspace_api_maps_unsafe_preview_to_forbidden(monkeypatch):
    monkeypatch.setattr(assistant_application, "preview_workspace_file", lambda path: (_ for _ in ()).throw(WorkspaceAccessError("outside")))
    app.dependency_overrides[get_current_user] = lambda: type("UserStub", (), {"id": 1})()
    try:
        response = TestClient(app).get("/api/assistant/workspace/file?path=../secret")
        assert response.status_code == 403
        assert response.json()["detail"] == "outside"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_workspace_api_uploads_multipart_file(monkeypatch):
    monkeypatch.setattr(assistant_application, "save_workspace_file", lambda path, data: {
        "path": path, "name": "notes.txt", "size": len(data),
    })
    app.dependency_overrides[get_current_user] = lambda: type("UserStub", (), {"id": 1})()
    try:
        response = TestClient(app).post(
            "/api/assistant/workspace/file?path=notes.txt",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 200
        assert response.json() == {"path": "notes.txt", "name": "notes.txt", "size": 5}
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_terminal_status_api_enforces_owner(monkeypatch):
    monkeypatch.setattr(assistant_application._terminal_manager, "get_session", lambda session_id, owner_id: (_ for _ in ()).throw(KeyError("missing")))
    app.dependency_overrides[get_current_user] = lambda: type("UserStub", (), {"id": 1})()
    try:
        response = TestClient(app).get("/api/assistant/terminal/sessions/terminal-1")
        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
