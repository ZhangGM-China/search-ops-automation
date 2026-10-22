from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_PATH = PROJECT_ROOT / "scripts" / "release" / "tke_adapter.py"
SHA = "a" * 40


def _load_adapter():
    spec = importlib.util.spec_from_file_location("tke_adapter_contract", ADAPTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def adapter():
    return _load_adapter()


def test_build_preview_is_bound_to_tke_resources_and_release_identity(adapter, tmp_path):
    manifests = tmp_path / "deploy" / "kubernetes" / "base"
    manifests.mkdir(parents=True)

    plan = adapter.build_preview(
        release_sha=SHA,
        manifests_dir=manifests,
        backend_image=f"registry.example.test/aiops/backend@sha256:{'b' * 64}",
        frontend_image=f"registry.example.test/aiops/frontend@sha256:{'c' * 64}",
    )

    assert plan["status"] == "tke_preview_only"
    assert plan["releaseSha"] == SHA
    assert plan["target"] == {
        "cluster": "SearchOps-MCP",
        "clusterId": "cls-01ckm389",
        "namespace": "aiops-demo",
        "registry": "searchops-registry",
        "registryNamespace": "aiops",
        "host": "ops.example.test",
    }
    assert plan["images"]["backend"]["reference"].endswith("@sha256:" + "b" * 64)
    assert [item["name"] for item in plan["phases"]] == [
        "candidate",
        "prepare",
        "rehearse-pull",
        "rehearse-mysql",
        "rehearse-deploy",
        "activate",
        "accept",
        "rollback",
    ]
    assert "migrate-schema" not in [item["name"] for item in plan["phases"]]
    assert plan["phases"][3]["requires"] == ["cluster-pull-proof"]
    assert plan["phases"][4]["schedulerMode"] == "standby"
    assert plan["phases"][4]["rendersIngress"] is False
    assert plan["phases"][4]["requires"] == [
        "cluster-pull-proof",
        "mysql-rehearsal-proof",
    ]
    assert plan["phases"][5]["schedulerMode"] == "active"
    assert plan["phases"][5]["requires"] == [
        "cluster-pull-proof",
        "mysql-rehearsal-proof",
        "standby-candidate-proof",
    ]
    assert plan["safety"] == {
        "mutationsAllowed": False,
        "kubectlApplyExecuted": False,
        "cloudWritesExecuted": False,
        "productionTrafficChanged": False,
    }


def test_build_preview_rejects_execution_requests_fail_closed(adapter, tmp_path):
    with pytest.raises(adapter.TkePreviewError, match="execution is disabled"):
        adapter.build_preview(
            release_sha=SHA,
            manifests_dir=tmp_path,
            execute=True,
        )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"release_sha": "not-a-sha"}, "release SHA is invalid"),
        ({"release_sha": SHA, "namespace": "AIOPS_PROD"}, "namespace is invalid"),
        ({"release_sha": SHA, "backend_image": "searchops-registry/aiops/backend:latest"}, "backend image reference is invalid"),
    ],
)
def test_build_preview_validates_inputs(adapter, tmp_path, kwargs, message):
    kwargs.setdefault("manifests_dir", tmp_path)
    with pytest.raises(adapter.TkePreviewError, match=message):
        adapter.build_preview(**kwargs)


def test_cli_preview_does_not_accept_execute_or_kubeconfig(adapter):
    parser = adapter.build_parser()
    args = parser.parse_args(["--release-sha", SHA])
    assert args.release_sha == SHA
    with pytest.raises(SystemExit):
        parser.parse_args(["--release-sha", SHA, "--execute"])
