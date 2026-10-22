"""Immutable TKE registry-candidate primitives.

The module deliberately does not retain registry credentials.  It binds one
clean source tree to linux/amd64 local images, obtains a short-lived TCR token
at execution time, and records only the immutable digests that TCR exposes
after push.  Cluster pull verification remains a separately authorized
candidate-Pod operation and is therefore represented as false here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable


SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")
BLUE_GREEN_SLOTS = frozenset({"blue", "green"})
SCHEMA_RELEASE = re.compile(r"[^\r\n\t\0]+\Z")
TARGET_PLATFORM = "linux/amd64"
REGISTRY_HOST = "registry.example.test"
REGISTRY_NAME = "searchops-registry"
REGISTRY_NAMESPACE = "aiops"
TCR_REGION = "ap-shanghai"
MAX_STATE_BYTES = 64 * 1024
TKE_CANDIDATE_STATE_FILENAME = "tke-candidate-state.json"
TKE_REHEARSAL_STATE_FILENAME = "tke-candidate-rehearsal-state.json"
TKE_MYSQL_REHEARSAL_STATE_FILENAME = "tke-mysql-rehearsal-state.json"
TKE_CANDIDATE_DEPLOYMENT_STATE_FILENAME = "tke-candidate-deployment-state.json"
RUNTIME_BASE_IMAGE_GATE = Path("scripts/ci/check_runtime_base_image.py")
RUNTIME_BASE_IMAGE_POLICY = Path("scripts/ci/runtime_base_image_policy.json")
SUPPLY_CHAIN_GATE = Path("scripts/ci/supply_chain_gate.py")
SUPPLY_CHAIN_TOOL_LOCK = Path("scripts/ci/supply_chain_tools.json")
SUPPLY_CHAIN_EXCEPTIONS = Path("scripts/ci/supply_chain_exceptions.json")
DOCKER_ENVIRONMENT_KEYS = frozenset(
    {
        "BUILDKIT_PROGRESS",
        "BUILDX_BUILDER",
        "BUILDX_CONFIG",
        "DOCKER_API_VERSION",
        "DOCKER_BUILDKIT",
        "DOCKER_CERT_PATH",
        "DOCKER_CLI_PLUGIN_EXTRA_DIRS",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_DEFAULT_PLATFORM",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "SSH_AUTH_SOCK",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class TkeCandidateError(RuntimeError):
    """Raised when an immutable TKE candidate cannot be safely created."""


class CommandResult:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes):
        self.returncode = int(returncode)
        self.stdout = bytes(stdout)
        self.stderr = bytes(stderr)


Runner = Callable[..., CommandResult]


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _validate_sha(value: str, label: str) -> str:
    if not isinstance(value, str) or SHA40.fullmatch(value) is None:
        raise TkeCandidateError(f"{label} is invalid")
    return value


def _validate_blue_green_slot(value: object) -> str:
    if not isinstance(value, str) or value not in BLUE_GREEN_SLOTS:
        raise TkeCandidateError("TKE candidate deployment slot is invalid")
    return value


def _validate_schema_release(value: str) -> str:
    if not isinstance(value, str) or SCHEMA_RELEASE.fullmatch(value) is None:
        raise TkeCandidateError("schema release is invalid")
    normalized = value.strip()
    if not normalized:
        raise TkeCandidateError("schema release is invalid")
    return normalized


def _component_local_image(component: str, release_sha: str) -> str:
    return f"aiops/{component}:{release_sha}"


def _component_remote_tag(component: str, release_sha: str) -> str:
    return f"{REGISTRY_HOST}/{REGISTRY_NAMESPACE}/{component}:{release_sha}"


def _artifact_manifest_sha256(
    *, release_sha: str, schema_release: str, source_tree_sha: str
) -> str:
    """Bind a candidate to source facts before a release artifact exists.

    The full release artifact may be generated only after this phase in a
    future CI adapter.  This deterministic identity is intentionally narrow:
    it contains no environment values, credentials, or mutable image tags.
    """
    payload = {
        "releaseSha": release_sha,
        "schemaRelease": schema_release,
        "sourceTreeSha": source_tree_sha,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def build_candidate_preview(
    *, release_sha: str, schema_release: str, source_tree_sha: str
) -> dict[str, Any]:
    """Build a deterministic, non-mutating TCR candidate plan."""
    release_sha = _validate_sha(release_sha, "release SHA")
    source_tree_sha = _validate_sha(source_tree_sha, "source tree SHA")
    schema_release = _validate_schema_release(schema_release)
    return {
        "artifactManifestSha256": _artifact_manifest_sha256(
            release_sha=release_sha,
            schema_release=schema_release,
            source_tree_sha=source_tree_sha,
        ),
        "localImages": {
            component: _component_local_image(component, release_sha)
            for component in ("backend", "frontend")
        },
        "registry": {
            "host": REGISTRY_HOST,
            "id": REGISTRY_NAME,
            "namespace": REGISTRY_NAMESPACE,
        },
        "releaseSha": release_sha,
        "remoteTags": {
            component: _component_remote_tag(component, release_sha)
            for component in ("backend", "frontend")
        },
        "schemaRelease": schema_release,
        "sourceTreeSha": source_tree_sha,
        "status": "tke_candidate_preview",
        "targetPlatform": TARGET_PLATFORM,
    }


def _run_subprocess(
    argv: Iterable[str],
    *,
    input_bytes: bytes | None = None,
    timeout_seconds: float | None = None,
    environment: dict[str, str] | None = None,
) -> CommandResult:
    try:
        result = subprocess.run(
            [str(item) for item in argv],
            input=input_bytes,
            capture_output=True,
            check=False,
            env=environment,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TkeCandidateError("TCR candidate command timed out") from exc
    except OSError as exc:
        raise TkeCandidateError("TCR candidate command is unavailable") from exc
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _execute(
    runner: Runner | None,
    argv: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout_seconds: float | None = None,
    environment: dict[str, str] | None = None,
) -> CommandResult:
    if runner is None:
        return _run_subprocess(
            argv,
            input_bytes=input_bytes,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
    try:
        result = runner(
            argv,
            input_bytes=input_bytes,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
    except TypeError:
        result = runner(argv)
    if not isinstance(result, CommandResult):
        raise TkeCandidateError("TCR candidate runner result is invalid")
    return result


def _require_success(result: CommandResult, action: str) -> bytes:
    if result.returncode != 0:
        raise TkeCandidateError(f"{action} failed")
    return result.stdout


def _require_execute(
    *, execute: bool, confirm_release_sha: str | None, release_sha: str
) -> None:
    if execute is not True:
        raise TkeCandidateError("TKE candidate requires --execute")
    if confirm_release_sha != release_sha:
        raise TkeCandidateError(
            "TKE candidate requires an exact --confirm-release-sha"
        )


def _require_preview(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "artifactManifestSha256",
        "localImages",
        "registry",
        "releaseSha",
        "remoteTags",
        "schemaRelease",
        "sourceTreeSha",
        "status",
        "targetPlatform",
    }:
        raise TkeCandidateError("TKE candidate preview is invalid")
    recreated = build_candidate_preview(
        release_sha=str(value.get("releaseSha")),
        schema_release=str(value.get("schemaRelease")),
        source_tree_sha=str(value.get("sourceTreeSha")),
    )
    if value != recreated:
        raise TkeCandidateError("TKE candidate preview is invalid")
    return recreated


def _read_ignore_patterns(path: Path) -> set[str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise TkeCandidateError("TKE candidate build context is invalid")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise TkeCandidateError("TKE candidate build context is invalid") from exc
    return {
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }


def validate_build_context(*, repo: Path) -> None:
    """Require runtime credentials/configuration to be outside both contexts."""
    root = Path(repo)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise TkeCandidateError("TKE candidate build context is invalid")
    root_patterns = _read_ignore_patterns(root / ".dockerignore")
    frontend_patterns = _read_ignore_patterns(root / "frontend" / ".dockerignore")
    required = {".env", ".env.*", "config.yaml"}
    if not required.issubset(root_patterns) or not required.issubset(frontend_patterns):
        raise TkeCandidateError(
            "TKE candidate build context excludes runtime configuration incompletely"
        )
    if "frontend/" not in root_patterns and "frontend" not in root_patterns:
        raise TkeCandidateError(
            "TKE candidate build context excludes frontend source incompletely"
        )


def _required_repo_file(repo: Path, relative_path: Path, label: str) -> Path:
    path = Path(repo) / relative_path
    if path.is_symlink() or not path.is_file():
        raise TkeCandidateError(f"{label} is unavailable")
    return path


def _run_runtime_base_image_gate(
    *, repo: Path, runner: Runner | None, environment: dict[str, str]
) -> None:
    """Verify the audited backend base image before any candidate build."""
    gate = _required_repo_file(repo, RUNTIME_BASE_IMAGE_GATE, "runtime base image gate")
    policy = _required_repo_file(
        repo, RUNTIME_BASE_IMAGE_POLICY, "runtime base image policy"
    )
    dockerfile = _required_repo_file(repo, Path("Dockerfile"), "runtime Dockerfile")
    _require_success(
        _execute(
            runner,
            [
                sys.executable,
                str(gate),
                "--dockerfile",
                str(dockerfile),
                "--policy",
                str(policy),
            ],
            timeout_seconds=30,
            environment=environment,
        ),
        "runtime base image gate",
    )


def _run_supply_chain_gate(
    *,
    repo: Path,
    preview: dict[str, Any],
    runner: Runner | None,
    environment: dict[str, str],
) -> None:
    """Block registry publication until both local candidate images are scanned."""
    gate = _required_repo_file(repo, SUPPLY_CHAIN_GATE, "supply-chain gate")
    tool_lock = _required_repo_file(repo, SUPPLY_CHAIN_TOOL_LOCK, "supply-chain tool lock")
    exceptions = _required_repo_file(
        repo, SUPPLY_CHAIN_EXCEPTIONS, "supply-chain exception contract"
    )
    with tempfile.TemporaryDirectory(prefix="aiops-supply-chain-") as temporary:
        output_dir = Path(temporary) / "evidence"
        _require_success(
            _execute(
                runner,
                [
                    sys.executable,
                    str(gate),
                    "--release-sha",
                    preview["releaseSha"],
                    "--artifact-manifest-sha256",
                    preview["artifactManifestSha256"],
                    "--image",
                    f"backend={preview['localImages']['backend']}",
                    "--image",
                    f"frontend={preview['localImages']['frontend']}",
                    "--output-dir",
                    str(output_dir),
                    "--tool-lock",
                    str(tool_lock),
                    "--exceptions",
                    str(exceptions),
                ],
                timeout_seconds=30 * 60,
                environment=environment,
            ),
            "supply-chain gate",
        )


def _canonical_state_path(path: Path) -> Path:
    value = Path(path)
    if (
        not value.is_absolute()
        or value.is_symlink()
        or value.name != TKE_CANDIDATE_STATE_FILENAME
        or os.path.normpath(str(value)) != str(value)
    ):
        raise TkeCandidateError("TKE candidate state path is invalid")
    return value


def _validate_candidate_record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "artifactManifestSha256",
        "backendImage",
        "clusterPullVerified",
        "frontendImage",
        "releaseSha",
        "schemaRelease",
        "sourceTreeSha",
    }:
        raise TkeCandidateError("TKE candidate state is invalid")
    release_sha = _validate_sha(str(value["releaseSha"]), "release SHA")
    source_tree_sha = _validate_sha(str(value["sourceTreeSha"]), "source tree SHA")
    schema_release = _validate_schema_release(str(value["schemaRelease"]))
    artifact_manifest_sha256 = str(value["artifactManifestSha256"])
    backend_image = str(value["backendImage"])
    frontend_image = str(value["frontendImage"])
    image_pattern = re.compile(
        r"registry\.example\.test/aiops/(?:backend|frontend)@sha256:[0-9a-f]{64}\Z"
    )
    if (
        SHA64.fullmatch(artifact_manifest_sha256) is None
        or image_pattern.fullmatch(backend_image) is None
        or not backend_image.startswith(f"{REGISTRY_HOST}/{REGISTRY_NAMESPACE}/backend@")
        or image_pattern.fullmatch(frontend_image) is None
        or not frontend_image.startswith(f"{REGISTRY_HOST}/{REGISTRY_NAMESPACE}/frontend@")
        or value["clusterPullVerified"] is not False
    ):
        raise TkeCandidateError("TKE candidate state is invalid")
    expected_artifact_sha = _artifact_manifest_sha256(
        release_sha=release_sha,
        schema_release=schema_release,
        source_tree_sha=source_tree_sha,
    )
    if artifact_manifest_sha256 != expected_artifact_sha:
        raise TkeCandidateError("TKE candidate state is invalid")
    return dict(value)


@contextmanager
def _locked_candidate_state(path: Path):
    state_path = _canonical_state_path(path)
    directory = state_path.parent
    if directory.is_symlink():
        raise TkeCandidateError("TKE candidate state directory is invalid")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_stat = directory.lstat()
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.geteuid()
        or directory_stat.st_mode & 0o022
    ):
        raise TkeCandidateError("TKE candidate state directory is invalid")
    os.chmod(directory, 0o700)
    lock_path = directory / ".tke-candidate-state.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise TkeCandidateError("TKE candidate state is unavailable") from exc
    try:
        lock_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.geteuid()
            or stat.S_IMODE(lock_stat.st_mode) != 0o600
        ):
            raise TkeCandidateError("TKE candidate state is invalid")
        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield state_path
    finally:
        try:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _read_candidate_unlocked(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise TkeCandidateError("TKE candidate state is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise TkeCandidateError("TKE candidate state is unavailable") from exc
    try:
        node = os.fstat(fd)
        if (
            not stat.S_ISREG(node.st_mode)
            or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) != 0o600
            or not 0 < node.st_size <= MAX_STATE_BYTES
        ):
            raise TkeCandidateError("TKE candidate state is invalid")
        payload = os.read(fd, MAX_STATE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeCandidateError("TKE candidate state is invalid") from exc
    if payload != _canonical_json(value):
        raise TkeCandidateError("TKE candidate state is invalid")
    return _validate_candidate_record(value)


def _write_candidate_unlocked(path: Path, record: dict[str, Any]) -> None:
    payload = _canonical_json(_validate_candidate_record(record))
    temporary = path.parent / (
        ".tke-candidate-state."
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
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise TkeCandidateError("TKE candidate state is unavailable") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def record_candidate(*, state_path: Path, record: dict[str, Any]) -> None:
    """Atomically persist only immutable candidate identity facts."""
    with _locked_candidate_state(state_path) as locked_path:
        _write_candidate_unlocked(locked_path, record)


def read_candidate(*, state_path: Path, release_sha: str) -> dict[str, Any]:
    expected_sha = _validate_sha(release_sha, "release SHA")
    with _locked_candidate_state(state_path) as locked_path:
        record = _read_candidate_unlocked(locked_path)
    if record is None or record["releaseSha"] != expected_sha:
        raise TkeCandidateError("recorded TKE candidate is missing")
    return record


def _canonical_rehearsal_path(path: Path) -> Path:
    value = Path(path)
    if (
        not value.is_absolute()
        or value.is_symlink()
        or value.name != TKE_REHEARSAL_STATE_FILENAME
        or os.path.normpath(str(value)) != str(value)
    ):
        raise TkeCandidateError("TKE rehearsal state path is invalid")
    return value


def record_cluster_pull_rehearsal(
    *, state_path: Path, candidate: dict[str, Any], kube_context: str, evidence: dict[str, Any]
) -> None:
    """Persist only a completed Pod-level pull proof, never image credentials."""
    candidate = _validate_candidate_record(candidate)
    if not isinstance(kube_context, str) or not kube_context or any(
        character.isspace() for character in kube_context
    ):
        raise TkeCandidateError("TKE rehearsal kube context is invalid")
    expected_evidence = {
        "status": "cluster_pull_verified",
        "releaseSha": candidate["releaseSha"],
        "namespace": "aiops-demo",
        "kubeContext": kube_context,
        "targetCluster": "cls-demo0000",
        "images": {
            "backend": candidate["backendImage"],
            "frontend": candidate["frontendImage"],
        },
    }
    if evidence != expected_evidence:
        raise TkeCandidateError("TKE rehearsal evidence is invalid")
    path = _canonical_rehearsal_path(state_path)
    directory = path.parent
    if directory.is_symlink():
        raise TkeCandidateError("TKE rehearsal state directory is invalid")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    node = directory.lstat()
    if not stat.S_ISDIR(node.st_mode) or node.st_uid != os.geteuid() or node.st_mode & 0o022:
        raise TkeCandidateError("TKE rehearsal state directory is invalid")
    os.chmod(directory, 0o700)
    payload = {
        "backendImage": candidate["backendImage"],
        "frontendImage": candidate["frontendImage"],
        "kubeContext": kube_context,
        "releaseSha": candidate["releaseSha"],
        "status": "cluster_pull_verified",
    }
    encoded = _canonical_json(payload)
    temporary = directory / (
        ".tke-candidate-rehearsal."
        + str(os.getpid())
        + "."
        + secrets.token_hex(8)
        + ".tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise TkeCandidateError("TKE rehearsal state is unavailable") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def require_cluster_pull_rehearsal(
    *, state_path: Path, candidate: dict[str, Any], kube_context: str
) -> None:
    candidate = _validate_candidate_record(candidate)
    path = _canonical_rehearsal_path(state_path)
    if not path.exists() or path.is_symlink():
        raise TkeCandidateError("TKE cluster image pull rehearsal is missing")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise TkeCandidateError("TKE cluster image pull rehearsal is unavailable") from exc
    try:
        node = os.fstat(fd)
        if (
            not stat.S_ISREG(node.st_mode)
            or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) != 0o600
            or not 0 < node.st_size <= MAX_STATE_BYTES
        ):
            raise TkeCandidateError("TKE cluster image pull rehearsal is invalid")
        encoded = os.read(fd, MAX_STATE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeCandidateError("TKE cluster image pull rehearsal is invalid") from exc
    expected = {
        "backendImage": candidate["backendImage"],
        "frontendImage": candidate["frontendImage"],
        "kubeContext": kube_context,
        "releaseSha": candidate["releaseSha"],
        "status": "cluster_pull_verified",
    }
    if encoded != _canonical_json(payload) or payload != expected:
        raise TkeCandidateError("TKE cluster image pull rehearsal is invalid")


def _canonical_mysql_rehearsal_path(path: Path) -> Path:
    value = Path(path)
    if (
        not value.is_absolute()
        or value.is_symlink()
        or value.name != TKE_MYSQL_REHEARSAL_STATE_FILENAME
        or os.path.normpath(str(value)) != str(value)
    ):
        raise TkeCandidateError("TKE MySQL rehearsal state path is invalid")
    return value


def _mysql_rehearsal_state_payload(
    *, candidate: dict[str, Any], kube_context: str, evidence: dict[str, Any]
) -> dict[str, Any]:
    candidate = _validate_candidate_record(candidate)
    if not isinstance(kube_context, str) or not kube_context or any(
        character.isspace() for character in kube_context
    ):
        raise TkeCandidateError("TKE MySQL rehearsal kube context is invalid")
    if not isinstance(evidence, dict):
        raise TkeCandidateError("TKE MySQL rehearsal evidence is invalid")
    expected_evidence = {
        "backendImage": candidate["backendImage"],
        "kubeContext": kube_context,
        "namespace": "aiops-demo",
        "readOnlyQuery": True,
        "releaseSha": candidate["releaseSha"],
        "status": "mysql_rehearsal_verified",
        "targetCluster": "cls-demo0000",
    }
    if evidence != expected_evidence:
        raise TkeCandidateError("TKE MySQL rehearsal evidence is invalid")
    return {
        "backendImage": candidate["backendImage"],
        "kubeContext": kube_context,
        "readOnlyQuery": True,
        "releaseSha": candidate["releaseSha"],
        "status": "mysql_rehearsal_verified",
    }


def record_mysql_rehearsal(
    *, state_path: Path, candidate: dict[str, Any], kube_context: str, evidence: dict[str, Any]
) -> None:
    """Persist a matching no-write MySQL connection proof for one candidate."""
    path = _canonical_mysql_rehearsal_path(state_path)
    payload = _mysql_rehearsal_state_payload(
        candidate=candidate,
        kube_context=kube_context,
        evidence=evidence,
    )
    directory = path.parent
    if directory.is_symlink():
        raise TkeCandidateError("TKE MySQL rehearsal state directory is invalid")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    node = directory.lstat()
    if not stat.S_ISDIR(node.st_mode) or node.st_uid != os.geteuid() or node.st_mode & 0o022:
        raise TkeCandidateError("TKE MySQL rehearsal state directory is invalid")
    os.chmod(directory, 0o700)
    temporary = directory / (
        ".tke-mysql-rehearsal."
        + str(os.getpid())
        + "."
        + secrets.token_hex(8)
        + ".tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        try:
            os.write(fd, _canonical_json(payload))
            os.fsync(fd)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise TkeCandidateError("TKE MySQL rehearsal state is unavailable") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def require_mysql_rehearsal(
    *, state_path: Path, candidate: dict[str, Any], kube_context: str
) -> dict[str, Any]:
    """Require a canonical no-write database proof before TKE deployment."""
    candidate = _validate_candidate_record(candidate)
    path = _canonical_mysql_rehearsal_path(state_path)
    if not path.exists() or path.is_symlink():
        raise TkeCandidateError("TKE MySQL rehearsal is missing")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise TkeCandidateError("TKE MySQL rehearsal is unavailable") from exc
    try:
        node = os.fstat(fd)
        if (
            not stat.S_ISREG(node.st_mode)
            or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) != 0o600
            or not 0 < node.st_size <= MAX_STATE_BYTES
        ):
            raise TkeCandidateError("TKE MySQL rehearsal is invalid")
        encoded = os.read(fd, MAX_STATE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeCandidateError("TKE MySQL rehearsal is invalid") from exc
    if not isinstance(payload, dict):
        raise TkeCandidateError("TKE MySQL rehearsal is invalid")
    evidence = {
        "backendImage": payload.get("backendImage"),
        "kubeContext": payload.get("kubeContext"),
        "namespace": "aiops-demo",
        "readOnlyQuery": payload.get("readOnlyQuery"),
        "releaseSha": payload.get("releaseSha"),
        "status": payload.get("status"),
        "targetCluster": "cls-demo0000",
    }
    expected = _mysql_rehearsal_state_payload(
        candidate=candidate,
        kube_context=kube_context,
        evidence=evidence,
    )
    if encoded != _canonical_json(payload) or payload != expected:
        raise TkeCandidateError("TKE MySQL rehearsal is invalid")
    return expected


def _canonical_candidate_deployment_path(path: Path) -> Path:
    value = Path(path)
    if (
        not value.is_absolute()
        or value.is_symlink()
        or value.name != TKE_CANDIDATE_DEPLOYMENT_STATE_FILENAME
        or os.path.normpath(str(value)) != str(value)
    ):
        raise TkeCandidateError("TKE candidate deployment state path is invalid")
    return value


def _candidate_deployment_state_payload(
    *, candidate: dict[str, Any], kube_context: str, evidence: dict[str, Any]
) -> dict[str, Any]:
    candidate = _validate_candidate_record(candidate)
    if not isinstance(kube_context, str) or not kube_context or any(
        character.isspace() for character in kube_context
    ):
        raise TkeCandidateError("TKE candidate deployment kube context is invalid")
    if not isinstance(evidence, dict):
        raise TkeCandidateError("TKE candidate deployment evidence is invalid")
    manifest_sha = evidence.get("manifestSha256")
    slot = _validate_blue_green_slot(evidence.get("slot"))
    expected_evidence = {
        "status": "candidate_deployed",
        "releaseSha": candidate["releaseSha"],
        "namespace": "aiops-demo",
        "kubeContext": kube_context,
        "targetCluster": "cls-demo0000",
        "manifestSha256": manifest_sha,
        "slot": slot,
        "deployments": [
            f"backend-{slot}",
            f"frontend-{slot}",
            f"scheduler-{slot}",
        ],
        "services": [f"api-{slot}", f"frontend-{slot}"],
        "schedulerMode": "standby",
    }
    if not isinstance(manifest_sha, str) or SHA64.fullmatch(manifest_sha) is None or evidence != expected_evidence:
        raise TkeCandidateError("TKE candidate deployment evidence is invalid")
    return {
        "backendImage": candidate["backendImage"],
        "frontendImage": candidate["frontendImage"],
        "kubeContext": kube_context,
        "manifestSha256": manifest_sha,
        "releaseSha": candidate["releaseSha"],
        "schedulerMode": "standby",
        "slot": slot,
        "status": "candidate_deployed",
    }


def record_candidate_deployment(
    *, state_path: Path, candidate: dict[str, Any], kube_context: str, evidence: dict[str, Any]
) -> None:
    """Persist a completed no-traffic standby deployment proof.

    The file intentionally contains immutable images and acceptance identity
    only.  It is the explicit boundary between a rehearsal and production
    activation, so an active Scheduler cannot be started merely because image
    pull proof exists.
    """
    path = _canonical_candidate_deployment_path(state_path)
    payload = _candidate_deployment_state_payload(
        candidate=candidate,
        kube_context=kube_context,
        evidence=evidence,
    )
    directory = path.parent
    if directory.is_symlink():
        raise TkeCandidateError("TKE candidate deployment state directory is invalid")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    node = directory.lstat()
    if not stat.S_ISDIR(node.st_mode) or node.st_uid != os.geteuid() or node.st_mode & 0o022:
        raise TkeCandidateError("TKE candidate deployment state directory is invalid")
    os.chmod(directory, 0o700)
    temporary = directory / (
        ".tke-candidate-deployment."
        + str(os.getpid())
        + "."
        + secrets.token_hex(8)
        + ".tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        try:
            os.write(fd, _canonical_json(payload))
            os.fsync(fd)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise TkeCandidateError("TKE candidate deployment state is unavailable") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def require_candidate_deployment(
    *, state_path: Path, candidate: dict[str, Any], kube_context: str
) -> dict[str, Any]:
    """Require a matching completed standby deployment before activation."""
    candidate = _validate_candidate_record(candidate)
    path = _canonical_candidate_deployment_path(state_path)
    if not path.exists() or path.is_symlink():
        raise TkeCandidateError("TKE candidate deployment rehearsal is missing")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise TkeCandidateError("TKE candidate deployment rehearsal is unavailable") from exc
    try:
        node = os.fstat(fd)
        if (
            not stat.S_ISREG(node.st_mode)
            or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) != 0o600
            or not 0 < node.st_size <= MAX_STATE_BYTES
        ):
            raise TkeCandidateError("TKE candidate deployment rehearsal is invalid")
        encoded = os.read(fd, MAX_STATE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeCandidateError("TKE candidate deployment rehearsal is invalid") from exc
    evidence = {
        "status": payload.get("status") if isinstance(payload, dict) else None,
        "releaseSha": payload.get("releaseSha") if isinstance(payload, dict) else None,
        "namespace": "aiops-demo",
        "kubeContext": payload.get("kubeContext") if isinstance(payload, dict) else None,
        "targetCluster": "cls-demo0000",
        "manifestSha256": payload.get("manifestSha256") if isinstance(payload, dict) else None,
        "slot": payload.get("slot") if isinstance(payload, dict) else None,
        "deployments": [
            f"backend-{payload.get('slot')}",
            f"frontend-{payload.get('slot')}",
            f"scheduler-{payload.get('slot')}",
        ] if isinstance(payload, dict) and isinstance(payload.get("slot"), str) else [],
        "services": [
            f"api-{payload.get('slot')}",
            f"frontend-{payload.get('slot')}",
        ] if isinstance(payload, dict) and isinstance(payload.get("slot"), str) else [],
        "schedulerMode": payload.get("schedulerMode") if isinstance(payload, dict) else None,
    }
    expected = _candidate_deployment_state_payload(
        candidate=candidate,
        kube_context=kube_context,
        evidence=evidence,
    )
    if encoded != _canonical_json(payload) or payload != expected:
        raise TkeCandidateError("TKE candidate deployment rehearsal is invalid")
    return expected


def _docker_image_identity(
    *,
    image: str,
    component: str,
    release_sha: str,
    schema_release: str,
    artifact_manifest_sha256: str,
    runner: Runner | None,
    environment: dict[str, str],
) -> None:
    raw = _require_success(
        _execute(
            runner,
            ["docker", "image", "inspect", image],
            environment=environment,
            timeout_seconds=30,
        ),
        f"{component} image inspect",
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
        item = payload[0]
        labels = item["Config"]["Labels"]
    except (UnicodeError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise TkeCandidateError(f"{component} image identity is invalid") from exc
    expected_labels = {
        "aiops.artifact-manifest-sha256": artifact_manifest_sha256,
        "aiops.component": component,
        "aiops.schema-release": schema_release,
        "org.opencontainers.image.revision": release_sha,
    }
    image_id = item.get("Id") if isinstance(item, dict) else None
    if (
        not isinstance(image_id, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        or item.get("Os") != "linux"
        or item.get("Architecture") != "amd64"
        or not isinstance(labels, dict)
        or any(labels.get(key) != expected for key, expected in expected_labels.items())
    ):
        raise TkeCandidateError(f"{component} image identity is invalid")


def _tccli_payload(raw: bytes, action: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeCandidateError(f"{action} readback is invalid") from exc
    if not isinstance(payload, dict):
        raise TkeCandidateError(f"{action} readback is invalid")
    response = payload.get("Response")
    if isinstance(response, dict):
        return response
    return payload


def _tcr_registry_id(*, runner: Runner | None) -> str:
    raw = _require_success(
        _execute(
            runner,
            [
                "tccli",
                "tcr",
                "DescribeInstances",
                "--region",
                TCR_REGION,
                "--Limit",
                "100",
                "--Offset",
                "0",
                "--output",
                "json",
            ],
            timeout_seconds=30,
        ),
        "TCR instance readback",
    )
    payload = _tccli_payload(raw, "TCR instance")
    registries = payload.get("Registries")
    if not isinstance(registries, list):
        raise TkeCandidateError("TCR instance readback is invalid")
    matches = [
        registry
        for registry in registries
        if isinstance(registry, dict)
        and registry.get("RegistryName") == REGISTRY_NAME
        and registry.get("PublicDomain") == REGISTRY_HOST
        and registry.get("Status") == "Running"
    ]
    if len(matches) != 1:
        raise TkeCandidateError("TCR registry identity is not unique and running")
    registry_id = matches[0].get("RegistryId")
    if not isinstance(registry_id, str) or not re.fullmatch(r"tcr-[a-z0-9]+", registry_id):
        raise TkeCandidateError("TCR registry ID is invalid")
    return registry_id


def _tcr_login_token(*, runner: Runner | None) -> tuple[str, bytes]:
    registry_id = _tcr_registry_id(runner=runner)
    raw = _require_success(
        _execute(
            runner,
            [
                "tccli",
                "tcr",
                "CreateInstanceToken",
                "--region",
                TCR_REGION,
                "--RegistryId",
                registry_id,
                "--TokenType",
                "temp",
                "--output",
                "json",
            ],
            timeout_seconds=30,
        ),
        "TCR temporary token creation",
    )
    response = _tccli_payload(raw, "TCR temporary token")
    username = response.get("Username")
    token = response.get("Token")
    token_id = response.get("TokenId")
    expiration = response.get("ExpTime")
    if (
        not isinstance(username, str)
        or not username
        or any(character.isspace() for character in username)
        or not isinstance(token, str)
        or not token
        or "\x00" in token
        or token_id not in (None, "")
        or not isinstance(expiration, int)
        or expiration <= 0
    ):
        raise TkeCandidateError("TCR token readback is invalid")
    return username, token.encode("utf-8") + b"\n"


def _remote_digest(
    *,
    remote_tag: str,
    component: str,
    runner: Runner | None,
    environment: dict[str, str],
) -> str:
    payload = _require_success(
        _execute(
            runner,
            ["docker", "buildx", "imagetools", "inspect", "--raw", remote_tag],
            environment=environment,
            timeout_seconds=60,
        ),
        f"{component} digest readback",
    )
    if not payload or len(payload) > 4 * 1024 * 1024:
        raise TkeCandidateError(f"{component} digest readback is invalid")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TkeCandidateError(f"{component} digest readback is invalid") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != 2:
        raise TkeCandidateError(f"{component} digest readback is invalid")
    manifests = value.get("manifests")
    if manifests is not None:
        if not isinstance(manifests, list) or not manifests:
            raise TkeCandidateError(f"{component} digest readback is invalid")
        platform_manifests = [
            item for item in manifests
            if isinstance(item, dict)
            and isinstance(item.get("platform"), dict)
            and item["platform"].get("os") == "linux"
            and item["platform"].get("architecture") == "amd64"
        ]
        if len(platform_manifests) != 1:
            raise TkeCandidateError(f"{component} linux/amd64 digest is missing or ambiguous")
        digest = platform_manifests[0].get("digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:") or SHA64.fullmatch(digest[7:]) is None:
            raise TkeCandidateError(f"{component} linux/amd64 digest is invalid")
        return digest
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _docker_environment() -> dict[str, str]:
    """Pass only Docker transport and CLI-discovery settings to subprocesses."""
    return {
        key: value
        for key in DOCKER_ENVIRONMENT_KEYS
        if (value := os.environ.get(key)) is not None
    }


def _buildx_plugin_candidates(environment: dict[str, str]) -> list[Path]:
    config_root = environment.get("DOCKER_CONFIG")
    if config_root:
        config_dirs = [Path(config_root)]
    else:
        home = environment.get("HOME")
        config_dirs = [Path(home) / ".docker"] if home else []
    extra_dirs = environment.get("DOCKER_CLI_PLUGIN_EXTRA_DIRS")
    if extra_dirs:
        config_dirs.extend(
            Path(item)
            for item in extra_dirs.split(os.pathsep)
            if item
        )
    candidates = [directory / "cli-plugins" / "docker-buildx" for directory in config_dirs]
    candidates.extend(
        Path(directory) / "docker-buildx"
        for directory in (
            "/usr/local/lib/docker/cli-plugins",
            "/usr/local/libexec/docker/cli-plugins",
            "/usr/lib/docker/cli-plugins",
            "/usr/libexec/docker/cli-plugins",
        )
    )
    return candidates


def _link_buildx_plugin(*, docker_config: Path, environment: dict[str, str]) -> None:
    for candidate in _buildx_plugin_candidates(environment):
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        plugin_dir = docker_config / "cli-plugins"
        plugin_dir.mkdir(mode=0o700)
        os.symlink(candidate, plugin_dir / "docker-buildx")
        return


@contextmanager
def _temporary_docker_environment():
    """Provide an owner-only Docker config and remove short-lived auth afterwards."""
    directory = Path(tempfile.mkdtemp(prefix="aiops-tcr-"))
    try:
        os.chmod(directory, 0o700)
        node = directory.lstat()
        if (
            not stat.S_ISDIR(node.st_mode)
            or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) != 0o700
        ):
            raise TkeCandidateError("TCR temporary credential directory is invalid")
        environment = _docker_environment()
        _link_buildx_plugin(docker_config=directory, environment=environment)
        environment["DOCKER_CONFIG"] = str(directory)
        yield environment
    finally:
        try:
            if directory.exists() or directory.is_symlink():
                shutil.rmtree(directory)
        except OSError as exc:
            raise TkeCandidateError("TCR temporary credential cleanup failed") from exc


def create_candidate(
    *,
    repo: Path,
    preview: dict[str, Any],
    execute: bool,
    confirm_release_sha: str | None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Build, push, re-read immutable digests, and locally pull each candidate.

    A real TKE pull needs creation of a temporary Pod and is intentionally not
    folded into this function.  Its proof is recorded by the later candidate
    rehearsal phase, rather than inferred from a local Docker pull.
    """
    preview = _require_preview(preview)
    release_sha = preview["releaseSha"]
    _require_execute(
        execute=execute,
        confirm_release_sha=confirm_release_sha,
        release_sha=release_sha,
    )
    validate_build_context(repo=Path(repo))
    build_environment = _docker_environment()
    _require_success(
        _execute(
            runner,
            ["docker", "version", "--format", "{{.Server.Version}}"],
            timeout_seconds=15,
            environment=build_environment,
        ),
        "Docker daemon preflight",
    )
    _run_runtime_base_image_gate(
        repo=Path(repo),
        runner=runner,
        environment=build_environment,
    )
    for component in ("backend", "frontend"):
        local_image = preview["localImages"][component]
        _require_success(
            _execute(
                runner,
                [
                    "docker",
                    "build",
                    "--platform",
                    TARGET_PLATFORM,
                    "--pull=false",
                    "--build-arg",
                    f"RELEASE_SHA={release_sha}",
                    "--build-arg",
                    f"SCHEMA_RELEASE={preview['schemaRelease']}",
                    "--build-arg",
                    f"ARTIFACT_MANIFEST_SHA256={preview['artifactManifestSha256']}",
                    "--tag",
                    local_image,
                    str((Path(repo) / "frontend") if component == "frontend" else Path(repo)),
                ],
                timeout_seconds=30 * 60,
                environment=build_environment,
            ),
            f"{component} build",
        )
        _docker_image_identity(
            image=local_image,
            component=component,
            release_sha=release_sha,
            schema_release=preview["schemaRelease"],
            artifact_manifest_sha256=preview["artifactManifestSha256"],
            runner=runner,
            environment=build_environment,
        )
    _run_supply_chain_gate(
        repo=Path(repo),
        preview=preview,
        runner=runner,
        environment=build_environment,
    )
    username, token_input = _tcr_login_token(runner=runner)
    with _temporary_docker_environment() as docker_environment:
        _require_success(
            _execute(
                runner,
                ["docker", "login", "--username", username, "--password-stdin", REGISTRY_HOST],
                input_bytes=token_input,
                timeout_seconds=30,
                environment=docker_environment,
            ),
            "TCR login",
        )
        digests: dict[str, str] = {}
        for component in ("backend", "frontend"):
            local_image = preview["localImages"][component]
            remote_tag = preview["remoteTags"][component]
            _require_success(
                _execute(
                    runner,
                    ["docker", "tag", local_image, remote_tag],
                    timeout_seconds=30,
                    environment=docker_environment,
                ),
                f"{component} tag",
            )
            _require_success(
                _execute(
                    runner,
                    ["docker", "push", remote_tag],
                    timeout_seconds=30 * 60,
                    environment=docker_environment,
                ),
                f"{component} push",
            )
            digest = _remote_digest(
                remote_tag=remote_tag,
                component=component,
                runner=runner,
                environment=docker_environment,
            )
            remote_digest = remote_tag.rsplit(":", 1)[0] + "@" + digest
            _require_success(
                _execute(
                    runner,
                    ["docker", "pull", "--platform", TARGET_PLATFORM, remote_digest],
                    timeout_seconds=30 * 60,
                    environment=docker_environment,
                ),
                f"{component} immutable pull",
            )
            digests[component] = remote_digest
    return {
        "artifactManifestSha256": preview["artifactManifestSha256"],
        "backendImage": digests["backend"],
        "clusterPullVerified": False,
        "frontendImage": digests["frontend"],
        "releaseSha": release_sha,
        "schemaRelease": preview["schemaRelease"],
        "sourceTreeSha": preview["sourceTreeSha"],
    }
