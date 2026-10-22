"""Safe local project workspace operations for the PI Agent workbench."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class WorkspaceAccessError(ValueError):
    """Raised when a workspace path escapes the configured roots."""


def configured_workspace_roots() -> list[Path]:
    raw = os.getenv("PI_AGENT_WORKSPACE_ROOTS") or os.getenv("PI_AGENT_WORKDIR") or os.getcwd()
    roots: list[Path] = []
    for value in raw.split(os.pathsep):
        value = value.strip()
        if value:
            roots.append(Path(value).expanduser().resolve())
    return roots or [Path.cwd().resolve()]


def resolve_workspace_path(relative_path: str = "", *, roots: list[Path] | None = None) -> Path:
    allowed_roots = roots or configured_workspace_roots()
    normalized = str(relative_path or ".").strip() or "."
    candidate = Path(normalized).expanduser()
    if not candidate.is_absolute():
        candidate = allowed_roots[0] / candidate
    candidate = candidate.resolve()
    if not any(candidate == root or root in candidate.parents for root in allowed_roots):
        raise WorkspaceAccessError("workspace path is outside the authorized project roots")
    return candidate


def list_workspace(relative_path: str = "", *, roots: list[Path] | None = None) -> dict[str, Any]:
    allowed_roots = roots or configured_workspace_roots()
    directory = resolve_workspace_path(relative_path, roots=allowed_roots)
    if not directory.exists() or not directory.is_dir():
        raise FileNotFoundError("workspace directory not found")
    entries = []
    for item in sorted(directory.iterdir(), key=lambda value: (not value.is_dir(), value.name.lower())):
        if item.name.startswith(".") and item.name not in {".env.example"}:
            continue
        entries.append({
            "name": item.name,
            "path": str(item.relative_to(allowed_roots[0])) if item.is_relative_to(allowed_roots[0]) else item.name,
            "kind": "directory" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
            "modifiedAt": item.stat().st_mtime,
        })
    return {"path": str(directory), "relativePath": relative_path or ".", "entries": entries}


def preview_workspace_file(relative_path: str, *, max_bytes: int = 200_000, roots: list[Path] | None = None) -> dict[str, Any]:
    file_path = resolve_workspace_path(relative_path, roots=roots)
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError("workspace file not found")
    data = file_path.read_bytes()
    truncated = len(data) > max_bytes
    sample = data[:max_bytes]
    try:
        content = sample.decode("utf-8")
        is_binary = "\x00" in content
    except UnicodeDecodeError:
        content = ""
        is_binary = True
    return {
        "name": file_path.name,
        "path": relative_path,
        "size": len(data),
        "truncated": truncated,
        "isBinary": is_binary,
        "content": "" if is_binary else content,
    }


def save_workspace_file(relative_path: str, data: bytes, *, roots: list[Path] | None = None) -> dict[str, Any]:
    file_path = resolve_workspace_path(relative_path, roots=roots)
    if file_path.name in {"", ".", ".."}:
        raise WorkspaceAccessError("workspace file name is required")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(data)
    return {"path": relative_path, "name": file_path.name, "size": len(data)}
