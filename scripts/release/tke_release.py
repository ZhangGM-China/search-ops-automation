"""Fail-closed TKE release primitives.

This module intentionally keeps all Kubernetes mutations behind an explicit
``execute`` + exact release SHA confirmation.  It is used by ``release.py``;
calling it directly does not bypass the repository release entrypoint.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml


SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_DIGEST = re.compile(
    r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?(?:/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)*"
    r"(?:/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)?@sha256:[0-9a-f]{64}\Z"
)
NAMESPACE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_RENDERED_BYTES = 4 * 1024 * 1024
ROLL_OUT_TIMEOUT_SECONDS = 10 * 60
PULL_REHEARSAL_TIMEOUT_SECONDS = 5 * 60
MYSQL_REHEARSAL_TIMEOUT_SECONDS = 5 * 60
CFS_RUNTIME_INIT_TIMEOUT_SECONDS = 5 * 60
RUNTIME_CONFIG_SYNC_TIMEOUT_SECONDS = 60
TARGET_CLUSTER_ID = "cls-demo0000"
MAX_STATE_BYTES = 64 * 1024
TKE_STATE_VERSION = 2
TKE_STATE_FILENAME = "tke-release-state.json"
KUBE_CONTEXT = re.compile(r"[^\x00\r\n\t ]{1,253}\Z")
RUNTIME_CONFIGMAP_NAME = "aiops-runtime-config"
RELEASE_CONFIGMAP_NAME = "aiops-release-info"
RUNTIME_SECRET_NAME = "aiops-runtime-secrets"
RUNTIME_PVC_NAME = "aiops-runtime-data"
RUNTIME_CONFIG_KEY = "config.yaml"
RUNTIME_SERVICEACCOUNT_NAME = "aiops-runtime"
CFS_RUNTIME_INIT_JOB_NAME = "aiops-cfs-runtime-init"
CFS_RUNTIME_DATA_PATH = "/app/data/runtime"
REQUIRED_SECRET_KEYS = frozenset({
    "KG_AGENT_MCP_TOKEN",
    "PI_AGENT_INTERNAL_TOKEN",
    "AUTO_DB_URI_PROD",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "SECRET_KEY",
})
DATABASE_SECRET_KEYS = frozenset({"AUTO_DB_URI_PROD"})
BLUE_GREEN_SLOTS = frozenset({"blue", "green"})
SLOT_LABEL = "aiops.example.test/slot"
MYSQL_REHEARSAL_FAILURE_CODES = frozenset({"imports", "database", "dialect", "read_only_query"})
CFS_RUNTIME_INIT_FAILURE_CODES = frozenset({"directory", "ownership", "mode"})

CFS_RUNTIME_INIT_SCRIPT = """
import json
import os
import stat
import sys

target = "/app/data/runtime"


def fail(code):
    print(json.dumps({"failureCode": code, "status": "runtime_data_init_failed"}, sort_keys=True))
    raise SystemExit(1)


try:
    try:
        os.mkdir(target, 0o770)
    except FileExistsError:
        pass
    node = os.lstat(target)
except OSError:
    fail("directory")
if not stat.S_ISDIR(node.st_mode):
    fail("directory")
try:
    os.chown(target, 10001, 10001, follow_symlinks=False)
except OSError:
    fail("ownership")
try:
    os.chmod(target, 0o770, follow_symlinks=False)
except OSError:
    fail("mode")
try:
    node = os.lstat(target)
except OSError:
    fail("directory")
if not stat.S_ISDIR(node.st_mode):
    fail("directory")
if node.st_uid != 10001 or node.st_gid != 10001:
    fail("ownership")
if stat.S_IMODE(node.st_mode) != 0o770:
    fail("mode")
""".strip()

CFS_RUNTIME_VERIFY_SCRIPT = """
import json
import os
import secrets
import stat
import sys

target = "/app/data/runtime"


def fail(code):
    print(json.dumps({"failureCode": code, "status": "runtime_data_non_root_write_failed"}, sort_keys=True))
    raise SystemExit(1)


try:
    node = os.lstat(target)
except OSError:
    fail("directory")
if not stat.S_ISDIR(node.st_mode):
    fail("directory")
if os.geteuid() != 10001 or os.getegid() != 10001:
    fail("identity")
if node.st_uid != 10001 or node.st_gid != 10001:
    fail("ownership")
if stat.S_IMODE(node.st_mode) != 0o770:
    fail("mode")
