import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "release" / "tke_adapter.py"
SHA = "a" * 40
BACKEND = "registry.example.test/aiops/backend@sha256:" + "1" * 64
FRONTEND = "registry.example.test/aiops/frontend@sha256:" + "2" * 64


def _load_adapter():
    spec = importlib.util.spec_from_file_location("test_tke_adapter", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_preview_declares_minimal_lifecycle_without_schema_migration_gate():
    adapter = _load_adapter()

    preview = adapter.build_preview(
        release_sha=SHA,
        manifests_dir=ROOT / "deploy" / "kubernetes" / "base",
        backend_image=BACKEND,
        frontend_image=FRONTEND,
    )

    phases = preview["phases"]
    assert [phase["name"] for phase in phases] == [
        "candidate",
        "prepare",
        "rehearse-pull",
        "rehearse-mysql",
        "rehearse-deploy",
        "activate",
        "accept",
        "rollback",
    ]
    assert phases[3]["customerTraffic"] is False
    assert phases[3]["requires"] == ["cluster-pull-proof"]
    assert phases[4]["requires"] == [
        "cluster-pull-proof",
        "mysql-rehearsal-proof",
    ]
    assert phases[5]["requires"] == [
        "cluster-pull-proof",
        "mysql-rehearsal-proof",
        "standby-candidate-proof",
    ]
