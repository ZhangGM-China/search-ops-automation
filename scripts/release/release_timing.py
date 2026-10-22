"""Safe, append-only stage timing evidence for the TKE release chain."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any


STAGE_TIMING_FILENAME = "tke-stage-timings.json"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
STAGE = re.compile(r"[a-z0-9-]{1,80}\Z")
FAILURE_CATEGORIES = frozenset(
    {"connectivity", "preflight", "timeout", "execution", "unknown"}
)


def classify_failure(error: BaseException) -> str:
    message = str(error).lower()
    if any(token in message for token in ("connection", "connect", "reset", "unavailable")):
        return "connectivity"
    if "timeout" in message or "deadline exceeded" in message:
        return "timeout"
    if any(token in message for token in ("invalid", "missing", "preflight", "config.yaml")):
        return "preflight"
    if "failed" in message or "error" in message:
        return "execution"
    return "unknown"


def _state_path(state_dir: Path) -> Path:
    directory = Path(state_dir)
    if not directory.is_absolute() or directory.is_symlink():
        raise ValueError("state directory is invalid")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    node = directory.lstat()
    if not stat.S_ISDIR(node.st_mode) or node.st_uid != os.geteuid() or node.st_mode & 0o022:
        raise ValueError("state directory is invalid")
    os.chmod(directory, 0o700)
    return directory / STAGE_TIMING_FILENAME


def record_stage(
    *,
    state_dir: Path,
    stage: str,
    release_sha: str | None,
    status: str,
    started_at: str,
    finished_at: str,
    elapsed_seconds: float,
    failure_category: str | None = None,
) -> dict[str, Any]:
    if STAGE.fullmatch(stage) is None:
        raise ValueError("stage is invalid")
    if release_sha is not None and SHA40.fullmatch(release_sha) is None:
        raise ValueError("release SHA is invalid")
    if status not in {"success", "failed"}:
        raise ValueError("stage status is invalid")
    if failure_category is not None and failure_category not in FAILURE_CATEGORIES:
        raise ValueError("failure category is invalid")
    record: dict[str, Any] = {
        "stage": stage,
        "releaseSha": release_sha,
        "status": status,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "elapsedSeconds": round(max(0.0, float(elapsed_seconds)), 3),
    }
    if failure_category is not None:
        record["failureCategory"] = failure_category

    path = _state_path(Path(state_dir))
    payload: dict[str, Any] = {"version": 1, "stages": []}
    if path.exists() and not path.is_symlink():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("stage timing state is invalid") from exc
        if not isinstance(loaded, dict) or loaded.get("version") != 1 or not isinstance(loaded.get("stages"), list):
            raise ValueError("stage timing state is invalid")
        payload = loaded
    payload["stages"].append(record)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tke-stage-timings.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return record