probe = os.path.join(target, ".aiops-cfs-write-probe-" + secrets.token_hex(16))
descriptor = None
created = False
try:
    descriptor = os.open(
        probe,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    created = True
    os.write(descriptor, b"ok")
    os.fsync(descriptor)
    os.close(descriptor)
    descriptor = None
    os.unlink(probe)
    created = False
except OSError:
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if created:
        try:
            os.unlink(probe)
        except OSError:
            pass
    fail("non_root_write")
print(
    json.dumps(
        {
            "gid": os.getegid(),
            "mode": "0770",
            "status": "runtime_data_non_root_write_verified",
            "uid": os.geteuid(),
        },
        sort_keys=True,
    )
)
""".strip()
MYSQL_REHEARSAL_SCRIPT = """import json


def main():
    engine = None
    stage = "imports"
    try:
        from sqlalchemy import event, text
        from src.persistence.session import engine as database_engine

        stage = "database"
        engine = database_engine
        stage = "dialect"
        if engine.dialect.name not in {"mysql", "mariadb"}:
            raise RuntimeError("MySQL rehearsal requires a MySQL-compatible database")

        @event.listens_for(engine, "connect")
        def _force_mysql_session_read_only(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
            finally:
                cursor.close()

        engine.dispose()
        stage = "read_only_query"
        with engine.connect() as connection:
            read_only_query = connection.execute(text("SELECT 1")).scalar_one()
        if read_only_query != 1:
            raise RuntimeError("read-only query returned an unexpected value")
        print(json.dumps({
            "readOnlyQuery": True,
            "status": "mysql_rehearsal_probe",
        }, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print(json.dumps({
            "failureCodes": [stage],
            "status": "mysql_rehearsal_probe_failed",
        }, sort_keys=True, separators=(",", ":")))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


raise SystemExit(main())
"""




class TkeReleaseError(RuntimeError):
    """Raised when a TKE release cannot safely proceed."""


class CommandResult:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes):
        self.returncode = int(returncode)
        self.stdout = bytes(stdout)
        self.stderr = bytes(stderr)


Runner = Callable[..., CommandResult]


def _validate_kube_context(value: str) -> str:
    if not isinstance(value, str) or KUBE_CONTEXT.fullmatch(value) is None:
        raise TkeReleaseError("kube context is invalid")
    return value


def _kubectl(kube_context: str, *args: str) -> list[str]:
    """Build an API-accessing kubectl command with an explicit context."""
    return ["kubectl", "--context", _validate_kube_context(kube_context), *args]


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _safe_error_category(stderr: bytes) -> str:
    value = stderr[:64 * 1024].decode("utf-8", errors="ignore").lower()
    if "forbidden" in value or "unauthorized" in value:
        return "kubernetes_access_denied"
    if "timeout" in value or "deadline exceeded" in value:
        return "kubernetes_timeout"
    if "no such host" in value or "connection" in value or "eof" in value:
        return "kubernetes_connection_failed"
    return "kubernetes_command_failed"


def _run_subprocess(
    argv: Iterable[str], *, input_bytes: bytes | None = None, timeout_seconds: float | None = None
) -> CommandResult:
    try:
        result = subprocess.run(
            [str(item) for item in argv],
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TkeReleaseError("kubernetes_timeout") from exc
    except OSError as exc:
        raise TkeReleaseError("kubernetes_command_unavailable") from exc
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _execute(
    runner: Runner | None,
    argv: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult:
    if runner is None:
        return _run_subprocess(argv, input_bytes=input_bytes, timeout_seconds=timeout_seconds)
    try:
        result = runner(argv, input_bytes=input_bytes, timeout_seconds=timeout_seconds)
    except TypeError:
        result = runner(argv)
    if not isinstance(result, CommandResult):
        raise TkeReleaseError("kubernetes_runner_result_invalid")
    return result


def _require_release_values(
    *,
    namespace: str,
    release_sha: str,
    backend_image: str,
    frontend_image: str,
    schema_release: str,
    artifact_manifest_sha256: str,
) -> None:
    if NAMESPACE.fullmatch(namespace) is None:
        raise TkeReleaseError("namespace is invalid")
    if SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("release SHA is invalid")
    if IMAGE_DIGEST.fullmatch(backend_image) is None:
        raise TkeReleaseError("backend image reference is invalid")
    if IMAGE_DIGEST.fullmatch(frontend_image) is None:
        raise TkeReleaseError("frontend image reference is invalid")
    if not schema_release or any(character in schema_release for character in "\r\n\t\0"):
        raise TkeReleaseError("schema release is invalid")
    if SHA64.fullmatch(artifact_manifest_sha256) is None:
        raise TkeReleaseError("artifact manifest SHA is invalid")


def _validate_manifests_dir(manifests_dir: Path) -> Path:
    path = Path(manifests_dir)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise TkeReleaseError("manifests directory is invalid")
    if not (path / "kustomization.yaml").is_file():
        raise TkeReleaseError("Kustomization is missing")
    return path


def _canonical_state_path(path: Path) -> Path:
    value = Path(path)
    if not value.is_absolute() or value.is_symlink() or value.name != TKE_STATE_FILENAME:
        raise TkeReleaseError("TKE release state path is invalid")
    if os.path.normpath(str(value)) != str(value):
        raise TkeReleaseError("TKE release state path is invalid")
    return value


def _initial_release_state() -> dict[str, Any]:
    return {
        "active": None,
        "history": [],
        "version": TKE_STATE_VERSION,
    }


def _validate_release_record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "artifactManifestSha256",
        "backendImage",
        "frontendImage",
        "manifestSha256",
        "releaseSha",
        "schemaRelease",
        "slot",
    }:
        raise TkeReleaseError("TKE release state is invalid")
    _require_release_values(
        namespace="aiops-demo",
        release_sha=str(value["releaseSha"]),
        backend_image=str(value["backendImage"]),
        frontend_image=str(value["frontendImage"]),
        schema_release=str(value["schemaRelease"]),
        artifact_manifest_sha256=str(value["artifactManifestSha256"]),
    )
    if SHA64.fullmatch(str(value["manifestSha256"])) is None:
        raise TkeReleaseError("TKE release state is invalid")
    _validate_slot(str(value["slot"]))
    return dict(value)


def _validate_release_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"active", "history", "version"}:
        raise TkeReleaseError("TKE release state is invalid")
    if value.get("version") != TKE_STATE_VERSION or not isinstance(value.get("history"), list):
        raise TkeReleaseError("TKE release state is invalid")
    active = value.get("active")
    if active is not None:
        _validate_release_record(active)
    history = value["history"]
    if len(history) > 32:
        raise TkeReleaseError("TKE release state is invalid")
    for item in history:
        _validate_release_record(item)
    return {
        "active": None if active is None else _validate_release_record(active),
        "history": [_validate_release_record(item) for item in history],
        "version": TKE_STATE_VERSION,
    }


@contextmanager
def _locked_release_state(path: Path):
    state_path = _canonical_state_path(path)
    directory = state_path.parent
    if directory.is_symlink():
        raise TkeReleaseError("TKE release state directory is invalid")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    node = directory.lstat()
    if (
        not stat.S_ISDIR(node.st_mode)
        or node.st_uid != os.geteuid()
        or node.st_mode & 0o022
    ):
        raise TkeReleaseError("TKE release state directory is invalid")
    os.chmod(directory, 0o700)
    lock_path = directory / ".tke-release-state.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise TkeReleaseError("TKE release state is unavailable") from exc
    try:
        node = os.fstat(fd)
        if (
            not stat.S_ISREG(node.st_mode)
            or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) != 0o600
        ):
            raise TkeReleaseError("TKE release state lock is invalid")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield state_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_release_state(path: Path) -> dict[str, Any]:
    state_path = _canonical_state_path(path)
    if not state_path.exists():
        return _initial_release_state()
    if state_path.is_symlink():
        raise TkeReleaseError("TKE release state is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(state_path, flags)
    except OSError as exc:
        raise TkeReleaseError("TKE release state is unavailable") from exc
    try:
        node = os.fstat(fd)
        if (
            not stat.S_ISREG(node.st_mode)
            or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) != 0o600
            or node.st_size <= 0
            or node.st_size > MAX_STATE_BYTES
        ):
            raise TkeReleaseError("TKE release state is invalid")
        raw = os.read(fd, MAX_STATE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeReleaseError("TKE release state is invalid") from exc
    if raw != _canonical_json(value):
        raise TkeReleaseError("TKE release state is invalid")
    return _validate_release_state(value)


def _write_release_state(path: Path, state: dict[str, Any]) -> None:
    state_path = _canonical_state_path(path)
    payload = _canonical_json(_validate_release_state(state))
    temporary = state_path.parent / (
        ".tke-release-state."
        + str(os.getpid())
        + "."
        + secrets.token_hex(8)
        + ".tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(temporary, state_path)
        os.chmod(state_path, 0o600)
    except OSError as exc:
        raise TkeReleaseError("TKE release state is unavailable") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _record_active_release(*, state_path: Path, record: dict[str, Any]) -> None:
    with _locked_release_state(state_path) as locked_path:
        current = _read_release_state(locked_path)
        active = current["active"]
        history = list(current["history"])
        if active is not None and active["releaseSha"] != record["releaseSha"]:
            history.append(active)
        _write_release_state(
            locked_path,
            {
                "active": _validate_release_record(record),
                "history": history[-32:],
                "version": TKE_STATE_VERSION,
            },
        )


def _recorded_rollback_target(*, state_path: Path, release_sha: str) -> dict[str, Any]:
    with _locked_release_state(state_path) as locked_path:
        state = _read_release_state(locked_path)
    active = state["active"]
    if active is None or active["releaseSha"] != release_sha:
        raise TkeReleaseError("recorded active TKE release target is missing")
    candidates = [item for item in state["history"] if item["releaseSha"] != release_sha]
    if not candidates:
        raise TkeReleaseError("recorded previous TKE release target is missing")
    return candidates[-1]


def candidate_slot(*, state_path: Path) -> str:
    """Return the only inactive slot without inferring from mutable cluster state."""
    with _locked_release_state(state_path) as locked_path:
        state = _read_release_state(locked_path)
    active = state["active"]
    if active is None:
        return "green"
    active_slot = _validate_slot(str(active["slot"]))
    return "blue" if active_slot == "green" else "green"


def require_candidate_slot_safe(
    *, state_path: Path, namespace: str, kube_context: str, runner: Runner | None = None
) -> str:
    """Refuse to deploy when local state cannot prove the live slot."""
    if NAMESPACE.fullmatch(namespace) is None:
        raise TkeReleaseError("TKE candidate slot namespace is invalid")
    _verify_target_context(kube_context=kube_context, runner=runner)
    active = _active_release_record(state_path=state_path)
    services = [
        _read_optional_named_resource(
            kube_context=kube_context,
            namespace=namespace,
            resource="service",
            name=name,
            action=f"candidate_active_{name}_readback",
            runner=runner,
        )
        for name in ("api", "frontend")
    ]
    ingress = _read_optional_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="ingress",
        name="aiops",
        action="candidate_active_ingress_readback",
        runner=runner,
    )
    if active is None:
        if any(value is not None for value in services) or ingress is not None:
            raise TkeReleaseError("active release state is missing while public entrypoints exist")
        return "green"

    active_slot = _validate_slot(str(active["slot"]))
    for name, value in zip(("api", "frontend"), services):
        if value is None:
            raise TkeReleaseError(f"stable public Service {name} is missing while active release is recorded")
        selector = value.get("spec", {}).get("selector") if isinstance(value.get("spec"), dict) else None
        if not isinstance(selector, dict) or selector.get(SLOT_LABEL) != active_slot:
            raise TkeReleaseError("recorded active slot does not match stable public Services")
    if ingress is None or _ingress_backend_service_names(ingress) != {"frontend"}:
        raise TkeReleaseError("recorded active release does not match the public Ingress")
    return "blue" if active_slot == "green" else "green"


def import_active_release_state(
    *,
    source_state_path: Path,
    target_state_path: Path,
    expected_active_release_sha: str,
    execute: bool,
    confirm_release_sha: str | None,
    namespace: str,
    kube_context: str,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Adopt an already active release into a fresh local state directory.

    This is a read-and-record recovery step only.  It requires the source
    record, stable Service selectors, active-slot acceptance, and image
    identities to agree before writing the target state.  It never applies or
    deletes Kubernetes resources.
    """
    if SHA40.fullmatch(expected_active_release_sha) is None:
        raise TkeReleaseError("active release adoption SHA is invalid")
    _require_execute(execute, confirm_release_sha, expected_active_release_sha)
    source = _canonical_state_path(source_state_path)
    target = _canonical_state_path(target_state_path)
    if source == target:
        raise TkeReleaseError("active release adoption state paths must differ")
    if target.exists():
        raise TkeReleaseError("active release adoption target state already exists")
    active = _active_release_record(state_path=source)
    if active is None or active["releaseSha"] != expected_active_release_sha:
        raise TkeReleaseError("active release adoption source record is missing or mismatched")
    slot = require_candidate_slot_safe(
        state_path=source,
        namespace=namespace,
        kube_context=kube_context,
        runner=runner,
    )
    active_slot = "blue" if slot == "green" else "green"
    acceptance = accept_slot_deploy(
        namespace=namespace,
        release_sha=active["releaseSha"],
        kube_context=kube_context,
        slot=active_slot,
        expected_scheduler_mode="active",
        include_public_entrypoints=True,
        runner=runner,
    )
    if acceptance.get("status") != "accepted" or acceptance.get("slot") != active_slot:
        raise TkeReleaseError("active release adoption acceptance failed")
    images = acceptance.get("images")
    if not isinstance(images, dict) or (
        images.get("backend") != active["backendImage"]
        or images.get("frontend") != active["frontendImage"]
    ):
        raise TkeReleaseError("active release adoption image identity mismatched")
    _record_active_release(state_path=target, record=active)
    return {
        "status": "active_release_state_imported",
        "activeReleaseSha": active["releaseSha"],
        "slot": active_slot,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
    }


def adopt_live_active_release_state(
    *,
    manifests_dir: Path,
    target_state_path: Path,
    expected_active_release_sha: str,
    execute: bool,
    confirm_release_sha: str | None,
    namespace: str,
    kube_context: str,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Reconstruct local active state from a live, already-managed TKE slot.

    This is a read-only cluster inspection plus a local state write.  It is
    intentionally separate from ``import_active_release_state``: adoption is
    allowed only when the stable Services, Ingress, slot release ConfigMap,
    workload annotations and full active acceptance all agree.  It never
    applies or deletes Kubernetes resources.
    """
    if SHA40.fullmatch(expected_active_release_sha) is None:
        raise TkeReleaseError("live active release adoption SHA is invalid")
    _require_execute(execute, confirm_release_sha, expected_active_release_sha)
    target = _canonical_state_path(target_state_path)
    if target.exists():
        raise TkeReleaseError("live active release adoption target state already exists")
    if NAMESPACE.fullmatch(namespace) is None:
        raise TkeReleaseError("live active release adoption namespace is invalid")
    _verify_target_context(kube_context=kube_context, runner=runner)

    services = {
        name: _read_named_resource(
            kube_context=kube_context,
            namespace=namespace,
            resource="service",
            name=name,
            action=f"live_active_{name}_readback",
            runner=runner,
        )
        for name in ("api", "frontend")
    }
    slots: set[str] = set()
    expected_service_components = {"api": "backend", "frontend": "frontend"}
    for name, service in services.items():
        spec = service.get("spec")
        selector = spec.get("selector") if isinstance(spec, dict) else None
        slot = selector.get(SLOT_LABEL) if isinstance(selector, dict) else None
        if (
            not isinstance(selector, dict)
            or selector.get("app.kubernetes.io/name") != expected_service_components[name]
            or not isinstance(slot, str)
            or slot not in BLUE_GREEN_SLOTS
        ):
            raise TkeReleaseError("live active release adoption Service slot is invalid")
        slots.add(slot)
    if len(slots) != 1:
        raise TkeReleaseError("live active release adoption Services disagree on slot")
    slot = next(iter(slots))

    ingress = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="ingress",
        name="aiops",
        action="live_active_ingress_readback",
        runner=runner,
    )
    if _ingress_backend_service_names(ingress) != {"frontend"}:
        raise TkeReleaseError("live active release adoption Ingress is invalid")

    release_info = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="configmap",
        name=f"{RELEASE_CONFIGMAP_NAME}-{slot}",
        action="live_active_release_configmap_readback",
        runner=runner,
    )
    data = release_info.get("data")
    metadata = release_info.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != f"{RELEASE_CONFIGMAP_NAME}-{slot}"
        or not isinstance(labels, dict)
        or labels.get(SLOT_LABEL) != slot
        or not isinstance(data, dict)
    ):
        raise TkeReleaseError("live active release adoption release ConfigMap is invalid")
    release_sha = data.get("RELEASE_SHA")
    schema_release = data.get("SCHEMA_RELEASE")
    artifact_manifest_sha256 = data.get("ARTIFACT_MANIFEST_SHA256")
    if (
        release_sha != expected_active_release_sha
        or not isinstance(schema_release, str)
        or not isinstance(artifact_manifest_sha256, str)
        or SHA64.fullmatch(artifact_manifest_sha256) is None
    ):
        raise TkeReleaseError("live active release adoption release identity mismatched")

    for component in ("backend", "frontend", "scheduler"):
        deployment = _read_named_resource(
            kube_context=kube_context,
            namespace=namespace,
            resource="deployment",
            name=_slot_name(component, slot),
            action=f"live_active_{component}_readback",
            runner=runner,
        )
        metadata = deployment.get("metadata")
        annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
        if (
            not isinstance(annotations, dict)
            or annotations.get("aiops.example.test/release-sha") != release_sha
            or annotations.get("aiops.example.test/artifact-manifest-sha256")
            != artifact_manifest_sha256
        ):
            raise TkeReleaseError("live active release adoption workload identity mismatched")

    acceptance = accept_slot_deploy(
        namespace=namespace,
        release_sha=release_sha,
        kube_context=kube_context,
        slot=slot,
        expected_scheduler_mode="active",
        include_public_entrypoints=True,
        runner=runner,
    )
    if acceptance.get("status") != "accepted" or acceptance.get("slot") != slot:
        raise TkeReleaseError("live active release adoption acceptance failed")
    images = acceptance.get("images")
    if not isinstance(images, dict):
        raise TkeReleaseError("live active release adoption image identity is invalid")
    backend_image = images.get("backend")
    frontend_image = images.get("frontend")
    if not isinstance(backend_image, str) or not isinstance(frontend_image, str):
        raise TkeReleaseError("live active release adoption image identity is invalid")

    rendered = render_slot_manifests(
        manifests_dir=manifests_dir,
        namespace=namespace,
        release_sha=release_sha,
        backend_image=backend_image,
        frontend_image=frontend_image,
        schema_release=schema_release,
        artifact_manifest_sha256=artifact_manifest_sha256,
        slot=slot,
        scheduler_mode="active",
        include_public_entrypoints=True,
        runner=runner,
    )
    record = {
        "artifactManifestSha256": artifact_manifest_sha256,
        "backendImage": backend_image,
        "frontendImage": frontend_image,
        "manifestSha256": _manifest_sha256(rendered),
        "releaseSha": release_sha,
        "schemaRelease": schema_release,
        "slot": slot,
    }
    _record_active_release(state_path=target, record=record)
    return {
        "status": "active_release_state_adopted",
        "activeReleaseSha": release_sha,
        "slot": slot,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "manifestSha256": record["manifestSha256"],
    }


def _replace_required_scalar(value: object, expected: str, replacement: str) -> str:
    if not isinstance(value, str) or value != expected:
        raise TkeReleaseError("base manifest identity placeholder is invalid")
    return replacement


def _validate_slot(slot: str) -> str:
    if slot not in BLUE_GREEN_SLOTS:
        raise TkeReleaseError("TKE blue-green slot is invalid")
    return slot


def _slot_name(component: str, slot: str) -> str:
    _validate_slot(slot)
    if component not in {"api", "backend", "frontend", "scheduler"}:
        raise TkeReleaseError("TKE blue-green component is invalid")
    return f"{component}-{slot}"


def _replace_release_config_reference(value: object, *, release_config_name: str) -> None:
    """Bind only release-identity ConfigMap references to one blue-green slot."""
    if isinstance(value, list):
        for item in value:
            _replace_release_config_reference(item, release_config_name=release_config_name)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key in {"configMapRef", "configMapKeyRef"} and isinstance(item, dict):
            if item.get("name") == RELEASE_CONFIGMAP_NAME:
                item["name"] = release_config_name
        _replace_release_config_reference(item, release_config_name=release_config_name)


def _set_scheduler_mode(deployment: dict[str, Any], scheduler_mode: str) -> None:
    if scheduler_mode not in {"active", "standby"}:
        raise TkeReleaseError("TKE Scheduler mode is invalid")
    try:
        containers = deployment["spec"]["template"]["spec"]["containers"]
        container = next(
            item
            for item in containers
            if isinstance(item, dict) and item.get("name") == "scheduler"
        )
        environment = container["env"]
    except (KeyError, TypeError, StopIteration) as exc:
        raise TkeReleaseError("Scheduler manifest is invalid") from exc
    found_mode = False
    found_expected_mode = False
    for item in environment:
        if not isinstance(item, dict):
            raise TkeReleaseError("Scheduler manifest is invalid")
        if item.get("name") == "AIOPS_SCHEDULER_MODE":
            item["value"] = scheduler_mode
            found_mode = True
        if item.get("name") == "SCHEDULER_EXPECTED_MODE":
            item["value"] = scheduler_mode
            found_expected_mode = True
    if not found_mode or not found_expected_mode:
        raise TkeReleaseError("Scheduler manifest is invalid")


def _set_frontend_api_upstream(deployment: dict[str, Any], *, upstream: str) -> None:
    try:
        containers = deployment["spec"]["template"]["spec"]["containers"]
        container = next(
            item
            for item in containers
            if isinstance(item, dict) and item.get("name") == "frontend"
        )
        environment = container["env"]
    except (KeyError, TypeError, StopIteration) as exc:
        raise TkeReleaseError("Frontend manifest is invalid") from exc
    for item in environment:
        if isinstance(item, dict) and item.get("name") == "API_UPSTREAM":
            item["value"] = upstream
            item.pop("valueFrom", None)
            return
    environment.append({"name": "API_UPSTREAM", "value": upstream})


def render_release_manifests(
    *,
    manifests_dir: Path,
    release_sha: str,
    backend_image: str,
    frontend_image: str,
    schema_release: str,
    artifact_manifest_sha256: str,
    namespace: str = "aiops-demo",
    runner: Runner | None = None,
    include_bootstrap: bool = False,
) -> bytes:
    """Render Kustomize output after binding immutable release identity.

    The deployment path deliberately consumes ``kubectl kustomize`` output,
    rather than reimplementing Kustomize by loading source YAML files.  That
    preserves generated labels, selector transforms and future Kustomize
    transformations in the exact manifest that is sent to the API server.
    """
    _require_release_values(
        namespace=namespace,
        release_sha=release_sha,
        backend_image=backend_image,
        frontend_image=frontend_image,
        schema_release=schema_release,
        artifact_manifest_sha256=artifact_manifest_sha256,
    )
    root = _validate_manifests_dir(manifests_dir)
    rendered_base = _require_success(
        _execute(
            runner,
            ["kubectl", "kustomize", str(root)],
            timeout_seconds=60,
        ),
        "kustomize_render",
    )
    if not rendered_base or len(rendered_base) > MAX_RENDERED_BYTES:
        raise TkeReleaseError("Kustomize output is invalid")
    try:
        documents = [
            item
            for item in yaml.safe_load_all(rendered_base)
            if item is not None
        ]
    except (UnicodeError, yaml.YAMLError) as exc:
        raise TkeReleaseError("Kustomize output is invalid") from exc
    if not documents or any(not isinstance(item, dict) for item in documents):
        raise TkeReleaseError("Kustomize output is invalid")
    # The browser Worker is an independently managed deployment.  Kustomize's
    # global selector labels describe only the AIOPS application and must not
    # be added to selectors that target the Worker.
    for item in documents:
        if item.get("kind") != "NetworkPolicy":
            continue
        spec = item.get("spec") or {}
        selectors = [spec.get("podSelector") or {}]
        for direction, peer_key in (("egress", "to"), ("ingress", "from")):
            for rule in spec.get(direction, []):
                selectors.extend(peer.get("podSelector") or {} for peer in rule.get(peer_key, []))
        for selector in selectors:
            labels = selector.get("matchLabels") or {}
            if labels.get("app.kubernetes.io/name") == "qg-atlas-browser-worker":
                labels.pop("app.kubernetes.io/managed-by", None)
                labels["app.kubernetes.io/part-of"] = "aiops-image-sync"
    named = {
        (str(item.get("kind") or ""), str(item.get("metadata", {}).get("name") or "")): item
        for item in documents
    }
    for component, image in (("backend", backend_image), ("scheduler", backend_image), ("frontend", frontend_image)):
        deployment = named.get(("Deployment", component))
        try:
            container = deployment["spec"]["template"]["spec"]["containers"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise TkeReleaseError("deployment manifest is invalid") from exc
        container["image"] = _replace_required_scalar(
            container.get("image"),
            "registry.example.test/aiops/backend@sha256:" + "0" * 64
            if component in {"backend", "scheduler"}
            else "registry.example.test/aiops/frontend@sha256:" + "0" * 64,
            image,
        )
        for sidecar in deployment["spec"]["template"]["spec"]["containers"][1:]:
            if sidecar.get("name") == "pi-runtime":
                sidecar["image"] = _replace_required_scalar(
                    sidecar.get("image"),
                    "registry.example.test/aiops/backend@sha256:" + "0" * 64,
                    backend_image,
                )
        metadata = deployment.setdefault("metadata", {})
        annotations = metadata.setdefault("annotations", {})
        labels = deployment["spec"]["template"].setdefault("metadata", {}).setdefault("labels", {})
        annotations["aiops.example.test/release-sha"] = release_sha
        annotations["aiops.example.test/artifact-manifest-sha256"] = artifact_manifest_sha256
        labels["app.kubernetes.io/version"] = release_sha
    config = named.get(("ConfigMap", RELEASE_CONFIGMAP_NAME))
    if not isinstance(config, dict) or not isinstance(config.get("data"), dict):
        raise TkeReleaseError("release ConfigMap manifest is invalid")
    data = config["data"]
    for field, replacement in (
        ("RELEASE_SHA", release_sha),
        ("SCHEMA_RELEASE", schema_release),
        ("ARTIFACT_MANIFEST_SHA256", artifact_manifest_sha256),
    ):
        _replace_required_scalar(data.get(field), "", replacement)
        data[field] = replacement
    runtime_documents: list[dict[str, Any]] = []
    bootstrap_kinds = {"Namespace", "ServiceAccount", "Role", "RoleBinding"}
    for item in documents:
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            raise TkeReleaseError("Kubernetes manifest metadata is invalid")
        if metadata.get("namespace") == "aiops-demo":
            metadata["namespace"] = namespace
        if include_bootstrap or item.get("kind") not in bootstrap_kinds:
            runtime_documents.append(item)
    if not runtime_documents:
        raise TkeReleaseError("rendered Kubernetes manifest is invalid")
    rendered = yaml.safe_dump_all(
        runtime_documents, allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    if not rendered or len(rendered) > MAX_RENDERED_BYTES:
        raise TkeReleaseError("rendered Kubernetes manifest is invalid")
    return rendered


def render_candidate_manifests(
    *,
    manifests_dir: Path,
    release_sha: str,
    backend_image: str,
    frontend_image: str,
    schema_release: str,
    artifact_manifest_sha256: str,
    namespace: str = "aiops-demo",
    runner: Runner | None = None,
) -> bytes:
    """Render a no-customer-traffic candidate with its Scheduler in standby.

    The candidate still starts the API and Frontend for internal verification,
    but it must never enqueue production scheduler jobs before the handoff.
    Only the rendered object is changed; the base manifest remains the active
    release contract.
    """
    rendered = render_release_manifests(
        manifests_dir=manifests_dir,
        release_sha=release_sha,
        backend_image=backend_image,
        frontend_image=frontend_image,
        schema_release=schema_release,
        artifact_manifest_sha256=artifact_manifest_sha256,
        namespace=namespace,
        runner=runner,
    )
    try:
        documents = [item for item in yaml.safe_load_all(rendered) if item is not None]
    except yaml.YAMLError as exc:
        raise TkeReleaseError("candidate Kubernetes manifest is invalid") from exc
    candidate_documents = [
        item
        for item in documents
        if not (
            isinstance(item, dict)
            and item.get("kind") == "Ingress"
            and item.get("metadata", {}).get("name") == "aiops"
        )
    ]
    if len(candidate_documents) != len(documents) - 1:
        raise TkeReleaseError("candidate Kubernetes ingress contract is invalid")
    scheduler = next(
        (
            item
            for item in candidate_documents
            if isinstance(item, dict)
            and item.get("kind") == "Deployment"
            and item.get("metadata", {}).get("name") == "scheduler"
        ),
        None,
    )
    try:
        container = next(
            item
            for item in scheduler["spec"]["template"]["spec"]["containers"]
            if isinstance(item, dict) and item.get("name") == "scheduler"
        )
        environment = container["env"]
    except (KeyError, TypeError, StopIteration) as exc:
        raise TkeReleaseError("candidate Scheduler manifest is invalid") from exc
    found_mode = False
    found_expected_mode = False
    for item in environment:
        if not isinstance(item, dict):
            raise TkeReleaseError("candidate Scheduler manifest is invalid")
        if item.get("name") == "AIOPS_SCHEDULER_MODE":
            item["value"] = "standby"
            found_mode = True
        if item.get("name") == "SCHEDULER_EXPECTED_MODE":
            item["value"] = "standby"
            found_expected_mode = True
    if not found_mode or not found_expected_mode:
        raise TkeReleaseError("candidate Scheduler manifest is invalid")
    candidate = yaml.safe_dump_all(
        candidate_documents, allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    _validate_rendered(candidate)
    return candidate


def _validate_slot_rendered(
    rendered: bytes,
    *,
    slot: str,
    include_public_entrypoints: bool,
) -> None:
    try:
        documents = [item for item in yaml.safe_load_all(rendered) if item is not None]
    except yaml.YAMLError as exc:
        raise TkeReleaseError("blue-green Kubernetes manifest is invalid") from exc
    expected_deployments = {
        _slot_name(component, slot)
        for component in ("backend", "frontend", "scheduler")
    }
    deployments = {
        item.get("metadata", {}).get("name")
        for item in documents
        if isinstance(item, dict) and item.get("kind") == "Deployment"
    }
    if deployments != expected_deployments:
        raise TkeReleaseError("blue-green Deployment set is invalid")
    expected_services = {_slot_name("api", slot), _slot_name("frontend", slot)}
    if include_public_entrypoints:
        expected_services.update({"api", "frontend"})
    services = {
        item.get("metadata", {}).get("name")
        for item in documents
        if isinstance(item, dict) and item.get("kind") == "Service"
    }
    if services != expected_services:
        raise TkeReleaseError("blue-green Service set is invalid")
    ingresses = [
        item
        for item in documents
        if isinstance(item, dict) and item.get("kind") == "Ingress"
    ]
    if include_public_entrypoints != (len(ingresses) == 1):
        raise TkeReleaseError("blue-green Ingress contract is invalid")
    if not any(
        isinstance(item, dict)
        and item.get("kind") == "ConfigMap"
        and item.get("metadata", {}).get("name")
        == f"{RELEASE_CONFIGMAP_NAME}-{slot}"
        for item in documents
    ):
        raise TkeReleaseError("blue-green release ConfigMap is missing")
    for item in documents:
        if not isinstance(item, dict):
            raise TkeReleaseError("blue-green Kubernetes manifest is invalid")
        if item.get("kind") == "Secret" or "stringData" in item:
            raise TkeReleaseError("blue-green manifest contains Secret data")


def render_slot_manifests(
    *,
    manifests_dir: Path,
    release_sha: str,
    backend_image: str,
    frontend_image: str,
    schema_release: str,
    artifact_manifest_sha256: str,
    slot: str,
    scheduler_mode: str,
    include_public_entrypoints: bool,
    namespace: str = "aiops-demo",
    runner: Runner | None = None,
) -> bytes:
    """Render one isolated blue-green slot from the canonical Kustomize base.

    Runtime configuration, Secret, PVC and ServiceAccount stay shared inside
    ``aiops-demo``.  Deployments, their release ConfigMap and both internal
    Services are slot-specific.  Only an active slot receives stable public
    Services and the customer-facing Ingress.
    """
    slot = _validate_slot(slot)
    if scheduler_mode not in {"active", "standby"}:
        raise TkeReleaseError("TKE Scheduler mode is invalid")
    if not isinstance(include_public_entrypoints, bool):
        raise TkeReleaseError("TKE blue-green public entrypoint mode is invalid")
    rendered = render_release_manifests(
        manifests_dir=manifests_dir,
        release_sha=release_sha,
        backend_image=backend_image,
        frontend_image=frontend_image,
        schema_release=schema_release,
        artifact_manifest_sha256=artifact_manifest_sha256,
        namespace=namespace,
        runner=runner,
        include_bootstrap=True,
    )
    try:
        documents = [item for item in yaml.safe_load_all(rendered) if item is not None]
    except yaml.YAMLError as exc:
        raise TkeReleaseError("blue-green Kubernetes manifest is invalid") from exc
    release_config_name = f"{RELEASE_CONFIGMAP_NAME}-{slot}"
    slot_documents: list[dict[str, Any]] = []
    public_services: list[dict[str, Any]] = []
    for item in documents:
        if not isinstance(item, dict):
            raise TkeReleaseError("blue-green Kubernetes manifest is invalid")
        metadata = item.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
            raise TkeReleaseError("blue-green Kubernetes metadata is invalid")
        kind = item.get("kind")
        name = metadata["name"]
        if kind in {"Namespace", "ServiceAccount"}:
            continue
        if kind == "ConfigMap" and name == RELEASE_CONFIGMAP_NAME:
            metadata["name"] = release_config_name
            metadata.setdefault("labels", {})[SLOT_LABEL] = slot
        elif kind == "Deployment" and name in {"backend", "frontend", "scheduler"}:
            metadata["name"] = _slot_name(name, slot)
            metadata.setdefault("labels", {})[SLOT_LABEL] = slot
            try:
                selector = item["spec"]["selector"]["matchLabels"]
                template_labels = item["spec"]["template"]["metadata"]["labels"]
            except (KeyError, TypeError) as exc:
                raise TkeReleaseError("blue-green Deployment manifest is invalid") from exc
            if not isinstance(selector, dict) or not isinstance(template_labels, dict):
                raise TkeReleaseError("blue-green Deployment manifest is invalid")
            selector[SLOT_LABEL] = slot
            template_labels[SLOT_LABEL] = slot
            _replace_release_config_reference(item, release_config_name=release_config_name)
            if name == "scheduler":
                _set_scheduler_mode(item, scheduler_mode)
            if name == "frontend":
                _set_frontend_api_upstream(item, upstream=_slot_name("api", slot))
        elif kind == "Service" and name in {"api", "frontend"}:
            try:
                selector = item["spec"]["selector"]
            except (KeyError, TypeError) as exc:
                raise TkeReleaseError("blue-green Service manifest is invalid") from exc
            if not isinstance(selector, dict):
                raise TkeReleaseError("blue-green Service manifest is invalid")
            metadata["name"] = _slot_name(name, slot)
            metadata.setdefault("labels", {})[SLOT_LABEL] = slot
            selector[SLOT_LABEL] = slot
            if include_public_entrypoints:
                public = copy.deepcopy(item)
                public["metadata"]["name"] = name
                public_services.append(public)
        elif kind == "PodDisruptionBudget" and name in {"backend", "frontend", "scheduler"}:
            try:
                selector = item["spec"]["selector"]["matchLabels"]
            except (KeyError, TypeError) as exc:
                raise TkeReleaseError("blue-green PDB manifest is invalid") from exc
            if not isinstance(selector, dict):
                raise TkeReleaseError("blue-green PDB manifest is invalid")
            metadata["name"] = _slot_name(name, slot)
            metadata.setdefault("labels", {})[SLOT_LABEL] = slot
            selector[SLOT_LABEL] = slot
        elif kind == "Ingress" and name == "aiops":
            if not include_public_entrypoints:
                continue
        slot_documents.append(item)
    if include_public_entrypoints:
        slot_documents.extend(public_services)
    blue_green = yaml.safe_dump_all(
        slot_documents, allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    if not blue_green or len(blue_green) > MAX_RENDERED_BYTES:
        raise TkeReleaseError("blue-green Kubernetes manifest is invalid")
    _validate_slot_rendered(
        blue_green,
        slot=slot,
        include_public_entrypoints=include_public_entrypoints,
    )
    return blue_green


def _manifest_sha256(rendered: bytes) -> str:
    return hashlib.sha256(rendered).hexdigest()


def _validate_rendered(rendered: bytes) -> None:
    try:
        documents = [item for item in yaml.safe_load_all(rendered) if item is not None]
    except yaml.YAMLError as exc:
        raise TkeReleaseError("rendered Kubernetes manifest is invalid") from exc
    deployments = {
        item.get("metadata", {}).get("name")
        for item in documents
        if isinstance(item, dict) and item.get("kind") == "Deployment"
    }
    if deployments != {"backend", "frontend", "scheduler"}:
        raise TkeReleaseError("rendered Deployment set is invalid")
    for item in documents:
        if not isinstance(item, dict):
            raise TkeReleaseError("rendered Kubernetes manifest is invalid")
        if item.get("kind") == "Secret" or "stringData" in item:
            raise TkeReleaseError("rendered manifest contains Secret data")


def _server_side_dry_run(
    *,
    rendered: bytes,
    kube_context: str,
    runner: Runner | None,
    action: str,
) -> None:
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "apply",
                "--server-side",
                "--field-manager=aiops-release",
                "--dry-run=server",
                "-f",
                "-",
            ),
            input_bytes=rendered,
            timeout_seconds=ROLL_OUT_TIMEOUT_SECONDS,
        ),
        action,
    )


def _apply_runtime_render(
    *,
    rendered: bytes,
    kube_context: str,
    runner: Runner | None,
    action: str,
) -> None:
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "apply",
                "--server-side",
                "--field-manager=aiops-release",
                "-f",
                "-",
            ),
            input_bytes=rendered,
            timeout_seconds=ROLL_OUT_TIMEOUT_SECONDS,
        ),
        action,
    )


def build_plan(
    *,
    repo: Path,
    manifests_dir: Path,
    release_sha: str,
    backend_image: str,
    frontend_image: str,
    schema_release: str,
    artifact_manifest_sha256: str,
    namespace: str = "aiops-demo",
    runner: Runner | None = None,
) -> dict[str, Any]:
    del repo
    rendered = render_release_manifests(
        manifests_dir=manifests_dir,
        namespace=namespace,
        release_sha=release_sha,
        backend_image=backend_image,
        frontend_image=frontend_image,
        schema_release=schema_release,
        artifact_manifest_sha256=artifact_manifest_sha256,
        runner=runner,
    )
    _validate_rendered(rendered)
    return {
        "adapter": "tke",
        "status": "tke_plan",
        "releaseSha": release_sha,
        "namespace": namespace,
        "images": {"backend": backend_image, "frontend": frontend_image},
        "manifestSha256": _manifest_sha256(rendered),
        "phases": [
            {
                "name": "candidate",
                "mode": "build-push-digest-verify",
                "mutationsAllowed": False,
            },
            {
                "name": "prepare",
                "mode": "rbac-secret-network-preflight",
                "mutationsAllowed": False,
            },
            {
                "name": "rehearse-pull",
                "mode": "temporary-pod-cluster-pull-verify",
                "mutationsAllowed": False,
                "customerTraffic": False,
            },
            {
                "name": "rehearse-mysql",
                "mode": "temporary-backend-read-only-query",
                "mutationsAllowed": False,
                "customerTraffic": False,
                "requires": ["cluster-pull-proof"],
            },
            {
                "name": "rehearse-deploy",
                "mode": "server-side-apply-standby-candidate",
                "mutationsAllowed": False,
                "customerTraffic": False,
                "requires": ["cluster-pull-proof", "mysql-rehearsal-proof"],
                "schedulerMode": "standby",
                "rendersIngress": False,
            },
            {
                "name": "activate",
                "mode": "server-side-apply-active-release",
                "mutationsAllowed": False,
                "schedulerMode": "active",
                "rendersIngress": True,
                "requires": [
                    "cluster-pull-proof",
                    "mysql-rehearsal-proof",
                    "standby-candidate-proof",
                ],
            },
            {
                "name": "accept",
                "mode": "workload-endpoint-identity-readback",
                "mutationsAllowed": False,
            },
            {
                "name": "rollback",
                "mode": "previous-digest-server-side-apply",
                "mutationsAllowed": False,
            },
        ],
        "safety": {
            "mutationsAllowed": False,
            "productionTrafficChanged": False,
            "secretValuesRecorded": False,
        },
    }


def _require_execute(execute: bool, confirm_release_sha: str | None, release_sha: str) -> None:
    if execute is not True:
        raise TkeReleaseError("TKE deploy requires --execute")
    if confirm_release_sha != release_sha:
        raise TkeReleaseError("TKE deploy requires an exact --confirm-release-sha")


def _require_success(result: CommandResult, action: str) -> bytes:
    if result.returncode != 0:
        raise TkeReleaseError(f"{action}:{_safe_error_category(result.stderr)}")
    return result.stdout


def _read_json(result: CommandResult, action: str) -> dict[str, Any]:
    raw = _require_success(result, action)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeReleaseError(f"{action}:invalid_json") from exc
    if not isinstance(value, dict):
        raise TkeReleaseError(f"{action}:invalid_json")
    return value


def _read_named_resource(
    *,
    kube_context: str,
    namespace: str,
    resource: str,
    name: str,
    action: str,
    runner: Runner | None,
) -> dict[str, Any]:
    return _read_json(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "get",
                resource,
                name,
                "--namespace",
                namespace,
                "-o",
                "json",
            ),
            timeout_seconds=30,
        ),
        action,
    )


def _verify_runtime_configmap(config: dict[str, Any]) -> None:
    metadata = config.get("metadata")
    data = config.get("data")
    raw_config = data.get(RUNTIME_CONFIG_KEY) if isinstance(data, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != RUNTIME_CONFIGMAP_NAME
        or not isinstance(raw_config, str)
        or not raw_config.strip()
    ):
        raise TkeReleaseError("TKE preflight runtime ConfigMap is missing or invalid")
    try:
        loaded = yaml.safe_load(raw_config)
    except yaml.YAMLError as exc:
        raise TkeReleaseError("TKE preflight runtime ConfigMap is missing or invalid") from exc
    if not isinstance(loaded, dict):
        raise TkeReleaseError("TKE preflight runtime ConfigMap is missing or invalid")


def _runtime_config_mapping(raw_config: str, *, error: str) -> dict[str, Any]:
    """Parse one runtime YAML mapping without exposing its values in errors."""
    if not isinstance(raw_config, str) or not raw_config.strip():
        raise TkeReleaseError(error)
    try:
        loaded = yaml.safe_load(raw_config)
    except yaml.YAMLError as exc:
        raise TkeReleaseError(error) from exc
    if not isinstance(loaded, dict):
        raise TkeReleaseError(error)
    return loaded


def _runtime_configmap_mapping(config: dict[str, Any]) -> dict[str, Any]:
    metadata = config.get("metadata")
    data = config.get("data")
    raw_config = data.get(RUNTIME_CONFIG_KEY) if isinstance(data, dict) else None
    if not isinstance(metadata, dict) or metadata.get("name") != RUNTIME_CONFIGMAP_NAME:
        raise TkeReleaseError("TKE runtime ConfigMap is missing or invalid")
    return _runtime_config_mapping(
        raw_config,
        error="TKE runtime ConfigMap is missing or invalid",
    )


def _runtime_config_template_mapping(template_path: Path) -> dict[str, Any]:
    path = Path(template_path)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > MAX_RENDERED_BYTES
    ):
        raise TkeReleaseError("runtime config template is invalid")
    try:
        raw_template = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise TkeReleaseError("runtime config template is invalid") from exc
    return _runtime_config_mapping(raw_template, error="runtime config template is invalid")


def _runtime_config_value_type(value: object) -> str:
    """Return an exact YAML-value category for safe schema comparisons."""
    if isinstance(value, dict):
        return "mapping"
    if isinstance(value, list):
        return "sequence"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _runtime_config_field_path(parts: tuple[str, ...]) -> str:
    return ".".join(parts) if parts else "<root>"


def _runtime_config_leaf_paths(value: object, *, parts: tuple[str, ...]) -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            if not isinstance(key, str):
                raise TkeReleaseError("runtime config schema key is invalid")
            paths.extend(_runtime_config_leaf_paths(child, parts=(*parts, key)))
        return paths or [_runtime_config_field_path(parts)]
    return [_runtime_config_field_path(parts)]


def _runtime_config_schema_node(
    *,
    template: object,
    production: object,
    production_present: bool,
    parts: tuple[str, ...],
    added_fields: list[str],
    removed_fields: list[str],
    preserved_field_count: list[int],
) -> object:
    """Reconcile one configuration node without ever copying production-only fields."""
    template_type = _runtime_config_value_type(template)
    if not production_present:
        added_fields.extend(_runtime_config_leaf_paths(template, parts=parts))
        return copy.deepcopy(template)

    production_type = _runtime_config_value_type(production)
    if template_type != production_type:
        raise TkeReleaseError(
            "runtime config schema type conflict at "
            f"{_runtime_config_field_path(parts)}: expected {template_type}, "
            f"found {production_type}"
        )
    if not isinstance(template, dict):
        preserved_field_count[0] += 1
        return copy.deepcopy(production)

    if not isinstance(production, dict):  # Satisfy static narrowing and fail closed.
        raise TkeReleaseError("runtime config schema type conflict")
    synchronized: dict[str, Any] = {}
    for key, template_value in template.items():
        if not isinstance(key, str):
            raise TkeReleaseError("runtime config schema key is invalid")
        synchronized[key] = _runtime_config_schema_node(
            template=template_value,
            production=production.get(key),
            production_present=key in production,
            parts=(*parts, key),
            added_fields=added_fields,
            removed_fields=removed_fields,
            preserved_field_count=preserved_field_count,
        )
    for key, production_value in production.items():
        if not isinstance(key, str):
            raise TkeReleaseError("runtime config schema key is invalid")
        if key not in template:
            removed_fields.extend(
                _runtime_config_leaf_paths(production_value, parts=(*parts, key))
            )
    return synchronized


def synchronize_runtime_config_schema(
    *, template: dict[str, Any], production: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep production values while aligning its YAML fields to the release template.

    Common leaf values are retained. New fields take the candidate's default;
    retired fields are omitted. Any mapping/sequence/scalar type change is
    rejected instead of guessing a value conversion.
    """
    if not isinstance(template, dict) or not isinstance(production, dict):
        raise TkeReleaseError("runtime config schema root must be a mapping")
    added_fields: list[str] = []
    removed_fields: list[str] = []
    preserved_field_count = [0]
    synchronized = _runtime_config_schema_node(
        template=template,
        production=production,
        production_present=True,
        parts=(),
        added_fields=added_fields,
        removed_fields=removed_fields,
        preserved_field_count=preserved_field_count,
    )
    if not isinstance(synchronized, dict):
        raise TkeReleaseError("runtime config schema root must be a mapping")
    return synchronized, {
        "addedFields": sorted(added_fields),
        "preservedFieldCount": preserved_field_count[0],
        "removedFields": sorted(removed_fields),
    }


def _runtime_config_schema_signature(value: object) -> object:
    """Create a value-free structural fingerprint input for release evidence."""
    value_type = _runtime_config_value_type(value)
    if isinstance(value, dict):
        fields: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TkeReleaseError("runtime config schema key is invalid")
            fields[key] = _runtime_config_schema_signature(child)
        return {"type": value_type, "fields": fields}
    return {"type": value_type}


def _runtime_config_schema_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(_runtime_config_schema_signature(value))).hexdigest()


def _runtime_configmap_manifest(*, namespace: str, config: dict[str, Any]) -> bytes:
    try:
        raw_config = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        manifest = yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": RUNTIME_CONFIGMAP_NAME,
                    "namespace": namespace,
                },
                "data": {RUNTIME_CONFIG_KEY: raw_config},
            },
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
    except (TypeError, yaml.YAMLError) as exc:
        raise TkeReleaseError("runtime config schema render is invalid") from exc
    if not manifest or len(manifest) > MAX_RENDERED_BYTES:
        raise TkeReleaseError("runtime config schema render is invalid")
    return manifest


def _apply_runtime_configmap_schema(
    *, rendered: bytes, kube_context: str, runner: Runner | None, dry_run: bool
) -> None:
    arguments = [
        "apply",
        "--server-side",
        "--force-conflicts",
        "--field-manager=aiops-runtime-config-schema-sync",
    ]
    if dry_run:
        arguments.append("--dry-run=server")
    arguments.extend(("-f", "-"))
    _require_success(
        _execute(
            runner,
            _kubectl(kube_context, *arguments),
            input_bytes=rendered,
            timeout_seconds=RUNTIME_CONFIG_SYNC_TIMEOUT_SECONDS,
        ),
        "runtime_config_schema_apply",
    )


def sync_runtime_config_schema(
    *,
    namespace: str,
    release_sha: str,
    kube_context: str,
    template_path: Path,
    execute: bool,
    confirm_release_sha: str | None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Safely align the shared runtime ConfigMap before a TKE standby deploy."""
    if NAMESPACE.fullmatch(namespace) is None or SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("runtime config schema identity is invalid")
    _require_execute(execute, confirm_release_sha, release_sha)
    _verify_target_context(kube_context=kube_context, runner=runner)
    template = _runtime_config_template_mapping(template_path)
    current_configmap = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="configmap",
        name=RUNTIME_CONFIGMAP_NAME,
        action="runtime_config_schema_read",
        runner=runner,
    )
    production = _runtime_configmap_mapping(current_configmap)
    synchronized, summary = synchronize_runtime_config_schema(
        template=template,
        production=production,
    )
    result = {
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "runtimeConfigMap": RUNTIME_CONFIGMAP_NAME,
        "changed": synchronized != production,
        **summary,
        "configSchemaSha256": _runtime_config_schema_sha256(synchronized),
    }
    if not result["changed"]:
        return {"status": "runtime_config_aligned", **result}
    rendered = _runtime_configmap_manifest(namespace=namespace, config=synchronized)
    _apply_runtime_configmap_schema(
        rendered=rendered,
        kube_context=kube_context,
        runner=runner,
        dry_run=True,
    )
    _apply_runtime_configmap_schema(
        rendered=rendered,
        kube_context=kube_context,
        runner=runner,
        dry_run=False,
    )
    readback_configmap = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="configmap",
        name=RUNTIME_CONFIGMAP_NAME,
        action="runtime_config_schema_readback",
        runner=runner,
    )
    if _runtime_configmap_mapping(readback_configmap) != synchronized:
        raise TkeReleaseError("runtime config schema readback does not match the candidate")
    return {"status": "runtime_config_synced", **result}


def _read_runtime_secret_keys(
    *,
    kube_context: str,
    namespace: str,
    runner: Runner | None,
    required_keys: frozenset[str] = REQUIRED_SECRET_KEYS,
) -> list[str]:
    """Read only Secret metadata and key names, never base64 values."""
    template = (
        "{{.metadata.name}}{{\"\\n\"}}"
        "{{range $key, $_ := .data}}{{$key}}{{\"\\n\"}}{{end}}"
    )
    raw = _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "get",
                "secret",
                RUNTIME_SECRET_NAME,
                "--namespace",
                namespace,
                f"--template={template}",
            ),
            timeout_seconds=30,
        ),
        "secret_metadata",
    )
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise TkeReleaseError("TKE preflight Secret is missing or incomplete") from exc
    if not lines or lines[0] != RUNTIME_SECRET_NAME:
        raise TkeReleaseError("TKE preflight Secret is missing or incomplete")
    keys = lines[1:]
    if (
        not keys
        or len(keys) != len(set(keys))
        or not required_keys.issubset(keys)
        or any(not key or any(character.isspace() for character in key) for key in keys)
    ):
        raise TkeReleaseError("TKE preflight Secret is missing or incomplete")
    return sorted(keys)


def _verify_runtime_pvc(pvc: dict[str, Any]) -> None:
    metadata = pvc.get("metadata")
    spec = pvc.get("spec")
    status = pvc.get("status")
    access_modes = spec.get("accessModes") if isinstance(spec, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != RUNTIME_PVC_NAME
        or not isinstance(spec, dict)
        or not isinstance(status, dict)
        or status.get("phase") != "Bound"
        or not isinstance(access_modes, list)
        or "ReadWriteMany" not in access_modes
        or not isinstance(spec.get("volumeName"), str)
        or not spec["volumeName"]
    ):
        raise TkeReleaseError("TKE preflight runtime PVC must be Bound ReadWriteMany")


def _verify_target_context(*, kube_context: str, runner: Runner | None) -> None:
    context = _validate_kube_context(kube_context)
    payload = _read_json(
        _execute(
            runner,
            _kubectl(context, "config", "view", "--minify", "--output", "json"),
            timeout_seconds=30,
        ),
        "kube_context",
    )
    contexts = payload.get("contexts")
    clusters = payload.get("clusters")
    if not isinstance(contexts, list) or not isinstance(clusters, list):
        raise TkeReleaseError("kube context target cluster is invalid")
    selected = [item for item in contexts if isinstance(item, dict) and item.get("name") == context]
    if len(selected) != 1:
        raise TkeReleaseError("kube context target cluster is invalid")
    details = selected[0].get("context")
    cluster_names = {
        item.get("name")
        for item in clusters
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if (
        not isinstance(details, dict)
        or details.get("cluster") != TARGET_CLUSTER_ID
        or TARGET_CLUSTER_ID not in cluster_names
    ):
        raise TkeReleaseError("kube context target cluster is invalid")


def prepare_deploy(
    *,
    namespace: str,
    release_sha: str,
    kube_context: str,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Read only the runtime materials required before a TKE deployment."""
    if NAMESPACE.fullmatch(namespace) is None or SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("TKE preflight identity is invalid")
    _verify_target_context(kube_context=kube_context, runner=runner)
    runtime_config = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="configmap",
        name=RUNTIME_CONFIGMAP_NAME,
        action="runtime_config_metadata",
        runner=runner,
    )
    _verify_runtime_configmap(runtime_config)
    secret_keys = _read_runtime_secret_keys(
        kube_context=kube_context,
        namespace=namespace,
        runner=runner,
    )
    pvc = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="pvc",
        name=RUNTIME_PVC_NAME,
        action="runtime_pvc_metadata",
        runner=runner,
    )
    _verify_runtime_pvc(pvc)
    return {
        "status": "prepared",
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "secretKeys": secret_keys,
        "runtimeConfigMap": RUNTIME_CONFIGMAP_NAME,
        "runtimePvc": RUNTIME_PVC_NAME,
    }


def _pull_rehearsal_pod(
    *, component: str, image: str, namespace: str, release_sha: str
) -> dict[str, Any]:
    if component not in {"backend", "frontend"}:
        raise TkeReleaseError("TKE pull rehearsal component is invalid")
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"aiops-pull-{component}-{release_sha[:12]}",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": f"aiops-pull-{component}",
                "app.kubernetes.io/part-of": "aiops",
                "aiops.example.test/release-sha": release_sha,
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": {
                "aiops.example.test/runtime": "shared",
            },
            "automountServiceAccountToken": False,
            "serviceAccountName": "aiops-runtime",
            "securityContext": {
                "runAsNonRoot": True,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": component,
                    "image": image,
                    "imagePullPolicy": "Always",
                    "command": ["/bin/sh", "-c", "exit 0"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi"},
                        "limits": {"cpu": "100m", "memory": "64Mi"},
                    },
                }
            ],
        },
    }


