"""Local-only TKE release planning.

This module intentionally has no Kubernetes or Tencent Cloud client dependency.
It produces a reviewable, immutable deployment plan and fails closed for any
execution request. Applying the plan belongs to a separately authorized phase.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


class TkePreviewError(ValueError):
    """Raised when a TKE preview cannot be safely constructed."""


SHA40 = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_DIGEST = re.compile(
    r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?(?:/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)*"
    r"(?:/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)?@sha256:[0-9a-f]{64}\Z"
)
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")

TARGET = {
    "cluster": "searchops",
    "clusterId": "cls-demo0000",
    "namespace": "aiops-demo",
    "registry": "searchops-registry",
    "registryNamespace": "aiops",
    "host": "ops.example.test",
}


def _validate_sha(value: str) -> str:
    if not isinstance(value, str) or SHA40.fullmatch(value) is None:
        raise TkePreviewError("release SHA is invalid")
    return value


def _validate_namespace(value: str, label: str) -> str:
    if not isinstance(value, str) or DNS_LABEL.fullmatch(value) is None:
        raise TkePreviewError(f"{label} is invalid")
    return value


def _validate_image(value: str, component: str) -> str:
    if not isinstance(value, str) or IMAGE_DIGEST.fullmatch(value) is None:
        raise TkePreviewError(f"{component} image reference is invalid")
    return value


def _manifest_inventory(root: Path) -> list[dict[str, Any]]:
    if not root.is_absolute() or root == Path("/") or root.is_symlink() or not root.is_dir():
        raise TkePreviewError("manifests directory is invalid")
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".yaml", ".yml"}
    )
    inventory: list[dict[str, Any]] = []
    for path in files:
        if path.is_symlink():
            raise TkePreviewError("manifest inventory contains a symlink")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise TkePreviewError("manifest inventory is unreadable") from exc
        inventory.append(
            {
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    return inventory


def build_preview(
    *,
    release_sha: str,
    manifests_dir: Path,
    backend_image: str = "registry.example.test/aiops/backend@sha256:" + "0" * 64,
    frontend_image: str = "registry.example.test/aiops/frontend@sha256:" + "0" * 64,
    namespace: str = TARGET["namespace"],
    registry_namespace: str = TARGET["registryNamespace"],
    execute: bool = False,
) -> dict[str, Any]:
    """Return a deterministic TKE lifecycle plan without performing mutations."""
    if execute:
        raise TkePreviewError("TKE preview execution is disabled")
    release_sha = _validate_sha(release_sha)
    namespace = _validate_namespace(namespace, "namespace")
    registry_namespace = _validate_namespace(registry_namespace, "registry namespace")
    backend_image = _validate_image(backend_image, "backend")
    frontend_image = _validate_image(frontend_image, "frontend")
    inventory = _manifest_inventory(Path(manifests_dir))
    phases = [
        {
            "name": "candidate",
            "mode": "tcr-build-push-digest-verify",
            "mutationsAllowed": False,
        },
        {
            "name": "prepare",
            "mode": "cluster-rbac-runtime-preflight",
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
            "mode": "temporary-backend-schema-readback",
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
    ]
    target = {**TARGET, "namespace": namespace, "registryNamespace": registry_namespace}
    return {
        "adapter": "tke",
        "version": 2,
        "status": "tke_preview_only",
        "releaseSha": release_sha,
        "target": target,
        "images": {
            "backend": {"reference": backend_image, "releaseSha": release_sha},
            "frontend": {"reference": frontend_image, "releaseSha": release_sha},
        },
        "manifests": {"directory": str(Path(manifests_dir)), "files": inventory},
        "phases": phases,
        "safety": {
            "mutationsAllowed": False,
            "kubectlApplyExecuted": False,
            "cloudWritesExecuted": False,
            "productionTrafficChanged": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a local-only TKE release preview")
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--manifests-dir", type=Path, default=Path("deploy/kubernetes/base"))
    parser.add_argument("--backend-image", required=False)
    parser.add_argument("--frontend-image", required=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = build_preview(
            release_sha=args.release_sha,
            manifests_dir=args.manifests_dir,
            **({"backend_image": args.backend_image} if args.backend_image else {}),
            **({"frontend_image": args.frontend_image} if args.frontend_image else {}),
        )
    except TkePreviewError as exc:
        print(json.dumps({"ok": False, "status": "blocked", "error": str(exc)}))
        return 1
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
