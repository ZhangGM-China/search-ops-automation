"""Fast, read-only checks that run before expensive release gates."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import yaml


SHA40 = re.compile(r"[0-9a-f]{40}\Z")
MAX_CONFIG_BYTES = 4 * 1024 * 1024
FRONTEND_TOOLS = ("vitest", "eslint", "playwright")


class ReleasePreflightError(RuntimeError):
    """Raised when a release can be rejected before expensive work starts."""


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def _leaf_count(value: object) -> int:
    if isinstance(value, dict):
        return sum(_leaf_count(child) for child in value.values())
    if isinstance(value, list):
        return 1
    return 1


def _read_config_template(repo: Path) -> tuple[int, int]:
    path = repo / "config.yaml"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise ReleasePreflightError("config.yaml is unavailable")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ReleasePreflightError("config.yaml is invalid") from exc
    if not isinstance(loaded, dict):
        raise ReleasePreflightError("config.yaml root must be a mapping")
    return len(loaded), _leaf_count(loaded)


def check_local(*, repo: Path, release_sha: str) -> dict[str, Any]:
    """Check local release prerequisites without exposing config values."""
    root = Path(repo)
    if SHA40.fullmatch(release_sha) is None:
        raise ReleasePreflightError("release SHA is invalid")
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ReleasePreflightError("release repository is invalid")

    top_level_count, leaf_count = _read_config_template(root)
    missing_tools = [
        name
        for name in FRONTEND_TOOLS
        if not (root / "frontend" / "node_modules" / ".bin" / name).is_file()
    ]
    if missing_tools:
        raise ReleasePreflightError(
            "frontend dependencies are missing: " + ",".join(missing_tools)
        )
    if shutil.which("docker") is None:
        raise ReleasePreflightError("docker command is unavailable")
    return {
        "configLeafFieldCount": leaf_count,
        "configTopLevelFieldCount": top_level_count,
        "frontendDependencies": "present",
        "releaseSha": release_sha,
        "status": "local_preflight_passed",
    }


def check_docker(*, runner: Runner | None = None) -> dict[str, str]:
    """Confirm the Docker daemon responds, without printing its details."""
    command = ["docker", "version", "--format", "{{.Server.Version}}"]
    try:
        result = (runner or subprocess.run)(
            command,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleasePreflightError("docker daemon is unavailable") from exc
    if result.returncode != 0:
        raise ReleasePreflightError("docker daemon is unavailable")
    return {"docker": "ready"}