def _validate_pull_rehearsal_pod(
    *, payload: dict[str, Any], component: str, image: str, expected_name: str
) -> None:
    metadata = payload.get("metadata")
    status = payload.get("status")
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != expected_name
        or not isinstance(metadata.get("uid"), str)
        or not metadata["uid"]
        or not isinstance(status, dict)
        or status.get("phase") != "Succeeded"
    ):
        raise TkeReleaseError("TKE pull rehearsal pod identity is invalid")
    statuses = status.get("containerStatuses")
    if not isinstance(statuses, list) or len(statuses) != 1 or not isinstance(statuses[0], dict):
        raise TkeReleaseError("TKE pull rehearsal pod status is invalid")
    container_status = statuses[0]
    terminated = container_status.get("state", {}).get("terminated")
    reported_image = container_status.get("image")
    image_id = container_status.get("imageID")
    expected_digest = image.rsplit("@", 1)[1]
    digest_only_image_ids = {
        expected_digest,
        f"docker-pullable://{expected_digest}",
        f"containerd://{expected_digest}",
    }
    is_runtime_config_digest = (
        isinstance(reported_image, str)
        and reported_image.startswith("sha256:")
        and SHA64.fullmatch(reported_image.removeprefix("sha256:")) is not None
    )
    if (
        container_status.get("name") != component
        or (reported_image != image and not is_runtime_config_digest)
        or not isinstance(terminated, dict)
        or terminated.get("exitCode") != 0
        or not isinstance(image_id, str)
        or (
            image_id
            and not image_id.endswith(image)
            and image_id not in digest_only_image_ids
        )
    ):
        raise TkeReleaseError("TKE pull rehearsal image identity is invalid")


