#!/usr/bin/env python3
"""Single, fail-closed entry point for the TKE release lifecycle.

The former SSH/Compose provider has been retired.  Only ``tke-*`` commands
remain available through this entrypoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.release import (
        image_worker_release,
        image_canary_release,
        pi_runtime_release,
        release_preflight,
        release_timing,
        tke_adapter,
        tke_candidate,
        tke_release,
    )
except ModuleNotFoundError:  # Direct execution from scripts/release.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.release import (
        image_worker_release,
        image_canary_release,
        pi_runtime_release,
        release_preflight,
        release_timing,
        tke_adapter,
        tke_candidate,
        tke_release,
    )


_SHA40 = re.compile(r"[0-9a-f]{40}\Z")


class ReleaseError(RuntimeError):
    """Raised when the release entrypoint safety contract is not met."""


def canonical_json(payload: object) -> bytes:
    """Encode stable JSON for machine-readable release output."""
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReleaseError("release result is not JSON serializable") from exc


def _canonical_absolute(path: Path, label: str) -> Path:
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw.startswith("/") or raw == "/":
        raise ReleaseError(f"{label} must be an absolute path")
    if os.path.normpath(raw) != raw or "\x00" in raw:
        raise ReleaseError(f"{label} must be canonical")
    return Path(raw)


class ReleaseOrchestrator:
    """Local source-identity gates retained by the sole production CLI."""

    def __init__(self, *, repo: Path, state_dir: Path):
        self.repo = _canonical_absolute(Path(repo), "repository")
        if not self.repo.is_dir() or self.repo.is_symlink():
            raise ReleaseError("repository is invalid")
        self.state_dir = _canonical_absolute(Path(state_dir), "state directory")

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ("git", "-C", str(self.repo), *args),
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseError("unable to inspect the release repository") from exc

    def _current_commit(self) -> str:
        result = self._git("rev-parse", "HEAD")
        value = result.stdout.decode("ascii", errors="ignore").strip()
        if result.returncode != 0 or _SHA40.fullmatch(value) is None:
            raise ReleaseError("unable to resolve the release commit")
        return value

    def _require_independent_worktree(self) -> None:
        result = self._git("worktree", "list", "--porcelain")
        if result.returncode != 0:
            raise ReleaseError("unable to inspect Git worktrees")
        try:
            lines = result.stdout.decode("utf-8", errors="strict").splitlines()
        except UnicodeError as exc:
            raise ReleaseError("Git worktree list is invalid") from exc
        paths: list[Path] = []
        for line in lines:
            if line.startswith("worktree "):
                raw_path = line.removeprefix("worktree ")
                if not raw_path.startswith("/"):
                    raise ReleaseError("Git worktree path is invalid")
                paths.append(Path(raw_path).resolve(strict=False))
        current = self.repo.resolve(strict=False)
        if not paths or current not in paths:
            raise ReleaseError("repository is not a registered Git worktree")
        if current == paths[0]:
            raise ReleaseError("candidate requires an independent worktree")

    def _require_clean_commit(self, expected_sha: str | None = None) -> str:
        status = self._git("status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0 or status.stdout.strip():
            raise ReleaseError("release requires a clean worktree")
        release_sha = self._current_commit()
        if expected_sha is not None and release_sha != expected_sha:
            raise ReleaseError("release commit does not match the verified candidate")
        return release_sha


def _default_repo() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_state_dir(repo: Path) -> Path:
    configured = os.environ.get("AIOPS_RELEASE_STATE_DIR")
    if configured:
        return Path(configured)
    try:
        result = subprocess.run(
            (
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ),
            capture_output=True,
            check=False,
            timeout=10,
        )
        raw_common_dir = result.stdout.decode("utf-8", errors="strict").strip()
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ReleaseError("unable to resolve the release repository identity") from exc
    common_dir = Path(raw_common_dir)
    if (
        result.returncode != 0
        or not raw_common_dir
        or not common_dir.is_absolute()
        or common_dir.is_symlink()
        or not common_dir.is_dir()
    ):
        raise ReleaseError("unable to resolve the release repository identity")
    identity = hashlib.sha256(str(common_dir).encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".local" / "state" / "aiops-release" / identity


def _add_confirmation(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-release-sha")


def _add_tke_identity(
    parser: argparse.ArgumentParser, *, release_sha_required: bool = True
) -> None:
    parser.add_argument("--namespace", default="aiops-demo")
    parser.add_argument("--release-sha", required=release_sha_required)
    parser.add_argument("--kube-context", required=True)


def pipeline_stages(through: str) -> tuple[str, ...]:
    """Return the fixed, fail-closed stage order for one pipeline run."""
    if through not in {"standby", "activate"}:
        raise ValueError("pipeline through value is invalid")
    stages = (
        "tke-preflight",
        "tke-candidate",
        "tke-prepare",
        "tke-rehearse-pull",
        "tke-rehearse-mysql",
        "tke-rehearse-deploy",
        "tke-rehearse-accept",
    )
    if through == "activate":
        return stages + ("tke-activate", "tke-accept")
    return stages


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _record_stage_timing(
    *,
    state_dir: Path,
    stage: str,
    release_sha: str | None,
    started_at: datetime,
    status: str,
    finished_at: datetime,
    error: BaseException | None = None,
) -> None:
    """Persist timing evidence without masking the release result."""
    try:
        release_timing.record_stage(
            state_dir=state_dir,
            stage=stage,
            release_sha=release_sha,
            status=status,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            elapsed_seconds=(finished_at - started_at).total_seconds(),
            failure_category=release_timing.classify_failure(error) if error else None,
        )
    except (OSError, ValueError):
        # Timing is diagnostic evidence; it must never turn a successful release
        # into a failed release or hide the original failure.
        return


def _pipeline_command(
    *,
    action: str,
    repo: Path,
    state_dir: Path,
    release_sha: str,
    schema_release: str,
    kube_context: str,
    execute: bool,
    confirm_release_sha: str | None,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--repo",
        str(repo),
        "--state-dir",
        str(state_dir),
        "--format",
        "json",
        action,
        "--release-sha",
        release_sha,
    ]
    if action == "tke-candidate":
        command.extend(("--schema-release", schema_release))
    else:
        command.extend(("--kube-context", kube_context))
    if execute and action in {
        "tke-candidate",
        "tke-rehearse-pull",
        "tke-rehearse-mysql",
        "tke-rehearse-deploy",
        "tke-activate",
    }:
        command.extend(("--execute", "--confirm-release-sha", confirm_release_sha or ""))
    return command


def _run_pipeline(
    *,
    repo: Path,
    state_dir: Path,
    release_sha: str,
    schema_release: str,
    kube_context: str,
    through: str,
    execute: bool,
    confirm_release_sha: str | None,
) -> dict[str, Any]:
    if not execute or confirm_release_sha != release_sha:
        raise ReleaseError("TKE pipeline requires --execute and exact --confirm-release-sha")
    results: list[dict[str, Any]] = []
    for action in pipeline_stages(through):
        command = _pipeline_command(
            action=action,
            repo=repo,
            state_dir=state_dir,
            release_sha=release_sha,
            schema_release=schema_release,
            kube_context=kube_context,
            execute=execute,
            confirm_release_sha=confirm_release_sha,
        )
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            raise ReleaseError(f"pipeline stopped at {action}")
        results.append({"stage": action, "status": "passed"})
    return {"releaseSha": release_sha, "status": "pipeline_completed", "through": through, "stages": results}


def _source_tree_sha(repo: Path, release_sha: str) -> str:
    """Return the immutable Git tree identity for one exact candidate commit."""
    try:
        result = subprocess.run(
            ("git", "-C", str(repo), "rev-parse", f"{release_sha}^{{tree}}"),
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError("unable to resolve candidate source tree identity") from exc
    value = result.stdout.decode("ascii", errors="ignore").strip()
    if result.returncode != 0 or _SHA40.fullmatch(value) is None:
        raise ReleaseError("unable to resolve candidate source tree identity")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOPS verified TKE release entrypoint")
    parser.add_argument("--repo", type=Path, default=_default_repo())
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    subparsers = parser.add_subparsers(dest="action", required=True)

    preview = subparsers.add_parser(
        "tke-preview",
        help="build a local-only TKE lifecycle preview; never applies Kubernetes resources",
    )
    preview.add_argument("--release-sha", required=True)
    preview.add_argument("--manifests-dir", type=Path, default=Path("deploy/kubernetes/base"))
    preview.add_argument("--backend-image")
    preview.add_argument("--frontend-image")

    preflight = subparsers.add_parser(
        "tke-preflight",
        help="fail fast on local release materials, Docker and target TKE readiness",
    )
    _add_tke_identity(preflight)

    candidate = subparsers.add_parser(
        "tke-candidate",
        help="build, push, and record immutable TCR digests for one TKE candidate",
    )
    candidate.add_argument("--release-sha", required=True)
    candidate.add_argument("--schema-release", default="zha33")
    _add_confirmation(candidate)

    prepare = subparsers.add_parser(
        "tke-prepare",
        help="read target TKE runtime configuration, Secret key names and RWX PVC without changing cluster state",
    )
    _add_tke_identity(prepare)

    rehearse_pull = subparsers.add_parser(
        "tke-rehearse-pull",
        help="create temporary no-traffic Pods to verify TKE pulls recorded candidate digests",
    )
    _add_tke_identity(rehearse_pull)
    _add_confirmation(rehearse_pull)

    init_runtime_data = subparsers.add_parser(
        "tke-init-runtime-data",
        help="initialize only the CFS runtime directory and prove non-root write access",
    )
    _add_tke_identity(init_runtime_data)
    _add_confirmation(init_runtime_data)

    rehearse_mysql = subparsers.add_parser(
        "tke-rehearse-mysql",
        help="run a temporary no-traffic backend Pod to verify MySQL read-only access",
    )
    _add_tke_identity(rehearse_mysql)
    _add_confirmation(rehearse_mysql)

    cleanup = subparsers.add_parser(
        "tke-cleanup-candidate",
        help="delete only a proven no-Ingress failed TKE candidate; never touches runtime materials",
    )
    _add_tke_identity(cleanup)
    _add_confirmation(cleanup)
    cleanup_scope = cleanup.add_mutually_exclusive_group(required=True)
    cleanup_scope.add_argument("--legacy-single-slot", action="store_true")
    cleanup_scope.add_argument("--slot", choices=("blue", "green"))

    rehearse_deploy = subparsers.add_parser(
        "tke-rehearse-deploy",
        help="server-side apply a no-customer-traffic standby TKE candidate and accept it",
    )
    _add_tke_identity(rehearse_deploy)
    _add_confirmation(rehearse_deploy)
    rehearse_deploy.add_argument("--manifests-dir", type=Path, default=Path("deploy/kubernetes/base"))

    rehearse_accept = subparsers.add_parser(
        "tke-rehearse-accept",
        help="read back one deployed no-customer-traffic standby TKE candidate",
    )
    _add_tke_identity(rehearse_accept)

    activate = subparsers.add_parser(
        "tke-activate",
        help="activate a previously accepted standby TKE candidate with Scheduler active",
    )
    _add_tke_identity(activate)
    _add_confirmation(activate)
    activate.add_argument("--manifests-dir", type=Path, default=Path("deploy/kubernetes/base"))

    accept = subparsers.add_parser(
        "tke-accept", help="read back TKE workload and Service endpoint acceptance evidence"
    )
    _add_tke_identity(accept)

    rollback = subparsers.add_parser(
        "tke-rollback",
        help="roll back only to the prior immutable TKE release recorded by deploy",
    )
    _add_tke_identity(rollback)
    _add_confirmation(rollback)
    rollback.add_argument("--manifests-dir", type=Path, default=Path("deploy/kubernetes/base"))

    adopt_active = subparsers.add_parser(
        "tke-adopt-active",
        help="read back the live active slot and initialize a missing local release state",
    )
    adopt_active.add_argument("--source-state-dir", type=Path, required=True)
    adopt_active.add_argument("--target-state-dir", type=Path, required=True)
    _add_tke_identity(adopt_active)
    _add_confirmation(adopt_active)

    adopt_live = subparsers.add_parser(
        "tke-adopt-live",
        help="reconstruct missing local release state from a fully accepted live TKE slot",
    )
    adopt_live.add_argument("--target-state-dir", type=Path, required=True)
    adopt_live.add_argument("--manifests-dir", type=Path, default=Path("deploy/kubernetes/base"))
    _add_tke_identity(adopt_live)
    _add_confirmation(adopt_live)

    pipeline = subparsers.add_parser(
        "tke-pipeline",
        help="run the existing verified stages in order; defaults to standby without traffic",
    )
    _add_tke_identity(pipeline)
    _add_confirmation(pipeline)
    pipeline.add_argument("--schema-release", default="zha33")
    pipeline.add_argument("--through", choices=("standby", "activate"), default="standby")

    pi_secret = subparsers.add_parser("tke-pi-provision", help="add missing PI credential keys without rotating existing secrets")
    _add_tke_identity(pi_secret)
    _add_confirmation(pi_secret)
    pi_secret.add_argument("--env-file", type=Path, required=True)

    worker = subparsers.add_parser(
        "tke-image-worker", help="preview, deploy, or verify the isolated Atlas browser worker"
    )
    worker.add_argument("--phase", choices=("preview", "build", "provision-secret", "deploy", "verify", "rollback"), required=True)
    worker.add_argument("--release-sha", required=True)
    worker.add_argument("--kube-context", required=True)
    worker.add_argument("--probe-url", default="https://static.atlas.example.test/")
    worker.add_argument("--urls-file", type=Path)
    _add_confirmation(worker)

    canary = subparsers.add_parser(
        "tke-image-canary", help="run a bounded production Atlas-to-COS canary"
    )
    canary.add_argument("--phase", choices=("preview", "provision-secret", "deploy", "verify"), required=True)
    canary.add_argument("--release-sha", required=True)
    canary.add_argument("--worker-release-sha", required=True)
    canary.add_argument("--worker-state-dir", type=Path)
    canary.add_argument("--env-file", type=Path)
    canary.add_argument("--kube-context", required=True)
    canary.add_argument("--backend-image")
    _add_confirmation(canary)
    return parser


def _resolve_manifests_dir(repo: Path, path: Path) -> Path:
    return path if path.is_absolute() else repo / path


def _print_result(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        sys.stdout.buffer.write(canonical_json(payload))
        return
    if payload.get("ok") is False:
        print(f"blocked: {payload.get('error', 'release failed')}", file=sys.stderr)
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _failed_standby_cleanup_hint(
    *, repo: Path, state_dir: Path, namespace: str, release_sha: str, kube_context: str, slot: str
) -> str:
    cleanup_command = shlex.join(
        (
            sys.executable,
            str(Path(__file__).resolve()),
            "--repo", str(repo), "--state-dir", str(state_dir),
            "tke-cleanup-candidate", "--release-sha", release_sha,
            "--namespace", namespace, "--kube-context", kube_context,
            "--slot", slot, "--execute", "--confirm-release-sha", release_sha,
        )
    )
    return (
        "tke-rehearse-deploy selected standby candidate "
        f"release SHA {release_sha}, slot {slot}; no resources were deleted automatically. "
        "If server-side apply completed and that non-active slot still contains the "
        "failed candidate, confirm the slot is inactive and run:\n"
        f"{cleanup_command}\n"
        "If the failure happened before server-side apply, do not run cleanup; fix the "
        "blocker and retry tke-rehearse-deploy."
    )


def _candidate_record(*, state_dir: Path, release_sha: str) -> dict[str, Any]:
    return tke_candidate.read_candidate(
        state_path=state_dir / tke_candidate.TKE_CANDIDATE_STATE_FILENAME,
        release_sha=release_sha,
    )


def _require_candidate_rehearsals(
    *, state_dir: Path, candidate: dict[str, Any], kube_context: str
) -> None:
    tke_candidate.require_cluster_pull_rehearsal(
        state_path=state_dir / tke_candidate.TKE_REHEARSAL_STATE_FILENAME,
        candidate=candidate,
        kube_context=kube_context,
    )
    tke_candidate.require_mysql_rehearsal(
        state_path=state_dir / tke_candidate.TKE_MYSQL_REHEARSAL_STATE_FILENAME,
        candidate=candidate,
        kube_context=kube_context,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    timing_started_at = _utc_now()
    timing_state_dir: Path | None = None
    timing_release_sha = getattr(args, "release_sha", None)
    try:
        repo = _canonical_absolute(args.repo, "repository")
        state_dir = args.state_dir or _default_state_dir(repo)
        timing_state_dir = state_dir
        orchestrator = ReleaseOrchestrator(repo=repo, state_dir=state_dir)
        if args.action == "tke-preview":
            result = tke_adapter.build_preview(
                release_sha=args.release_sha,
                manifests_dir=_resolve_manifests_dir(repo, args.manifests_dir),
                **({"backend_image": args.backend_image} if args.backend_image else {}),
                **({"frontend_image": args.frontend_image} if args.frontend_image else {}),
            )
        elif args.action == "tke-pi-provision":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit(args.release_sha)
            result = pi_runtime_release.provision(env_file=args.env_file, namespace=args.namespace,
                release_sha=args.release_sha, kube_context=args.kube_context, execute=args.execute,
                confirm_release_sha=args.confirm_release_sha)
        elif args.action == "tke-preflight":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit(args.release_sha)
            local = release_preflight.check_local(repo=repo, release_sha=args.release_sha)
            docker = release_preflight.check_docker()
            cluster = tke_release.prepare_deploy(
                namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context
            )
            result = {
                **local,
                **docker,
                "cluster": cluster,
                "status": "preflight_passed",
            }
        elif args.action == "tke-candidate":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit(args.release_sha)
            preview = tke_candidate.build_candidate_preview(
                release_sha=args.release_sha,
                schema_release=args.schema_release,
                source_tree_sha=_source_tree_sha(repo, args.release_sha),
            )
            if args.execute:
                record = tke_candidate.create_candidate(
                    repo=repo, preview=preview, execute=True,
                    confirm_release_sha=args.confirm_release_sha,
                )
                tke_candidate.record_candidate(
                    state_path=state_dir / tke_candidate.TKE_CANDIDATE_STATE_FILENAME,
                    record=record,
                )
                result = {"status": "candidate_verified", **record}
            else:
                if args.confirm_release_sha is not None:
                    raise tke_candidate.TkeCandidateError("TKE candidate confirmation requires --execute")
                result = preview
        elif args.action == "tke-prepare":
            result = tke_release.prepare_deploy(
                namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context
            )
        elif args.action == "tke-rehearse-pull":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit(args.release_sha)
            record = _candidate_record(state_dir=state_dir, release_sha=args.release_sha)
            evidence = tke_release.execute_pull_rehearsal(
                namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context,
                backend_image=record["backendImage"], frontend_image=record["frontendImage"],
                execute=args.execute, confirm_release_sha=args.confirm_release_sha,
            )
            tke_candidate.record_cluster_pull_rehearsal(
                state_path=state_dir / tke_candidate.TKE_REHEARSAL_STATE_FILENAME,
                candidate=record, kube_context=args.kube_context, evidence=evidence,
            )
            result = evidence
        elif args.action == "tke-init-runtime-data":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit()
            record = _candidate_record(state_dir=state_dir, release_sha=args.release_sha)
            tke_candidate.require_cluster_pull_rehearsal(
                state_path=state_dir / tke_candidate.TKE_REHEARSAL_STATE_FILENAME,
                candidate=record, kube_context=args.kube_context,
            )
            result = tke_release.execute_cfs_runtime_init(
                namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context,
                backend_image=record["backendImage"], execute=args.execute,
                confirm_release_sha=args.confirm_release_sha,
            )
        elif args.action == "tke-rehearse-mysql":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit(args.release_sha)
            record = _candidate_record(state_dir=state_dir, release_sha=args.release_sha)
            tke_candidate.require_cluster_pull_rehearsal(
                state_path=state_dir / tke_candidate.TKE_REHEARSAL_STATE_FILENAME,
                candidate=record, kube_context=args.kube_context,
            )
            evidence = tke_release.execute_mysql_rehearsal(
                namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context,
                backend_image=record["backendImage"], execute=args.execute,
                confirm_release_sha=args.confirm_release_sha,
            )
            tke_candidate.record_mysql_rehearsal(
                state_path=state_dir / tke_candidate.TKE_MYSQL_REHEARSAL_STATE_FILENAME,
                candidate=record, kube_context=args.kube_context, evidence=evidence,
            )
            result = evidence
        elif args.action == "tke-cleanup-candidate":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit()
            result = tke_release.cleanup_failed_candidate(
                namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context,
                execute=args.execute, confirm_release_sha=args.confirm_release_sha,
                legacy_single_slot=args.legacy_single_slot, slot=args.slot,
                state_path=(state_dir / tke_release.TKE_STATE_FILENAME if args.slot is not None else None),
            )
        elif args.action in {"tke-rehearse-deploy", "tke-activate"}:
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit(args.release_sha)
            record = _candidate_record(state_dir=state_dir, release_sha=args.release_sha)
            _require_candidate_rehearsals(state_dir=state_dir, candidate=record, kube_context=args.kube_context)
            manifests_dir = _resolve_manifests_dir(repo, args.manifests_dir)
            if args.action == "tke-rehearse-deploy":
                slot = tke_release.require_candidate_slot_safe(
                    state_path=state_dir / tke_release.TKE_STATE_FILENAME,
                    namespace=args.namespace,
                    kube_context=args.kube_context,
                )
                tke_release.prepare_deploy(
                    namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context
                )
                runtime_config_sync = tke_release.sync_runtime_config_schema(
                    namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context,
                    template_path=repo / "config.yaml", execute=args.execute,
                    confirm_release_sha=args.confirm_release_sha,
                )
                try:
                    result = tke_release.execute_slot_deploy(
                        manifests_dir=manifests_dir, namespace=args.namespace, release_sha=args.release_sha,
                        kube_context=args.kube_context, backend_image=record["backendImage"],
                        frontend_image=record["frontendImage"], schema_release=record["schemaRelease"],
                        artifact_manifest_sha256=record["artifactManifestSha256"], slot=slot,
                        scheduler_mode="standby", include_public_entrypoints=False,
                        execute=args.execute, confirm_release_sha=args.confirm_release_sha,
                    )
                    tke_candidate.record_candidate_deployment(
                        state_path=state_dir / tke_candidate.TKE_CANDIDATE_DEPLOYMENT_STATE_FILENAME,
                        candidate=record, kube_context=args.kube_context, evidence=result,
                    )
                    result = {**result, "runtimeConfigSync": runtime_config_sync}
                except (tke_candidate.TkeCandidateError, tke_release.TkeReleaseError) as exc:
                    hint = _failed_standby_cleanup_hint(
                        repo=repo, state_dir=state_dir, namespace=args.namespace,
                        release_sha=args.release_sha, kube_context=args.kube_context, slot=slot,
                    )
                    raise tke_release.TkeReleaseError(f"{exc}\n{hint}") from exc
            else:
                proof = tke_candidate.require_candidate_deployment(
                    state_path=state_dir / tke_candidate.TKE_CANDIDATE_DEPLOYMENT_STATE_FILENAME,
                    candidate=record, kube_context=args.kube_context,
                )
                tke_release.prepare_deploy(
                    namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context
                )
                result = tke_release.execute_slot_activate(
                    manifests_dir=manifests_dir, namespace=args.namespace, release_sha=args.release_sha,
                    kube_context=args.kube_context, state_path=state_dir / tke_release.TKE_STATE_FILENAME,
                    backend_image=record["backendImage"], frontend_image=record["frontendImage"],
                    schema_release=record["schemaRelease"], artifact_manifest_sha256=record["artifactManifestSha256"],
                    slot=proof["slot"], execute=args.execute, confirm_release_sha=args.confirm_release_sha,
                )
        elif args.action == "tke-rehearse-accept":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit()
            record = _candidate_record(state_dir=state_dir, release_sha=args.release_sha)
            proof = tke_candidate.require_candidate_deployment(
                state_path=state_dir / tke_candidate.TKE_CANDIDATE_DEPLOYMENT_STATE_FILENAME,
                candidate=record, kube_context=args.kube_context,
            )
            acceptance = tke_release.accept_slot_deploy(
                namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context,
                slot=proof["slot"], expected_scheduler_mode="standby", include_public_entrypoints=False,
            )
            if acceptance.get("status") != "accepted":
                raise tke_release.TkeReleaseError("TKE standby candidate acceptance failed")
            result = {**acceptance, "status": "candidate_accepted"}
        elif args.action == "tke-accept":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit(args.release_sha)
            active = tke_release.read_active_release(state_path=state_dir / tke_release.TKE_STATE_FILENAME)
            if active["releaseSha"] != args.release_sha:
                raise tke_release.TkeReleaseError("requested TKE acceptance release is not the recorded active slot")
            result = tke_release.accept_slot_deploy(
                namespace=args.namespace, release_sha=args.release_sha, kube_context=args.kube_context,
                slot=active["slot"], expected_scheduler_mode="active", include_public_entrypoints=True,
            )
        elif args.action == "tke-rollback":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit(args.release_sha)
            result = tke_release.execute_slot_rollback(
                manifests_dir=_resolve_manifests_dir(repo, args.manifests_dir), namespace=args.namespace,
                release_sha=args.release_sha, kube_context=args.kube_context,
                state_path=state_dir / tke_release.TKE_STATE_FILENAME, execute=args.execute,
                confirm_release_sha=args.confirm_release_sha,
            )
        elif args.action == "tke-adopt-active":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit()
            if args.target_state_dir != state_dir:
                raise ReleaseError("active release adoption target must equal --state-dir")
            source_state = args.source_state_dir / tke_release.TKE_STATE_FILENAME
            target_state = args.target_state_dir / tke_release.TKE_STATE_FILENAME
            result = tke_release.import_active_release_state(
                source_state_path=source_state,
                target_state_path=target_state,
                expected_active_release_sha=args.release_sha,
                execute=args.execute,
                confirm_release_sha=args.confirm_release_sha,
                namespace=args.namespace,
                kube_context=args.kube_context,
            )
        elif args.action == "tke-adopt-live":
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit()
            if args.target_state_dir != state_dir:
                raise ReleaseError("live active adoption target must equal --state-dir")
            result = tke_release.adopt_live_active_release_state(
                manifests_dir=_resolve_manifests_dir(repo, args.manifests_dir),
                target_state_path=args.target_state_dir / tke_release.TKE_STATE_FILENAME,
                expected_active_release_sha=args.release_sha,
                execute=args.execute,
                confirm_release_sha=args.confirm_release_sha,
                namespace=args.namespace,
                kube_context=args.kube_context,
            )
        elif args.action == "tke-pipeline":
            result = _run_pipeline(
                repo=repo,
                state_dir=state_dir,
                release_sha=args.release_sha,
                schema_release=args.schema_release,
                kube_context=args.kube_context,
                through=args.through,
                execute=args.execute,
                confirm_release_sha=args.confirm_release_sha,
            )
        elif args.action == "tke-image-worker":
            image_worker_release.validate_target(args.kube_context, "aiops-demo")
            if args.phase == "preview":
                result = image_worker_release.preview(args.release_sha)
            else:
                orchestrator._require_independent_worktree()
                orchestrator._require_clean_commit(args.release_sha)
                if args.phase != "verify" and (not args.execute or args.confirm_release_sha != args.release_sha):
                    raise ReleaseError("worker mutation requires --execute and exact --confirm-release-sha")
                worker_state = state_dir / image_worker_release.STATE_FILE
                if args.phase == "build":
                    result = image_worker_release.build(repo, args.release_sha, worker_state)
                elif args.phase == "provision-secret":
                    result = image_worker_release.provision_secret()
                elif args.phase == "deploy":
                    result = image_worker_release.deploy(repo, worker_state, args.release_sha, args.probe_url)
                elif args.phase == "rollback":
                    result = image_worker_release.rollback(worker_state, args.release_sha)
                else:
                    if not args.urls_file:
                        raise ReleaseError("worker verify requires --urls-file")
                    urls = [line.strip() for line in args.urls_file.read_text().splitlines() if line.strip()]
                    result = image_worker_release.verify(worker_state, args.release_sha, urls)
        elif args.action == "tke-image-canary":
            image_worker_release.validate_target(args.kube_context, "aiops-demo")
            if args.phase in {"provision-secret", "deploy"} and (not args.execute or args.confirm_release_sha != args.release_sha):
                raise ReleaseError("image canary mutation requires exact confirmation")
            orchestrator._require_independent_worktree()
            orchestrator._require_clean_commit(args.release_sha)
            canary_state = state_dir / image_canary_release.STATE_FILE
            if args.phase == "preview":
                result = {"status": "preview", "releaseSha": args.release_sha,
                          "workerReleaseSha": args.worker_release_sha,
                          "resources": ["Job", "NetworkPolicy"],
                          "requiresEnvFile": True}
            elif args.phase == "provision-secret":
                if not args.execute:
                    raise image_canary_release.worker.ImageWorkerReleaseError("provision-secret requires --execute")
                if not args.env_file:
                    raise image_canary_release.worker.ImageWorkerReleaseError("--env-file is required")
                result = image_canary_release.provision_assets(args.env_file)
            elif args.phase == "deploy":
                if args.worker_state_dir is None:
                    raise image_canary_release.worker.ImageWorkerReleaseError("--worker-state-dir is required")
                worker_state = args.worker_state_dir / image_worker_release.STATE_FILE
                if not args.execute or args.confirm_release_sha != args.release_sha:
                    raise image_canary_release.worker.ImageWorkerReleaseError("deploy requires exact confirmation")
                backend_image = args.backend_image
                if not backend_image:
                    raise image_canary_release.worker.ImageWorkerReleaseError("--backend-image is required")
                result = image_canary_release.deploy(repo, canary_state, args.release_sha,
                                                     args.worker_release_sha, worker_state, backend_image)
            else:
                if args.worker_state_dir is None:
                    raise image_canary_release.worker.ImageWorkerReleaseError("--worker-state-dir is required")
                result = image_canary_release.verify(canary_state, args.release_sha)

        else:  # pragma: no cover - argparse guarantees a known action.
            raise ReleaseError("unknown release action")
        _print_result(result, args.format)
        _record_stage_timing(
            state_dir=state_dir,
            stage=args.action,
            release_sha=timing_release_sha,
            started_at=timing_started_at,
            status="success",
            finished_at=_utc_now(),
        )
        return 0
    except (
        ReleaseError,
        release_preflight.ReleasePreflightError,
        tke_adapter.TkePreviewError,
        tke_candidate.TkeCandidateError,
        tke_release.TkeReleaseError,
        OSError,
        ValueError,
    ) as exc:
        _print_result({"error": str(exc), "ok": False, "status": "blocked"}, args.format)
        if timing_state_dir is not None:
            _record_stage_timing(
                state_dir=timing_state_dir,
                stage=args.action,
                release_sha=timing_release_sha,
                started_at=timing_started_at,
                status="failed",
                finished_at=_utc_now(),
                error=exc,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
