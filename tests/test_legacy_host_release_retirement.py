"""Contract for retiring the former SSH/Compose production release path."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_ENTRY = ROOT / "scripts" / "release" / "release.py"

LEGACY_HOST_PATHS = (
    Path("docker-compose.release.yml"),
    Path("deploy/nginx.conf"),
    Path("scripts/release/bootstrap.py"),
    Path("scripts/release/helper_upgrade.py"),
    Path("scripts/release/release_bundle.py"),
    Path("scripts/release/build_release_artifact.sh"),
    Path("scripts/release/preflight_release.sh"),
    Path("scripts/release/build_release_images.sh"),
    Path("scripts/release/export_release_images.py"),
    Path("scripts/release/receive_release_artifact.py"),
    Path("scripts/release/verify_git_release_identity.py"),
    Path("scripts/release/remote_action_handler.py"),
    Path("scripts/release/remote_contract.py"),
    Path("scripts/release/remote_image_receive_handler.py"),
    Path("scripts/release/remote_receive_handler.py"),
    Path("scripts/release/remote_wrapper.py"),
    Path("scripts/release/prod_backup.sh"),
    Path("scripts/release/prod_preflight.py"),
    Path("scripts/release/prod_readiness.py"),
    Path("scripts/release/run_migration_with_lock.py"),
    Path("scripts/release/dba_runtime.py"),
    Path("scripts/release/dba_window.py"),
    Path("scripts/release/schema_risk.py"),
    Path("scripts/release/registry_preflight.py"),
    Path("scripts/release/plane_evidence.py"),
    Path("scripts/release/run_remote_release.sh"),
)


def _release_actions() -> set[str]:
    spec = importlib.util.spec_from_file_location("retired_release_entry", RELEASE_ENTRY)
    assert spec is not None and spec.loader is not None
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    parser = release.build_parser()
    action = next(
        item
        for item in parser._actions
        if isinstance(item, argparse._SubParsersAction)
    )
    return set(action.choices)


def test_legacy_host_release_chain_is_removed_and_cli_is_tke_only():
    assert [str(path) for path in LEGACY_HOST_PATHS if (ROOT / path).exists()] == []
    actions = _release_actions()
    assert actions
    assert all(action.startswith("tke-") for action in actions)
    assert "tke-deploy" not in actions