def _pull_rehearsal_pod_names(*, release_sha: str) -> tuple[str, str]:
    if SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("TKE pull rehearsal identity is invalid")
    return (
        f"aiops-pull-backend-{release_sha[:12]}",
        f"aiops-pull-frontend-{release_sha[:12]}",
    )


def _cleanup_pull_rehearsal_pod(
    *, kube_context: str, namespace: str, name: str, runner: Runner | None
) -> None:
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "delete",
                "pod",
                name,
                "--namespace",
                namespace,
                "--ignore-not-found=true",
                "--wait=true",
            ),
            timeout_seconds=60,
        ),
        "pull_rehearsal_cleanup",
    )


def execute_pull_rehearsal(
    *,
    namespace: str,
    release_sha: str,
    kube_context: str,
    backend_image: str,
    frontend_image: str,
    execute: bool,
    confirm_release_sha: str | None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Prove TKE can pull both immutable candidate images without runtime inputs."""
    _require_release_values(
        namespace=namespace,
        release_sha=release_sha,
        backend_image=backend_image,
        frontend_image=frontend_image,
        schema_release="pull-rehearsal",
        artifact_manifest_sha256="0" * 64,
    )
    _require_execute(execute, confirm_release_sha, release_sha)
    _verify_target_context(kube_context=kube_context, runner=runner)
    pods = {
        component: _pull_rehearsal_pod(
            component=component,
            image=image,
            namespace=namespace,
            release_sha=release_sha,
        )
        for component, image in (("backend", backend_image), ("frontend", frontend_image))
    }
    rendered = yaml.safe_dump_all(
        [pods["backend"], pods["frontend"]], allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    names = _pull_rehearsal_pod_names(release_sha=release_sha)
    create_attempted = False
    cleanup_error: TkeReleaseError | None = None
    try:
        create_attempted = True
        _require_success(
            _execute(
                runner,
                _kubectl(kube_context, "create", "-f", "-"),
                input_bytes=rendered,
                timeout_seconds=60,
            ),
            "pull_rehearsal_create",
        )
        for component, image in (("backend", backend_image), ("frontend", frontend_image)):
            name = pods[component]["metadata"]["name"]
            _require_success(
                _execute(
                    runner,
                    _kubectl(
                        kube_context,
                        "wait",
                        "--for=condition=Ready=false",
                        f"pod/{name}",
                        "--namespace",
                        namespace,
                        "--timeout=10s",
                    ),
                    timeout_seconds=30,
                ),
                "pull_rehearsal_pull_wait",
            )
            _require_success(
                _execute(
                    runner,
                    _kubectl(
                        kube_context,
                        "wait",
                        "--for=jsonpath={.status.phase}=Succeeded",
                        f"pod/{name}",
                        "--namespace",
                        namespace,
                        f"--timeout={PULL_REHEARSAL_TIMEOUT_SECONDS}s",
                    ),
                    timeout_seconds=PULL_REHEARSAL_TIMEOUT_SECONDS + 30,
                ),
                "pull_rehearsal_complete_wait",
            )
            payload = _read_json(
                _execute(
                    runner,
                    _kubectl(
                        kube_context,
                        "get",
                        "pod",
                        name,
                        "--namespace",
                        namespace,
                        "-o",
                        "json",
                    ),
                    timeout_seconds=30,
                ),
                "pull_rehearsal_readback",
            )
            _validate_pull_rehearsal_pod(
                payload=payload,
                component=component,
                image=image,
                expected_name=name,
            )
    finally:
        if create_attempted:
            for name in names:
                try:
                    _cleanup_pull_rehearsal_pod(
                        kube_context=kube_context,
                        namespace=namespace,
                        name=name,
                        runner=runner,
                    )
                except TkeReleaseError as exc:
                    cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error
    return {
        "status": "cluster_pull_verified",
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "images": {"backend": backend_image, "frontend": frontend_image},
    }


def _job_condition_is_true(payload: dict[str, Any], condition_type: str) -> bool:
    status = payload.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    return bool(
        isinstance(conditions, list)
        and any(
            isinstance(condition, dict)
            and condition.get("type") == condition_type
            and condition.get("status") == "True"
            for condition in conditions
        )
    )


def _cfs_runtime_init_job(
    *, namespace: str, release_sha: str, backend_image: str
) -> dict[str, Any]:
    """Create the only root-scoped operation permitted for the CFS runtime path.

    The init container may only create and ownership-initialize ``/runtime``.
    The main container runs as the application UID and must create then remove
    its own random probe file before the Job can succeed.  It receives neither
    runtime configuration nor application Secrets.
    """
    if NAMESPACE.fullmatch(namespace) is None or SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("TKE CFS runtime initialization identity is invalid")
    if IMAGE_DIGEST.fullmatch(backend_image) is None:
        raise TkeReleaseError("TKE CFS runtime initialization backend image is invalid")
    labels = {
        "app.kubernetes.io/name": "cfs-runtime-init",
        "app.kubernetes.io/part-of": "aiops",
        "aiops.example.test/release-sha": release_sha,
    }
    volume_mounts = [{"name": "runtime-data", "mountPath": "/app/data"}]
    resources = {
        "requests": {"cpu": "25m", "memory": "64Mi"},
        "limits": {"cpu": "100m", "memory": "128Mi"},
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": CFS_RUNTIME_INIT_JOB_NAME,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": CFS_RUNTIME_INIT_TIMEOUT_SECONDS,
            "ttlSecondsAfterFinished": 60,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": RUNTIME_SERVICEACCOUNT_NAME,
                    "automountServiceAccountToken": False,
                    "nodeSelector": {
                        "aiops.example.test/runtime": "shared",
                    },
                    "securityContext": {
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "volumes": [
                        {
                            "name": "runtime-data",
                            "persistentVolumeClaim": {
                                "claimName": RUNTIME_PVC_NAME,
                                "readOnly": False,
                            },
                        }
                    ],
                    "initContainers": [
                        {
                            "name": "runtime-data-init",
                            "image": backend_image,
                            "imagePullPolicy": "Always",
                            "command": ["python", "-c", CFS_RUNTIME_INIT_SCRIPT],
                            "resources": resources,
                            "securityContext": {
                                "runAsNonRoot": False,
                                "runAsUser": 0,
                                "runAsGroup": 0,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {
                                    "drop": ["ALL"],
                                    "add": ["CHOWN"],
                                },
                            },
                            "volumeMounts": volume_mounts,
                        }
                    ],
                    "containers": [
                        {
                            "name": "runtime-data-verify",
                            "image": backend_image,
                            "imagePullPolicy": "Always",
                            "command": ["python", "-c", CFS_RUNTIME_VERIFY_SCRIPT],
                            "resources": resources,
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 10001,
                                "runAsGroup": 10001,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": volume_mounts,
                        }
                    ],
                },
            },
        },
    }


def _cfs_runtime_init_job_logs(
    *,
    kube_context: str,
    namespace: str,
    name: str,
    container: str,
    runner: Runner | None,
) -> bytes:
    return _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "logs",
                f"job/{name}",
                "--namespace",
                namespace,
                "--container",
                container,
            ),
            timeout_seconds=30,
        ),
        "cfs_runtime_init_logs",
    )


def _parse_cfs_runtime_init_log(raw: bytes) -> dict[str, int | str]:
    if not raw or len(raw) > 64 * 1024:
        raise TkeReleaseError("TKE CFS runtime initialization result is invalid")
    try:
        lines = raw.decode("utf-8").splitlines()
        payload = json.loads(lines[-1]) if lines else None
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeReleaseError("TKE CFS runtime initialization result is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"gid", "mode", "status", "uid"}
        or payload.get("status") != "runtime_data_non_root_write_verified"
        or payload.get("uid") != 10001
        or payload.get("gid") != 10001
        or payload.get("mode") != "0770"
    ):
        raise TkeReleaseError("TKE CFS runtime initialization result is invalid")
    return {"gid": 10001, "mode": "0770", "uid": 10001}


def _parse_cfs_runtime_init_failure_log(raw: bytes) -> str:
    if not raw or len(raw) > 64 * 1024:
        raise TkeReleaseError("TKE CFS runtime initialization failure result is invalid")
    try:
        lines = raw.decode("utf-8").splitlines()
        payload = json.loads(lines[-1]) if lines else None
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeReleaseError("TKE CFS runtime initialization failure result is invalid") from exc
    failure_code = payload.get("failureCode") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"failureCode", "status"}
        or payload.get("status") != "runtime_data_init_failed"
        or failure_code not in CFS_RUNTIME_INIT_FAILURE_CODES
    ):
        raise TkeReleaseError("TKE CFS runtime initialization failure result is invalid")
    return str(failure_code)


def _cfs_runtime_init_failure_error(
    *, kube_context: str, namespace: str, name: str, runner: Runner | None
) -> TkeReleaseError:
    try:
        failure_code = _parse_cfs_runtime_init_failure_log(
            _cfs_runtime_init_job_logs(
                kube_context=kube_context,
                namespace=namespace,
                name=name,
                container="runtime-data-init",
                runner=runner,
            )
        )
    except TkeReleaseError:
        return TkeReleaseError("TKE CFS runtime initialization Job failed")
    return TkeReleaseError("TKE CFS runtime initialization failed: " + failure_code)


def _wait_for_cfs_runtime_init_completion(
    *, kube_context: str, namespace: str, name: str, runner: Runner | None
) -> dict[str, Any]:
    deadline = time.monotonic() + CFS_RUNTIME_INIT_TIMEOUT_SECONDS
    while True:
        payload = _read_named_resource(
            kube_context=kube_context,
            namespace=namespace,
            resource="job",
            name=name,
            action="cfs_runtime_init_job_readback",
            runner=runner,
        )
        if _job_condition_is_true(payload, "Complete") or _job_condition_is_true(
            payload, "Failed"
        ):
            return payload
        if time.monotonic() >= deadline:
            raise TkeReleaseError("TKE CFS runtime initialization Job timed out")
        time.sleep(2)


def _cleanup_cfs_runtime_init_job(
    *, kube_context: str, namespace: str, name: str, runner: Runner | None
) -> None:
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "delete",
                "job",
                name,
                "--namespace",
                namespace,
                "--ignore-not-found=true",
                "--wait=true",
            ),
            timeout_seconds=60,
        ),
        "cfs_runtime_init_cleanup",
    )


def execute_cfs_runtime_init(
    *,
    namespace: str,
    release_sha: str,
    kube_context: str,
    backend_image: str,
    execute: bool,
    confirm_release_sha: str | None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Initialize only the CFS runtime directory, then prove it is app-writable."""
    job = _cfs_runtime_init_job(
        namespace=namespace,
        release_sha=release_sha,
        backend_image=backend_image,
    )
    _require_execute(execute, confirm_release_sha, release_sha)
    _verify_target_context(kube_context=kube_context, runner=runner)
    pvc = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="pvc",
        name=RUNTIME_PVC_NAME,
        action="cfs_runtime_init_pvc",
        runner=runner,
    )
    _verify_runtime_pvc(pvc)
    name = CFS_RUNTIME_INIT_JOB_NAME
    rendered = yaml.safe_dump(job, allow_unicode=True, sort_keys=False).encode("utf-8")
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "create",
                "--dry-run=server",
                "-f",
                "-",
            ),
            input_bytes=rendered,
            timeout_seconds=60,
        ),
        "cfs_runtime_init_server_dry_run",
    )
    cleanup_required = False
    cleanup_error: TkeReleaseError | None = None
    try:
        _require_success(
            _execute(
                runner,
                _kubectl(kube_context, "create", "-f", "-"),
                input_bytes=rendered,
                timeout_seconds=60,
            ),
            "cfs_runtime_init_create",
        )
        cleanup_required = True
        completed_job = _wait_for_cfs_runtime_init_completion(
            kube_context=kube_context,
            namespace=namespace,
            name=name,
            runner=runner,
        )
        if _job_condition_is_true(completed_job, "Failed"):
            raise _cfs_runtime_init_failure_error(
                kube_context=kube_context,
                namespace=namespace,
                name=name,
                runner=runner,
            )
        result = _parse_cfs_runtime_init_log(
            _cfs_runtime_init_job_logs(
                kube_context=kube_context,
                namespace=namespace,
                name=name,
                container="runtime-data-verify",
                runner=runner,
            )
        )
    finally:
        if cleanup_required:
            try:
                _cleanup_cfs_runtime_init_job(
                    kube_context=kube_context,
                    namespace=namespace,
                    name=name,
                    runner=runner,
                )
            except TkeReleaseError as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error
    return {
        "status": "cfs_runtime_init_verified",
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "backendImage": backend_image,
        "jobName": name,
        "runtimeDataPath": CFS_RUNTIME_DATA_PATH,
        "runtimeDataUid": result["uid"],
        "runtimeDataGid": result["gid"],
        "runtimeDataMode": result["mode"],
        "nonRootWriteVerified": True,
    }


