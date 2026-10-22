import json
import os
import re
from pathlib import Path


BUILD_INFO_PATH = Path("/app/release-build-info.json")
_BUILD_INFO_FIELDS = (
    "releaseSha",
    "schemaRelease",
    "artifactManifestSha256",
)
_ENVIRONMENT_FIELDS = {
    "RELEASE_SHA": "releaseSha",
    "SCHEMA_RELEASE": "schemaRelease",
    "ARTIFACT_MANIFEST_SHA256": "artifactManifestSha256",
}
_DEV_RELEASE_INFO = {
    "releaseSha": "dev",
    "schemaRelease": "unversioned",
    "artifactManifestSha256": "unversioned",
}


def _validate_build_info(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != set(_BUILD_INFO_FIELDS):
        raise RuntimeError("release build info is invalid")
    release_sha = payload.get("releaseSha")
    schema_release = payload.get("schemaRelease")
    artifact_manifest_sha = payload.get("artifactManifestSha256")
    if not isinstance(release_sha, str) or re.fullmatch(r"[0-9a-f]{40}", release_sha) is None:
        raise RuntimeError("release build info is invalid")
    if not isinstance(schema_release, str) or not schema_release.strip():
        raise RuntimeError("release build info is invalid")
    if not isinstance(artifact_manifest_sha, str) or re.fullmatch(
        r"[0-9a-f]{64}", artifact_manifest_sha
    ) is None:
        raise RuntimeError("release build info is invalid")
    return {field: payload[field] for field in _BUILD_INFO_FIELDS}


def _read_build_info() -> dict[str, str]:
    try:
        payload = json.loads(BUILD_INFO_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if os.getenv("ENV", "").strip().lower() == "prod":
            raise RuntimeError("release build info is missing") from None
        return dict(_DEV_RELEASE_INFO)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("release build info is invalid") from None
    return _validate_build_info(payload)


def get_release_info() -> dict[str, str]:
    release_info = _read_build_info()
    for environment_name, field_name in _ENVIRONMENT_FIELDS.items():
        if environment_name in os.environ and os.environ[environment_name] != release_info[field_name]:
            raise RuntimeError("release build identity does not match runtime environment")
    return release_info


def scheduler_config_fingerprint(release_info: dict[str, str]) -> str | None:
    """调度器运行记录的 config_fingerprint 受 sha256 CHECK 约束。

    非发布构建（本地 dev 的 artifactManifestSha256 为 "unversioned"）返回
    None，由 start_scheduler 回退到代码侧配置指纹，避免运行槽位创建失败。
    """
    value = release_info.get("artifactManifestSha256")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None