def _mysql_rehearsal_pod_name(*, release_sha: str) -> str:
    if SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("TKE MySQL rehearsal identity is invalid")
    return f"aiops-mysql-rehearse-{release_sha[:12]}"


def _mysql_rehearsal_pod(
    *, namespace: str, release_sha: str, backend_image: str
) -> dict[str, Any]:
    if NAMESPACE.fullmatch(namespace) is None or SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("TKE MySQL rehearsal identity is invalid")
    if IMAGE_DIGEST.fullmatch(backend_image) is None:
        raise TkeReleaseError("TKE MySQL rehearsal backend image is invalid")
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": _mysql_rehearsal_pod_name(release_sha=release_sha),
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "backend",
                "app.kubernetes.io/part-of": "aiops",
                "aiops.example.test/rehearsal": "mysql",
                "aiops.example.test/release-sha": release_sha,
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": {
                "aiops.example.test/runtime": "shared",
            },
            "automountServiceAccountToken": True,
            "serviceAccountName": RUNTIME_SERVICEACCOUNT_NAME,
            "securityContext": {
                "runAsNonRoot": True,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "mysql-rehearsal",
                    "image": backend_image,
                    "imagePullPolicy": "Always",
                    "command": ["python", "-c", MYSQL_REHEARSAL_SCRIPT],
                    "env": [
                        {"name": "ENV", "value": "prod"},
                        {
                            "name": "AUTO_DB_URI_PROD",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": RUNTIME_SECRET_NAME,
                                    "key": "AUTO_DB_URI_PROD",
                                    "optional": False,
                                }
                            },
                        },
                        {
                            "name": "AIOPS_CONFIG_PERSISTENCE",
                            "value": "kubernetes_configmap",
                        },
                        {
                            "name": "AIOPS_CONFIGMAP_NAME",
                            "value": RUNTIME_CONFIGMAP_NAME,
                        },
                        {
                            "name": "AIOPS_CONFIGMAP_NAMESPACE",
                            "value": namespace,
                        },
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "128Mi"},
                        "limits": {"cpu": "100m", "memory": "256Mi"},
                    },
                }
            ],
        },
    }


def _validate_mysql_rehearsal_pod(
    *, payload: dict[str, Any], expected_name: str, backend_image: str
) -> None:
    metadata = payload.get("metadata")
    status = payload.get("status")
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != expected_name
        or not isinstance(metadata.get("uid"), str)
        or not metadata["uid"]
        or not isinstance(status, dict)
        or status.get("phase") != "Succeeded"
    ):
        raise TkeReleaseError("TKE MySQL rehearsal Pod identity is invalid")
    statuses = status.get("containerStatuses")
    if not isinstance(statuses, list) or len(statuses) != 1 or not isinstance(statuses[0], dict):
        raise TkeReleaseError("TKE MySQL rehearsal Pod status is invalid")
    container = statuses[0]
    terminated = container.get("state", {}).get("terminated")
    reported_image = container.get("image")
    image_id = container.get("imageID")
    expected_digest = backend_image.rsplit("@", 1)[1]
    digest_only_image_ids = {
        expected_digest,
        f"docker-pullable://{expected_digest}",
        f"containerd://{expected_digest}",
    }
    is_runtime_config_digest = (
        isinstance(reported_image, str)
        and reported_image.startswith("sha256:")
        and SHA64.fullmatch(reported_image.removeprefix("sha256:")) is not None
    )
    if (
        container.get("name") != "mysql-rehearsal"
        or (reported_image != backend_image and not is_runtime_config_digest)
        or not isinstance(terminated, dict)
        or terminated.get("exitCode") != 0
        or not isinstance(image_id, str)
        or (
            image_id
            and not image_id.endswith(backend_image)
            and image_id not in digest_only_image_ids
        )
    ):
        raise TkeReleaseError("TKE MySQL rehearsal Pod image identity is invalid")


def _parse_mysql_rehearsal_log(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > 64 * 1024:
        raise TkeReleaseError("TKE MySQL rehearsal result is invalid")
    try:
        lines = raw.decode("utf-8").splitlines()
        payload = json.loads(lines[-1]) if lines else None
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeReleaseError("TKE MySQL rehearsal result is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"readOnlyQuery", "status"}:
        raise TkeReleaseError("TKE MySQL rehearsal result is invalid")
    if (
        payload.get("status") != "mysql_rehearsal_probe"
        or payload.get("readOnlyQuery") is not True
    ):
        raise TkeReleaseError("TKE MySQL rehearsal result is invalid")
    return {
        "readOnlyQuery": True,
    }


def _parse_mysql_rehearsal_failure_log(raw: bytes) -> tuple[str, ...]:
    if not raw or len(raw) > 64 * 1024:
        raise TkeReleaseError("TKE MySQL rehearsal failure result is invalid")
    try:
        lines = raw.decode("utf-8").splitlines()
        payload = json.loads(lines[-1]) if lines else None
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeReleaseError("TKE MySQL rehearsal failure result is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"failureCodes", "status"}
        or payload.get("status") != "mysql_rehearsal_probe_failed"
    ):
        raise TkeReleaseError("TKE MySQL rehearsal failure result is invalid")
    failure_codes = payload.get("failureCodes")
    if (
        not isinstance(failure_codes, list)
        or not failure_codes
        or any(
            not isinstance(code, str) or code not in MYSQL_REHEARSAL_FAILURE_CODES
            for code in failure_codes
        )
        or len(set(failure_codes)) != len(failure_codes)
    ):
        raise TkeReleaseError("TKE MySQL rehearsal failure result is invalid")
    return tuple(sorted(failure_codes))


def _cleanup_mysql_rehearsal_pod(
    *, kube_context: str, namespace: str, name: str, runner: Runner | None
) -> None:
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "delete",
                "pod",
                name,
                "--namespace",
                namespace,
                "--ignore-not-found=true",
                "--wait=true",
            ),
            timeout_seconds=60,
        ),
        "mysql_rehearsal_cleanup",
    )


def _wait_for_mysql_rehearsal_completion(
    *, kube_context: str, namespace: str, name: str, runner: Runner | None
) -> dict[str, Any]:
    deadline = time.monotonic() + MYSQL_REHEARSAL_TIMEOUT_SECONDS
    while True:
        payload = _read_json(
            _execute(
                runner,
                _kubectl(
                    kube_context,
                    "get",
                    "pod",
                    name,
                    "--namespace",
                    namespace,
                    "-o",
                    "json",
                ),
                timeout_seconds=30,
            ),
            "mysql_rehearsal_status",
        )
        status = payload.get("status")
        phase = status.get("phase") if isinstance(status, dict) else None
        if phase in {"Succeeded", "Failed"}:
            return payload
        if phase not in {None, "Pending", "Running"}:
            raise TkeReleaseError("TKE MySQL rehearsal Pod status is invalid")
        if time.monotonic() >= deadline:
            raise TkeReleaseError("TKE MySQL rehearsal timed out")
        time.sleep(2)


def execute_mysql_rehearsal(
    *,
    namespace: str,
    release_sha: str,
    kube_context: str,
    backend_image: str,
    execute: bool,
    confirm_release_sha: str | None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Prove the candidate backend can issue a read-only production DB query.

    The transient Pod receives only ``AUTO_DB_URI_PROD`` by key reference,
    emits a bounded non-sensitive result, and is deleted regardless of success.
    It creates no Service, Ingress, persistent workload, schema migration, or
    business write.
    """
    if NAMESPACE.fullmatch(namespace) is None or SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("TKE MySQL rehearsal identity is invalid")
    if IMAGE_DIGEST.fullmatch(backend_image) is None:
        raise TkeReleaseError("TKE MySQL rehearsal backend image is invalid")
    _require_execute(execute, confirm_release_sha, release_sha)
    _verify_target_context(kube_context=kube_context, runner=runner)
    _read_runtime_secret_keys(
        kube_context=kube_context,
        namespace=namespace,
        runner=runner,
        required_keys=DATABASE_SECRET_KEYS,
    )
    pod = _mysql_rehearsal_pod(
        namespace=namespace,
        release_sha=release_sha,
        backend_image=backend_image,
    )
    name = pod["metadata"]["name"]
    rendered = yaml.safe_dump(pod, allow_unicode=True, sort_keys=False).encode("utf-8")
    create_attempted = False
    cleanup_error: TkeReleaseError | None = None
    try:
        create_attempted = True
        _require_success(
            _execute(
                runner,
                _kubectl(kube_context, "create", "-f", "-"),
                input_bytes=rendered,
                timeout_seconds=60,
            ),
            "mysql_rehearsal_create",
        )
        payload = _wait_for_mysql_rehearsal_completion(
            kube_context=kube_context,
            namespace=namespace,
            name=name,
            runner=runner,
        )
        if payload.get("status", {}).get("phase") == "Failed":
            failure_codes = _parse_mysql_rehearsal_failure_log(
                _require_success(
                    _execute(
                        runner,
                        _kubectl(
                            kube_context,
                            "logs",
                            name,
                            "--namespace",
                            namespace,
                            "--container",
                            "mysql-rehearsal",
                        ),
                        timeout_seconds=30,
                    ),
                    "mysql_rehearsal_failure_logs",
                )
            )
            raise TkeReleaseError(
                "TKE MySQL rehearsal failed: " + ",".join(failure_codes)
            )
        _validate_mysql_rehearsal_pod(
            payload=payload,
            expected_name=name,
            backend_image=backend_image,
        )
        result = _parse_mysql_rehearsal_log(
            _require_success(
                _execute(
                    runner,
                    _kubectl(
                        kube_context,
                        "logs",
                        name,
                        "--namespace",
                        namespace,
                        "--container",
                        "mysql-rehearsal",
                    ),
                    timeout_seconds=30,
                ),
                "mysql_rehearsal_logs",
            )
        )
    finally:
        if create_attempted:
            try:
                _cleanup_mysql_rehearsal_pod(
                    kube_context=kube_context,
                    namespace=namespace,
                    name=name,
                    runner=runner,
                )
            except TkeReleaseError as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error
    return {
        "status": "mysql_rehearsal_verified",
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "backendImage": backend_image,
        **result,
    }


def _require_missing_ingress(
    *, kube_context: str, namespace: str, runner: Runner | None
) -> None:
    result = _execute(
        runner,
        _kubectl(
            kube_context,
            "get",
            "ingress",
            "aiops",
            "--namespace",
            namespace,
            "-o",
            "json",
        ),
        timeout_seconds=30,
    )
    if result.returncode == 0:
        raise TkeReleaseError("failed candidate cleanup refuses a customer Ingress")
    stderr = result.stderr[:64 * 1024].decode("utf-8", errors="ignore").lower()
    if "not found" not in stderr and "notfound" not in stderr:
        raise TkeReleaseError(
            "failed_candidate_ingress_readback:" + _safe_error_category(result.stderr)
        )


def _read_optional_named_resource(
    *,
    kube_context: str,
    namespace: str,
    resource: str,
    name: str,
    action: str,
    runner: Runner | None,
) -> dict[str, Any] | None:
    """Read a named resource, distinguishing absence from an access failure."""
    result = _execute(
        runner,
        _kubectl(
            kube_context,
            "get",
            resource,
            name,
            "--namespace",
            namespace,
            "-o",
            "json",
        ),
        timeout_seconds=30,
    )
    if result.returncode == 0:
        return _read_json(result, action)
    stderr = result.stderr[:64 * 1024].decode("utf-8", errors="ignore").lower()
    if "not found" in stderr or "notfound" in stderr:
        return None
    raise TkeReleaseError(action + ":" + _safe_error_category(result.stderr))


def _require_public_entrypoints_exclude_slot(
    *, kube_context: str, namespace: str, slot: str, runner: Runner | None
) -> None:
    """Fail closed if a stable public Service could route to the cleanup slot."""
    for name in ("api", "frontend"):
        value = _read_optional_named_resource(
            kube_context=kube_context,
            namespace=namespace,
            resource="service",
            name=name,
            action=f"failed_candidate_stable_{name}_readback",
            runner=runner,
        )
        if value is None:
            continue
        metadata = value.get("metadata")
        selector = (
            value.get("spec", {}).get("selector")
            if isinstance(value.get("spec"), dict)
            else None
        )
        selected_slot = selector.get(SLOT_LABEL) if isinstance(selector, dict) else None
        if not isinstance(metadata, dict) or metadata.get("name") != name or not isinstance(selector, dict):
            raise TkeReleaseError("failed candidate cleanup stable Service identity mismatched")
        if selected_slot not in BLUE_GREEN_SLOTS:
            raise TkeReleaseError("failed candidate cleanup stable Service selector is invalid")
        if selected_slot == slot:
            raise TkeReleaseError("failed candidate cleanup stable Service targets cleanup slot")


def _ingress_backend_service_names(value: dict[str, Any]) -> set[str]:
    metadata = value.get("metadata")
    spec = value.get("spec")
    if not isinstance(metadata, dict) or metadata.get("name") != "aiops" or not isinstance(spec, dict):
        raise TkeReleaseError("failed candidate cleanup Ingress identity mismatched")
    backends: list[object] = []
    if "defaultBackend" in spec:
        backends.append(spec["defaultBackend"])
    rules = spec.get("rules")
    if rules is not None:
        if not isinstance(rules, list):
            raise TkeReleaseError("failed candidate cleanup Ingress identity mismatched")
        for rule in rules:
            http = rule.get("http") if isinstance(rule, dict) else None
            paths = http.get("paths") if isinstance(http, dict) else None
            if not isinstance(paths, list):
                raise TkeReleaseError("failed candidate cleanup Ingress identity mismatched")
            for path in paths:
                if not isinstance(path, dict) or "backend" not in path:
                    raise TkeReleaseError("failed candidate cleanup Ingress identity mismatched")
                backends.append(path["backend"])
    if not backends:
        raise TkeReleaseError("failed candidate cleanup Ingress identity mismatched")
    names: set[str] = set()
    for backend in backends:
        service = backend.get("service") if isinstance(backend, dict) else None
        name = service.get("name") if isinstance(service, dict) else None
        if not isinstance(name, str) or not name:
            raise TkeReleaseError("failed candidate cleanup Ingress identity mismatched")
        names.add(name)
    return names


def _require_ingress_excludes_slot(
    *, kube_context: str, namespace: str, slot: str, runner: Runner | None
) -> None:
    """An existing customer Ingress must stay behind stable public Services."""
    value = _read_optional_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="ingress",
        name="aiops",
        action="failed_candidate_ingress_readback",
        runner=runner,
    )
    if value is None:
        return
    names = _ingress_backend_service_names(value)
    target_services = set(_slot_service_names(slot))
    if names & target_services:
        raise TkeReleaseError("failed candidate cleanup Ingress targets cleanup slot")
    if not names <= {"api", "frontend"}:
        raise TkeReleaseError("failed candidate cleanup Ingress target is invalid")


def _require_slot_deployment_for_cleanup(
    *,
    kube_context: str,
    namespace: str,
    release_sha: str,
    component: str,
    slot: str,
    runner: Runner | None,
) -> None:
    name = _slot_name(component, slot)
    value = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="deployment",
        name=name,
        action=f"failed_candidate_{name}_readback",
        runner=runner,
    )
    metadata = value.get("metadata")
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    spec = value.get("spec")
    selector = spec.get("selector", {}).get("matchLabels") if isinstance(spec, dict) else None
    template_labels = (
        spec.get("template", {}).get("metadata", {}).get("labels")
        if isinstance(spec, dict)
        else None
    )
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != name
        or not isinstance(annotations, dict)
        or annotations.get("aiops.example.test/release-sha") != release_sha
        or not isinstance(labels, dict)
        or labels.get(SLOT_LABEL) != slot
        or not isinstance(selector, dict)
        or selector.get("app.kubernetes.io/name") != component
        or selector.get(SLOT_LABEL) != slot
        or not isinstance(template_labels, dict)
        or template_labels.get("app.kubernetes.io/name") != component
        or template_labels.get(SLOT_LABEL) != slot
    ):
        raise TkeReleaseError("failed candidate cleanup deployment identity mismatched")


def _require_slot_service_for_cleanup(
    *,
    kube_context: str,
    namespace: str,
    component: str,
    slot: str,
    runner: Runner | None,
) -> None:
    name = _slot_name("api" if component == "backend" else "frontend", slot)
    value = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="service",
        name=name,
        action=f"failed_candidate_{name}_readback",
        runner=runner,
    )
    metadata = value.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    selector = (
        value.get("spec", {}).get("selector")
        if isinstance(value.get("spec"), dict)
        else None
    )
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != name
        or not isinstance(labels, dict)
        or labels.get(SLOT_LABEL) != slot
        or not isinstance(selector, dict)
        or selector.get("app.kubernetes.io/name") != component
        or selector.get(SLOT_LABEL) != slot
    ):
        raise TkeReleaseError("failed candidate cleanup Service identity mismatched")


def _require_slot_pdb_for_cleanup(
    *,
    kube_context: str,
    namespace: str,
    component: str,
    slot: str,
    runner: Runner | None,
) -> None:
    name = _slot_name(component, slot)
    value = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="poddisruptionbudget",
        name=name,
        action=f"failed_candidate_{name}_readback",
        runner=runner,
    )
    metadata = value.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    selector = (
        value.get("spec", {}).get("selector", {}).get("matchLabels")
        if isinstance(value.get("spec"), dict)
        else None
    )
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != name
        or not isinstance(labels, dict)
        or labels.get(SLOT_LABEL) != slot
        or not isinstance(selector, dict)
        or selector.get("app.kubernetes.io/name") != component
        or selector.get(SLOT_LABEL) != slot
    ):
        raise TkeReleaseError("failed candidate cleanup PDB identity mismatched")


def _require_slot_release_configmap_for_cleanup(
    *,
    kube_context: str,
    namespace: str,
    release_sha: str,
    slot: str,
    runner: Runner | None,
) -> None:
    name = f"{RELEASE_CONFIGMAP_NAME}-{slot}"
    value = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="configmap",
        name=name,
        action=f"failed_candidate_{name}_readback",
        runner=runner,
    )
    metadata = value.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    data = value.get("data")
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != name
        or not isinstance(labels, dict)
        or labels.get(SLOT_LABEL) != slot
        or not isinstance(data, dict)
        or data.get("RELEASE_SHA") != release_sha
    ):
        raise TkeReleaseError("failed candidate cleanup release ConfigMap identity mismatched")


def _require_legacy_pdb_for_cleanup(
    *,
    kube_context: str,
    namespace: str,
    component: str,
    runner: Runner | None,
) -> None:
    value = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="poddisruptionbudget",
        name=component,
        action=f"failed_candidate_{component}_pdb_readback",
        runner=runner,
    )
    metadata = value.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    selector = (
        value.get("spec", {}).get("selector", {}).get("matchLabels")
        if isinstance(value.get("spec"), dict)
        else None
    )
    expected_selector = {
        "app.kubernetes.io/managed-by": "kustomize",
        "app.kubernetes.io/name": component,
        "app.kubernetes.io/part-of": "aiops",
    }
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != component
        or not isinstance(labels, dict)
        or labels.get("app.kubernetes.io/managed-by") != "kustomize"
        or labels.get("app.kubernetes.io/part-of") != "aiops"
        or SLOT_LABEL in labels
        or selector != expected_selector
    ):
        raise TkeReleaseError("failed candidate cleanup legacy PDB identity mismatched")


def _require_legacy_release_configmap_for_cleanup(
    *,
    kube_context: str,
    namespace: str,
    release_sha: str,
    runner: Runner | None,
) -> None:
    value = _read_named_resource(
        kube_context=kube_context,
        namespace=namespace,
        resource="configmap",
        name=RELEASE_CONFIGMAP_NAME,
        action="failed_candidate_legacy_release_configmap_readback",
        runner=runner,
    )
    metadata = value.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    data = value.get("data")
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != RELEASE_CONFIGMAP_NAME
        or not isinstance(labels, dict)
        or labels.get("app.kubernetes.io/managed-by") != "kustomize"
        or labels.get("app.kubernetes.io/part-of") != "aiops"
        or SLOT_LABEL in labels
        or not isinstance(data, dict)
        or data.get("RELEASE_SHA") != release_sha
    ):
        raise TkeReleaseError("failed candidate cleanup legacy release ConfigMap identity mismatched")


def cleanup_failed_candidate(
    *,
    namespace: str,
    release_sha: str,
    kube_context: str,
    execute: bool,
    confirm_release_sha: str | None,
    legacy_single_slot: bool,
    slot: str | None = None,
    state_path: Path | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Delete only a proven failed candidate without shared-resource cleanup."""
    if NAMESPACE.fullmatch(namespace) is None or SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("failed candidate cleanup identity is invalid")
    _require_execute(execute, confirm_release_sha, release_sha)
    _verify_target_context(kube_context=kube_context, runner=runner)
    if legacy_single_slot is True:
        if slot is not None or state_path is not None:
            raise TkeReleaseError("legacy failed candidate cleanup scope is invalid")
        # This narrowly scoped bridge exists solely to remove the pre-blue-green
        # rehearsal objects that used the un-suffixed names.  It never lists or
        # deletes shared runtime materials, and it refuses to run while an Ingress
        # exists because that would make a Service deletion customer-visible.
        _require_missing_ingress(
            kube_context=kube_context,
            namespace=namespace,
            runner=runner,
        )
        deployments = ("backend", "frontend", "scheduler")
        services = (("api", "backend"), ("frontend", "frontend"))
        deployment_values = {
            name: _read_optional_named_resource(
                kube_context=kube_context,
                namespace=namespace,
                resource="deployment",
                name=name,
                action=f"failed_candidate_{name}_readback",
                runner=runner,
            )
            for name in deployments
        }
        service_values = {
            name: _read_optional_named_resource(
                kube_context=kube_context,
                namespace=namespace,
                resource="service",
                name=name,
                action=f"failed_candidate_{name}_readback",
                runner=runner,
            )
            for name, _component in services
        }
        workload_values = [*deployment_values.values(), *service_values.values()]
        workload_present = all(value is not None for value in workload_values)
        workload_absent = all(value is None for value in workload_values)
        if not workload_present and not workload_absent:
            raise TkeReleaseError("failed candidate cleanup legacy candidate resource set is partial")
        if workload_present:
            for name, value in deployment_values.items():
                if value is None:
                    raise TkeReleaseError(
                        "failed candidate cleanup legacy candidate resource set is partial"
                    )
                metadata = value.get("metadata")
                annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
                labels = metadata.get("labels") if isinstance(metadata, dict) else None
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("name") != name
                    or not isinstance(annotations, dict)
                    or annotations.get("aiops.example.test/release-sha") != release_sha
                    or (isinstance(labels, dict) and SLOT_LABEL in labels)
                ):
                    raise TkeReleaseError("failed candidate cleanup deployment identity mismatched")
            for name, component in services:
                value = service_values[name]
                if value is None:
                    raise TkeReleaseError(
                        "failed candidate cleanup legacy candidate resource set is partial"
                    )
                metadata = value.get("metadata")
                selector = (
                    value.get("spec", {}).get("selector")
                    if isinstance(value.get("spec"), dict)
                    else None
                )
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("name") != name
                    or not isinstance(selector, dict)
                    or selector.get("app.kubernetes.io/name") != component
                    or SLOT_LABEL in selector
                ):
                    raise TkeReleaseError("failed candidate cleanup Service identity mismatched")
        for component in deployments:
            _require_legacy_pdb_for_cleanup(
                kube_context=kube_context,
                namespace=namespace,
                component=component,
                runner=runner,
            )
        _require_legacy_release_configmap_for_cleanup(
            kube_context=kube_context,
            namespace=namespace,
            release_sha=release_sha,
            runner=runner,
        )
        if workload_present:
            _require_success(
                _execute(
                    runner,
                    _kubectl(
                        kube_context,
                        "delete",
                        "deployment",
                        *deployments,
                        "--namespace",
                        namespace,
                        "--wait=true",
                    ),
                    timeout_seconds=ROLL_OUT_TIMEOUT_SECONDS,
                ),
                "failed_candidate_deployment_delete",
            )
            _require_success(
                _execute(
                    runner,
                    _kubectl(
                        kube_context,
                        "delete",
                        "service",
                        *(name for name, _component in services),
                        "--namespace",
                        namespace,
                        "--wait=true",
                    ),
                    timeout_seconds=60,
                ),
                "failed_candidate_service_delete",
            )
        _require_success(
            _execute(
                runner,
                _kubectl(
                    kube_context,
                    "delete",
                    "poddisruptionbudget",
                    *deployments,
                    "--namespace",
                    namespace,
                    "--wait=true",
                ),
                timeout_seconds=60,
            ),
            "failed_candidate_pdb_delete",
        )
        _require_success(
            _execute(
                runner,
                _kubectl(
                    kube_context,
                    "delete",
                    "configmap",
                    RELEASE_CONFIGMAP_NAME,
                    "--namespace",
                    namespace,
                    "--wait=true",
                ),
                timeout_seconds=60,
            ),
            "failed_candidate_release_configmap_delete",
        )
        deleted: list[str] = []
        if workload_present:
            deleted.extend(f"deployment/{name}" for name in deployments)
            deleted.extend(f"service/{name}" for name, _component in services)
        deleted.extend(f"poddisruptionbudget/{name}" for name in deployments)
        deleted.append(f"configmap/{RELEASE_CONFIGMAP_NAME}")
        return {
            "status": "candidate_cleaned",
            "releaseSha": release_sha,
            "namespace": namespace,
            "kubeContext": kube_context,
            "targetCluster": TARGET_CLUSTER_ID,
            "legacySingleSlot": True,
            "deleted": deleted,
        }
    if legacy_single_slot is not False or slot is None or state_path is None:
        raise TkeReleaseError("failed candidate cleanup blue-green scope is invalid")
    slot = _validate_slot(slot)
    active = _active_release_record(state_path=state_path)
    if active is not None and active["slot"] == slot:
        raise TkeReleaseError("failed candidate cleanup refuses the recorded active slot")
    _require_public_entrypoints_exclude_slot(
        kube_context=kube_context,
        namespace=namespace,
        slot=slot,
        runner=runner,
    )
    _require_ingress_excludes_slot(
        kube_context=kube_context,
        namespace=namespace,
        slot=slot,
        runner=runner,
    )
    for component in ("backend", "frontend", "scheduler"):
        _require_slot_deployment_for_cleanup(
            kube_context=kube_context,
            namespace=namespace,
            release_sha=release_sha,
            component=component,
            slot=slot,
            runner=runner,
        )
        _require_slot_pdb_for_cleanup(
            kube_context=kube_context,
            namespace=namespace,
            component=component,
            slot=slot,
            runner=runner,
        )
    for component in ("backend", "frontend"):
        _require_slot_service_for_cleanup(
            kube_context=kube_context,
            namespace=namespace,
            component=component,
            slot=slot,
            runner=runner,
        )
    _require_slot_release_configmap_for_cleanup(
        kube_context=kube_context,
        namespace=namespace,
        release_sha=release_sha,
        slot=slot,
        runner=runner,
    )
    deployments = _slot_deployment_names(slot)
    services = _slot_service_names(slot)
    pdbs = deployments
    release_configmap = f"{RELEASE_CONFIGMAP_NAME}-{slot}"
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "delete",
                "deployment",
                *deployments,
                "--namespace",
                namespace,
                "--wait=true",
            ),
            timeout_seconds=ROLL_OUT_TIMEOUT_SECONDS,
        ),
        "failed_candidate_deployment_delete",
    )
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "delete",
                "service",
                *services,
                "--namespace",
                namespace,
                "--wait=true",
            ),
            timeout_seconds=60,
        ),
        "failed_candidate_service_delete",
    )
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "delete",
                "poddisruptionbudget",
                *pdbs,
                "--namespace",
                namespace,
                "--wait=true",
            ),
            timeout_seconds=60,
        ),
        "failed_candidate_pdb_delete",
    )
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "delete",
                "configmap",
                release_configmap,
                "--namespace",
                namespace,
                "--wait=true",
            ),
            timeout_seconds=60,
        ),
        "failed_candidate_release_configmap_delete",
    )
    return {
        "status": "candidate_cleaned",
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "legacySingleSlot": False,
        "slot": slot,
        "deleted": [
            *(f"deployment/{name}" for name in deployments),
            *(f"service/{name}" for name in services),
            *(f"poddisruptionbudget/{name}" for name in pdbs),
            f"configmap/{release_configmap}",
        ],
    }


def _deployment_identity(value: dict[str, Any], release_sha: str, name: str) -> None:
    metadata = value.get("metadata")
    status = value.get("status")
    spec = value.get("spec")
    if not isinstance(metadata, dict) or metadata.get("name") != name:
        raise TkeReleaseError("workload acceptance identity is invalid")
    annotations = metadata.get("annotations")
    if not isinstance(annotations, dict) or annotations.get("aiops.example.test/release-sha") != release_sha:
        raise TkeReleaseError("workload acceptance release identity mismatched")
    if not isinstance(status, dict) or not isinstance(spec, dict):
        raise TkeReleaseError("workload acceptance status is invalid")
    replicas = spec.get("replicas")
    if not isinstance(replicas, int) or replicas < 1:
        raise TkeReleaseError("workload acceptance replicas are invalid")
    for field in ("readyReplicas", "updatedReplicas", "availableReplicas"):
        if status.get(field) != replicas:
            raise TkeReleaseError("workload acceptance rollout is incomplete")


def _workload_container_image(
    value: dict[str, Any], *, name: str, container_name: str
) -> str:
    try:
        containers = value["spec"]["template"]["spec"]["containers"]
        container = next(
            item
            for item in containers
            if isinstance(item, dict) and item.get("name") == container_name
        )
        image = container["image"]
    except (KeyError, TypeError, StopIteration) as exc:
        raise TkeReleaseError("workload acceptance image is invalid") from exc
    if not isinstance(image, str) or IMAGE_DIGEST.fullmatch(image) is None:
        raise TkeReleaseError("workload acceptance image is invalid")
    return image


def _workload_scheduler_mode(value: dict[str, Any]) -> str:
    try:
        containers = value["spec"]["template"]["spec"]["containers"]
        scheduler = next(
            item
            for item in containers
            if isinstance(item, dict) and item.get("name") == "scheduler"
        )
        environment = scheduler["env"]
    except (KeyError, TypeError, StopIteration) as exc:
        raise TkeReleaseError("workload acceptance Scheduler mode is invalid") from exc
    values = {
        item.get("name"): item.get("value")
        for item in environment
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    mode = values.get("AIOPS_SCHEDULER_MODE")
    expected = values.get("SCHEDULER_EXPECTED_MODE")
    if mode not in {"active", "standby"} or expected != mode:
        raise TkeReleaseError("workload acceptance Scheduler mode is invalid")
    return mode


def _pod_list_for_component(
    *,
    kube_context: str,
    namespace: str,
    component: str,
    slot: str | None = None,
    runner: Runner | None,
) -> list[dict[str, Any]]:
    selector = f"app.kubernetes.io/name={component}"
    if slot is not None:
        selector = selector + "," + SLOT_LABEL + "=" + _validate_slot(slot)
    payload = _read_json(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "get",
                "pods",
                "--namespace",
                namespace,
                "--selector",
                selector,
                "-o",
                "json",
            ),
            timeout_seconds=60,
        ),
        f"pod_{component}_readback",
    )
    items = payload.get("items")
    if not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items):
        raise TkeReleaseError("workload acceptance Pod is missing")
    # rollout may complete while replaced Pods are still draining.
    return [item for item in items if not item.get("metadata", {}).get("deletionTimestamp")]


def _pod_container_identity(
    *,
    pod: dict[str, Any],
    container_name: str,
    expected_image: str,
) -> None:
    status = pod.get("status")
    statuses = status.get("containerStatuses") if isinstance(status, dict) else None
    if not isinstance(statuses, list):
        raise TkeReleaseError("workload acceptance Pod status is invalid")
    container = next(
        (
            item
            for item in statuses
            if isinstance(item, dict) and item.get("name") == container_name
        ),
        None,
    )
    reported_image = container.get("image") if isinstance(container, dict) else None
    image_id = container.get("imageID") if isinstance(container, dict) else None
    expected_digest = expected_image.rsplit("@", 1)[1]
    digest_only_image_ids = {
        expected_digest,
        f"docker-pullable://{expected_digest}",
        f"containerd://{expected_digest}",
    }
    is_runtime_config_digest = (
        isinstance(reported_image, str)
        and reported_image.startswith("sha256:")
        and SHA64.fullmatch(reported_image.removeprefix("sha256:")) is not None
    )
    if (
        not isinstance(container, dict)
        or container.get("ready") is not True
        or (reported_image != expected_image and not is_runtime_config_digest)
        or not isinstance(image_id, str)
        or (
            image_id
            and not image_id.endswith(expected_image)
            and image_id not in digest_only_image_ids
        )
    ):
        raise TkeReleaseError("workload acceptance Pod image identity is invalid")


def _service_proxy_json(
    *,
    kube_context: str,
    namespace: str,
    service: str,
    port: int,
    path: str,
    action: str,
    runner: Runner | None,
) -> dict[str, Any]:
    return _read_json(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "get",
                "--raw",
                f"/api/v1/namespaces/{namespace}/services/http:{service}:{port}/proxy{path}",
            ),
            timeout_seconds=60,
        ),
        action,
    )


def accept_deploy(
    *,
    namespace: str,
    release_sha: str,
    kube_context: str,
    expected_scheduler_mode: str | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Read workload status and endpoint existence; never changes cluster state."""
    if NAMESPACE.fullmatch(namespace) is None or SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("TKE acceptance identity is invalid")
    if expected_scheduler_mode not in {None, "active", "standby"}:
        raise TkeReleaseError("TKE acceptance Scheduler mode is invalid")
    _verify_target_context(kube_context=kube_context, runner=runner)
    workloads = _read_json(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "get",
                "deployments",
                "--namespace",
                namespace,
                "-o",
                "json",
            ),
            timeout_seconds=60,
        ),
        "workload_readback",
    )
    items = workloads.get("items")
    if not isinstance(items, list):
        raise TkeReleaseError("workload acceptance status is invalid")
    by_name = {
        item.get("metadata", {}).get("name"): item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("metadata"), dict)
    }
    expected_images: dict[str, str] = {}
    scheduler_mode: str | None = None
    for name in ("backend", "frontend", "scheduler"):
        item = by_name.get(name)
        if not isinstance(item, dict):
            raise TkeReleaseError("workload acceptance workload is missing")
        _deployment_identity(item, release_sha, name)
        expected_images[name] = _workload_container_image(
            item,
            name=name,
            container_name=name,
        )
        if name == "scheduler":
            scheduler_mode = _workload_scheduler_mode(item)
    if scheduler_mode != expected_scheduler_mode and expected_scheduler_mode is not None:
        raise TkeReleaseError("workload acceptance Scheduler mode mismatched")
    if expected_images["backend"] != expected_images["scheduler"]:
        raise TkeReleaseError("workload acceptance backend Scheduler image mismatched")
    for service in ("api", "frontend"):
        endpoint = _read_json(
            _execute(
                runner,
                _kubectl(
                    kube_context,
                    "get",
                    "endpoints",
                    service,
                    "--namespace",
                    namespace,
                    "-o",
                    "json",
                ),
                timeout_seconds=30,
            ),
            f"endpoint_{service}",
        )
        subsets = endpoint.get("subsets")
        if not isinstance(subsets, list) or not subsets:
            raise TkeReleaseError("workload acceptance endpoint is empty")
    for component in ("backend", "frontend", "scheduler"):
        pods = _pod_list_for_component(
            kube_context=kube_context,
            namespace=namespace,
            component=component,
            runner=runner,
        )
        expected_replicas = by_name[component]["spec"]["replicas"]
        if len(pods) != expected_replicas:
            raise TkeReleaseError("workload acceptance Pod count is invalid")
        for pod in pods:
            _pod_container_identity(
                pod=pod,
                container_name=component,
                expected_image=expected_images[component],
            )
            sidecars = pod.get("spec", {}).get("containers", [])
            for sidecar in sidecars:
                if sidecar.get("name") == "pi-runtime":
                    _pod_container_identity(pod=pod, container_name="pi-runtime", expected_image=expected_images[component])
    api_live = _service_proxy_json(
        kube_context=kube_context,
        namespace=namespace,
        service="api",
        port=8000,
        path="/api/health/live",
        action="api_live_readback",
        runner=runner,
    )
    if api_live.get("live") is not True:
        raise TkeReleaseError("workload acceptance API live probe failed")
    api_startup = _service_proxy_json(
        kube_context=kube_context,
        namespace=namespace,
        service="api",
        port=8000,
        path="/api/health/startup",
        action="api_startup_readback",
        runner=runner,
    )
    if api_startup.get("ready") is not True:
        raise TkeReleaseError("workload acceptance API startup probe failed")
    api_ready = _service_proxy_json(
        kube_context=kube_context,
        namespace=namespace,
        service="api",
        port=8000,
        path="/api/health/ready",
        action="api_ready_readback",
        runner=runner,
    )
    if api_ready.get("ready") is not True or api_ready.get("releaseSha") != release_sha:
        raise TkeReleaseError("workload acceptance API readiness identity failed")
    frontend_release = _service_proxy_json(
        kube_context=kube_context,
        namespace=namespace,
        service="frontend",
        port=80,
        path="/release-info.json",
        action="frontend_release_readback",
        runner=runner,
    )
    if frontend_release.get("releaseSha") != release_sha:
        raise TkeReleaseError("workload acceptance Frontend release identity failed")
    frontend_api_ready = _service_proxy_json(
        kube_context=kube_context,
        namespace=namespace,
        service="frontend",
        port=80,
        path="/api/health/ready",
        action="frontend_api_ready_readback",
        runner=runner,
    )
    if (
        frontend_api_ready.get("ready") is not True
        or frontend_api_ready.get("releaseSha") != release_sha
    ):
        raise TkeReleaseError("workload acceptance Frontend API proxy failed")
    return {
        "status": "accepted",
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "workloads": ["backend", "frontend", "scheduler"],
        "endpoints": ["api", "frontend"],
        "schedulerMode": scheduler_mode,
        "images": {
            "backend": expected_images["backend"],
            "frontend": expected_images["frontend"],
        },
    }


def _slot_deployment_names(slot: str) -> tuple[str, str, str]:
    return tuple(
        _slot_name(component, slot)
        for component in ("backend", "frontend", "scheduler")
    )


def _slot_service_names(slot: str) -> tuple[str, str]:
    return (_slot_name("api", slot), _slot_name("frontend", slot))


def accept_slot_deploy(
    *,
    namespace: str,
    release_sha: str,
    kube_context: str,
    slot: str,
    expected_scheduler_mode: str,
    include_public_entrypoints: bool,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Read back one blue-green slot without relying on the public entrypoint."""
    if NAMESPACE.fullmatch(namespace) is None or SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("blue-green acceptance identity is invalid")
    slot = _validate_slot(slot)
    if expected_scheduler_mode not in {"active", "standby"}:
        raise TkeReleaseError("blue-green acceptance Scheduler mode is invalid")
    if not isinstance(include_public_entrypoints, bool):
        raise TkeReleaseError("blue-green acceptance public entrypoint mode is invalid")
    _verify_target_context(kube_context=kube_context, runner=runner)
    workloads = _read_json(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "get",
                "deployments",
                "--namespace",
                namespace,
                "-o",
                "json",
            ),
            timeout_seconds=60,
        ),
        "blue_green_workload_readback",
    )
    items = workloads.get("items")
    if not isinstance(items, list):
        raise TkeReleaseError("blue-green acceptance status is invalid")
    by_name = {
        item.get("metadata", {}).get("name"): item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("metadata"), dict)
    }
    expected_images: dict[str, str] = {}
    for component in ("backend", "frontend", "scheduler"):
        name = _slot_name(component, slot)
        item = by_name.get(name)
        if not isinstance(item, dict):
            raise TkeReleaseError("blue-green acceptance workload is missing")
        _deployment_identity(item, release_sha, name)
        labels = item.get("metadata", {}).get("labels")
        template_labels = item.get("spec", {}).get("template", {}).get("metadata", {}).get("labels")
        if (
            not isinstance(labels, dict)
            or labels.get(SLOT_LABEL) != slot
            or not isinstance(template_labels, dict)
            or template_labels.get(SLOT_LABEL) != slot
        ):
            raise TkeReleaseError("blue-green acceptance workload slot mismatched")
        expected_images[component] = _workload_container_image(
            item,
            name=name,
            container_name=component,
        )
    scheduler_mode = _workload_scheduler_mode(by_name[_slot_name("scheduler", slot)])
    if scheduler_mode != expected_scheduler_mode:
        raise TkeReleaseError("blue-green acceptance Scheduler mode mismatched")
    if expected_images["backend"] != expected_images["scheduler"]:
        raise TkeReleaseError("blue-green acceptance backend Scheduler image mismatched")
    services = list(_slot_service_names(slot))
    if include_public_entrypoints:
        services.extend(("api", "frontend"))
    for service in services:
        endpoint = _read_json(
            _execute(
                runner,
                _kubectl(
                    kube_context,
                    "get",
                    "endpoints",
                    service,
                    "--namespace",
                    namespace,
                    "-o",
                    "json",
                ),
                timeout_seconds=30,
            ),
            f"blue_green_endpoint_{service}",
        )
        subsets = endpoint.get("subsets")
        if not isinstance(subsets, list) or not subsets:
            raise TkeReleaseError("blue-green acceptance endpoint is empty")
    for component in ("backend", "frontend", "scheduler"):
        pods = _pod_list_for_component(
            kube_context=kube_context,
            namespace=namespace,
            component=component,
            slot=slot,
            runner=runner,
        )
        expected_replicas = by_name[_slot_name(component, slot)]["spec"]["replicas"]
        if len(pods) != expected_replicas:
            raise TkeReleaseError("blue-green acceptance Pod count is invalid")
        for pod in pods:
            _pod_container_identity(
                pod=pod,
                container_name=component,
                expected_image=expected_images[component],
            )
            sidecars = pod.get("spec", {}).get("containers", [])
            for sidecar in sidecars:
                if sidecar.get("name") == "pi-runtime":
                    _pod_container_identity(pod=pod, container_name="pi-runtime", expected_image=expected_images[component])
    api_service, frontend_service = _slot_service_names(slot)
    api_live = _service_proxy_json(
        kube_context=kube_context,
        namespace=namespace,
        service=api_service,
        port=8000,
        path="/api/health/live",
        action="blue_green_api_live_readback",
        runner=runner,
    )
    if api_live.get("live") is not True:
        raise TkeReleaseError("blue-green acceptance API live probe failed")
    api_startup = _service_proxy_json(
        kube_context=kube_context,
        namespace=namespace,
        service=api_service,
        port=8000,
        path="/api/health/startup",
        action="blue_green_api_startup_readback",
        runner=runner,
    )
    if api_startup.get("ready") is not True:
        raise TkeReleaseError("blue-green acceptance API startup probe failed")
    api_ready = _service_proxy_json(
        kube_context=kube_context,
        namespace=namespace,
        service=api_service,
        port=8000,
        path="/api/health/ready",
        action="blue_green_api_ready_readback",
        runner=runner,
    )
    if api_ready.get("ready") is not True or api_ready.get("releaseSha") != release_sha:
        raise TkeReleaseError("blue-green acceptance API readiness identity failed")
    frontend_release = _service_proxy_json(
        kube_context=kube_context,
        namespace=namespace,
        service=frontend_service,
        port=80,
        path="/release-info.json",
        action="blue_green_frontend_release_readback",
        runner=runner,
    )
    if frontend_release.get("releaseSha") != release_sha:
        raise TkeReleaseError("blue-green acceptance Frontend release identity failed")
    frontend_api_ready = _service_proxy_json(
        kube_context=kube_context,
        namespace=namespace,
        service=frontend_service,
        port=80,
        path="/api/health/ready",
        action="blue_green_frontend_api_ready_readback",
        runner=runner,
    )
    if (
        frontend_api_ready.get("ready") is not True
        or frontend_api_ready.get("releaseSha") != release_sha
    ):
        raise TkeReleaseError("blue-green acceptance Frontend API proxy failed")
    return {
        "status": "accepted",
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "slot": slot,
        "workloads": list(_slot_deployment_names(slot)),
        "endpoints": services,
        "schedulerMode": scheduler_mode,
        "images": {
            "backend": expected_images["backend"],
            "frontend": expected_images["frontend"],
        },
    }


def execute_slot_deploy(
    *,
    manifests_dir: Path,
    namespace: str,
    release_sha: str,
    kube_context: str,
    backend_image: str,
    frontend_image: str,
    schema_release: str,
    artifact_manifest_sha256: str,
    slot: str,
    scheduler_mode: str,
    include_public_entrypoints: bool,
    execute: bool,
    confirm_release_sha: str | None,
    authorization_release_sha: str | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Apply, roll out and verify one slot after all immutable gates pass."""
    authorized_release = release_sha if authorization_release_sha is None else authorization_release_sha
    if SHA40.fullmatch(authorized_release) is None:
        raise TkeReleaseError("TKE blue-green authorization release SHA is invalid")
    _require_execute(execute, confirm_release_sha, authorized_release)
    slot = _validate_slot(slot)
    if scheduler_mode not in {"active", "standby"}:
        raise TkeReleaseError("TKE Scheduler mode is invalid")
    _verify_target_context(kube_context=kube_context, runner=runner)
    rendered = render_slot_manifests(
        manifests_dir=manifests_dir,
        namespace=namespace,
        release_sha=release_sha,
        backend_image=backend_image,
        frontend_image=frontend_image,
        schema_release=schema_release,
        artifact_manifest_sha256=artifact_manifest_sha256,
        slot=slot,
        scheduler_mode=scheduler_mode,
        include_public_entrypoints=include_public_entrypoints,
        runner=runner,
    )
    _server_side_dry_run(
        rendered=rendered,
        kube_context=kube_context,
        runner=runner,
        action="blue_green_server_dry_run",
    )
    _apply_runtime_render(
        rendered=rendered,
        kube_context=kube_context,
        runner=runner,
        action="blue_green_apply",
    )
    deployments = _slot_deployment_names(slot)
    for deployment in deployments:
        _require_success(
            _execute(
                runner,
                _kubectl(
                    kube_context,
                    "rollout",
                    "status",
                    f"deployment/{deployment}",
                    "--namespace",
                    namespace,
                    "--timeout",
                    "10m",
                ),
                timeout_seconds=ROLL_OUT_TIMEOUT_SECONDS,
            ),
            f"blue_green_rollout_{deployment}",
        )
    acceptance = accept_slot_deploy(
        namespace=namespace,
        release_sha=release_sha,
        kube_context=kube_context,
        slot=slot,
        expected_scheduler_mode=scheduler_mode,
        include_public_entrypoints=include_public_entrypoints,
        runner=runner,
    )
    if acceptance.get("status") != "accepted":
        raise TkeReleaseError("blue-green deployment acceptance failed")
    return {
        "status": "candidate_deployed" if scheduler_mode == "standby" else "deployed",
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "slot": slot,
        "manifestSha256": _manifest_sha256(rendered),
        "deployments": list(deployments),
        "services": list(_slot_service_names(slot)),
        "schedulerMode": scheduler_mode,
    }


def _active_release_record(*, state_path: Path) -> dict[str, Any] | None:
    with _locked_release_state(state_path) as locked_path:
        state = _read_release_state(locked_path)
    active = state["active"]
    return None if active is None else _validate_release_record(active)


def read_active_release(*, state_path: Path) -> dict[str, Any]:
    """Read the immutable active slot record without consulting mutable labels."""
    active = _active_release_record(state_path=state_path)
    if active is None:
        raise TkeReleaseError("recorded active TKE blue-green release target is missing")
    return active


def _set_slot_scheduler_replicas(
    *,
    kube_context: str,
    namespace: str,
    slot: str,
    replicas: int,
    runner: Runner | None,
    action: str,
) -> None:
    slot = _validate_slot(slot)
    if replicas not in {0, 1}:
        raise TkeReleaseError("blue-green Scheduler replica target is invalid")
    name = _slot_name("scheduler", slot)
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "scale",
                f"deployment/{name}",
                f"--replicas={replicas}",
                "--namespace",
                namespace,
            ),
            timeout_seconds=60,
        ),
        action + "_scale",
    )
    _require_success(
        _execute(
            runner,
            _kubectl(
                kube_context,
                "rollout",
                "status",
                f"deployment/{name}",
                "--namespace",
                namespace,
                "--timeout",
                "10m",
            ),
            timeout_seconds=ROLL_OUT_TIMEOUT_SECONDS,
        ),
        action + "_rollout",
    )


def execute_slot_activate(
    *,
    manifests_dir: Path,
    namespace: str,
    release_sha: str,
    kube_context: str,
    state_path: Path,
    backend_image: str,
    frontend_image: str,
    schema_release: str,
    artifact_manifest_sha256: str,
    slot: str,
    execute: bool,
    confirm_release_sha: str | None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Promote a verified standby slot while keeping exactly one Scheduler active."""
    _require_execute(execute, confirm_release_sha, release_sha)
    slot = _validate_slot(slot)
    _verify_target_context(kube_context=kube_context, runner=runner)
    previous = _active_release_record(state_path=state_path)
    if previous is not None and previous["slot"] == slot:
        raise TkeReleaseError("blue-green candidate slot is already active")
    previous_scheduler_stopped = False
    try:
        if previous is not None:
            _set_slot_scheduler_replicas(
                kube_context=kube_context,
                namespace=namespace,
                slot=previous["slot"],
                replicas=0,
                runner=runner,
                action="blue_green_previous_scheduler_stop",
            )
            previous_scheduler_stopped = True
        deployed = execute_slot_deploy(
            manifests_dir=manifests_dir,
            namespace=namespace,
            release_sha=release_sha,
            kube_context=kube_context,
            backend_image=backend_image,
            frontend_image=frontend_image,
            schema_release=schema_release,
            artifact_manifest_sha256=artifact_manifest_sha256,
            slot=slot,
            scheduler_mode="active",
            include_public_entrypoints=True,
            execute=execute,
            confirm_release_sha=confirm_release_sha,
            runner=runner,
        )
        if deployed.get("status") != "deployed":
            raise TkeReleaseError("blue-green activation deployment failed")
    except Exception as exc:
        if previous is not None and previous_scheduler_stopped:
            try:
                _set_slot_scheduler_replicas(
                    kube_context=kube_context,
                    namespace=namespace,
                    slot=previous["slot"],
                    replicas=1,
                    runner=runner,
                    action="blue_green_previous_scheduler_restore",
                )
            except TkeReleaseError as restore_error:
                raise TkeReleaseError(
                    "blue-green activation failed and previous Scheduler restore failed"
                ) from restore_error
        if isinstance(exc, TkeReleaseError):
            raise
        raise TkeReleaseError("blue-green activation deployment failed") from exc
    record = {
        "artifactManifestSha256": artifact_manifest_sha256,
        "backendImage": backend_image,
        "frontendImage": frontend_image,
        "manifestSha256": deployed["manifestSha256"],
        "releaseSha": release_sha,
        "schemaRelease": schema_release,
        "slot": slot,
    }
    _record_active_release(state_path=state_path, record=record)
    return {
        "status": "activated",
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "slot": slot,
        "previousSlot": None if previous is None else previous["slot"],
        "manifestSha256": deployed["manifestSha256"],
    }


def execute_slot_rollback(
    *,
    manifests_dir: Path,
    namespace: str,
    release_sha: str,
    kube_context: str,
    state_path: Path,
    execute: bool,
    confirm_release_sha: str | None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Restore only the prior immutable slot recorded by a successful activation."""
    _require_execute(execute, confirm_release_sha, release_sha)
    _verify_target_context(kube_context=kube_context, runner=runner)
    current = _active_release_record(state_path=state_path)
    if current is None or current["releaseSha"] != release_sha:
        raise TkeReleaseError("recorded active TKE blue-green release target is missing")
    previous = _recorded_rollback_target(
        state_path=state_path,
        release_sha=release_sha,
    )
    if previous["slot"] == current["slot"]:
        raise TkeReleaseError("recorded blue-green rollback slot is invalid")
    current_scheduler_stopped = False
    try:
        _set_slot_scheduler_replicas(
            kube_context=kube_context,
            namespace=namespace,
            slot=current["slot"],
            replicas=0,
            runner=runner,
            action="blue_green_current_scheduler_stop",
        )
        current_scheduler_stopped = True
        deployed = execute_slot_deploy(
            manifests_dir=manifests_dir,
            namespace=namespace,
            release_sha=previous["releaseSha"],
            kube_context=kube_context,
            backend_image=previous["backendImage"],
            frontend_image=previous["frontendImage"],
            schema_release=previous["schemaRelease"],
            artifact_manifest_sha256=previous["artifactManifestSha256"],
            slot=previous["slot"],
            scheduler_mode="active",
            include_public_entrypoints=True,
            execute=execute,
            confirm_release_sha=release_sha,
            authorization_release_sha=release_sha,
            runner=runner,
        )
        if deployed.get("status") != "deployed":
            raise TkeReleaseError("blue-green rollback deployment failed")
    except Exception as exc:
        if current_scheduler_stopped:
            try:
                _set_slot_scheduler_replicas(
                    kube_context=kube_context,
                    namespace=namespace,
                    slot=current["slot"],
                    replicas=1,
                    runner=runner,
                    action="blue_green_current_scheduler_restore",
                )
            except TkeReleaseError as restore_error:
                raise TkeReleaseError(
                    "blue-green rollback failed and current Scheduler restore failed"
                ) from restore_error
        if isinstance(exc, TkeReleaseError):
            raise
        raise TkeReleaseError("blue-green rollback deployment failed") from exc
    restored = {
        **previous,
        "manifestSha256": deployed["manifestSha256"],
    }
    _record_active_release(state_path=state_path, record=restored)
    return {
        "status": "rolled_back",
        "releaseSha": release_sha,
        "previousReleaseSha": previous["releaseSha"],
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "slot": previous["slot"],
        "manifestSha256": deployed["manifestSha256"],
    }


def execute_rollback(
    *,
    manifests_dir: Path,
    namespace: str,
    release_sha: str,
    kube_context: str,
    state_path: Path,
    execute: bool,
    confirm_release_sha: str | None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Apply only a recorded prior immutable image pair and wait for rollout."""
    if NAMESPACE.fullmatch(namespace) is None or SHA40.fullmatch(release_sha) is None:
        raise TkeReleaseError("TKE rollback identity is invalid")
    _require_execute(execute, confirm_release_sha, release_sha)
    previous = _recorded_rollback_target(
        state_path=state_path,
        release_sha=release_sha,
    )
    _verify_target_context(kube_context=kube_context, runner=runner)
    rendered = render_release_manifests(
        manifests_dir=manifests_dir,
        namespace=namespace,
        release_sha=previous["releaseSha"],
        backend_image=previous["backendImage"],
        frontend_image=previous["frontendImage"],
        schema_release=previous["schemaRelease"],
        artifact_manifest_sha256=previous["artifactManifestSha256"],
        runner=runner,
    )
    _validate_rendered(rendered)
    _server_side_dry_run(
        rendered=rendered,
        kube_context=kube_context,
        runner=runner,
        action="rollback_server_dry_run",
    )
    _apply_runtime_render(
        rendered=rendered,
        kube_context=kube_context,
        runner=runner,
        action="rollback_apply",
    )
    for deployment in ("backend", "frontend", "scheduler"):
        _require_success(
            _execute(
                runner,
                _kubectl(
                    kube_context,
                    "rollout",
                    "status",
                    f"deployment/{deployment}",
                    "--namespace",
                    namespace,
                    "--timeout",
                    "10m",
                ),
                timeout_seconds=ROLL_OUT_TIMEOUT_SECONDS,
            ),
            f"rollback_{deployment}",
        )
    acceptance = accept_deploy(
        namespace=namespace,
        release_sha=previous["releaseSha"],
        kube_context=kube_context,
        expected_scheduler_mode="active",
        runner=runner,
    )
    if acceptance.get("status") != "accepted":
        raise TkeReleaseError("rollback acceptance failed")
    restored = {
        **previous,
        "manifestSha256": _manifest_sha256(rendered),
    }
    _record_active_release(state_path=state_path, record=restored)
    return {
        "status": "rolled_back",
        "releaseSha": release_sha,
        "previousReleaseSha": previous["releaseSha"],
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "manifestSha256": _manifest_sha256(rendered),
    }


def execute_deploy(
    *,
    manifests_dir: Path,
    namespace: str,
    release_sha: str,
    kube_context: str,
    state_path: Path,
    backend_image: str,
    frontend_image: str,
    schema_release: str,
    artifact_manifest_sha256: str,
    execute: bool,
    confirm_release_sha: str | None,
    candidate: bool = False,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Apply an immutable render and wait for every AIOPS workload rollout."""
    _require_execute(execute, confirm_release_sha, release_sha)
    _verify_target_context(kube_context=kube_context, runner=runner)
    render = render_candidate_manifests if candidate else render_release_manifests
    rendered = render(
        manifests_dir=manifests_dir,
        namespace=namespace,
        release_sha=release_sha,
        backend_image=backend_image,
        frontend_image=frontend_image,
        schema_release=schema_release,
        artifact_manifest_sha256=artifact_manifest_sha256,
        runner=runner,
    )
    _validate_rendered(rendered)
    _server_side_dry_run(
        rendered=rendered,
        kube_context=kube_context,
        runner=runner,
        action="server_dry_run",
    )
    _apply_runtime_render(
        rendered=rendered,
        kube_context=kube_context,
        runner=runner,
        action="apply",
    )
    for deployment in ("backend", "frontend", "scheduler"):
        _require_success(
            _execute(
                runner,
                _kubectl(
                    kube_context,
                    "rollout",
                    "status",
                    f"deployment/{deployment}",
                    "--namespace",
                    namespace,
                    "--timeout",
                    "10m",
                ),
                timeout_seconds=ROLL_OUT_TIMEOUT_SECONDS,
            ),
            f"rollout_{deployment}",
        )
    expected_scheduler_mode = "standby" if candidate else "active"
    acceptance = accept_deploy(
        namespace=namespace,
        release_sha=release_sha,
        kube_context=kube_context,
        expected_scheduler_mode=expected_scheduler_mode,
        runner=runner,
    )
    if acceptance.get("status") != "accepted":
        raise TkeReleaseError("TKE deployment acceptance failed")
    if not candidate:
        _record_active_release(
            state_path=state_path,
            record={
                "artifactManifestSha256": artifact_manifest_sha256,
                "backendImage": backend_image,
                "frontendImage": frontend_image,
                "manifestSha256": _manifest_sha256(rendered),
                "releaseSha": release_sha,
                "schemaRelease": schema_release,
                "slot": "blue",
            },
        )
    return {
        "status": "candidate_deployed" if candidate else "deployed",
        "releaseSha": release_sha,
        "namespace": namespace,
        "kubeContext": kube_context,
        "targetCluster": TARGET_CLUSTER_ID,
        "manifestSha256": _manifest_sha256(rendered),
        "deployments": ["backend", "frontend", "scheduler"],
        "schedulerMode": "standby" if candidate else "active",
    }
