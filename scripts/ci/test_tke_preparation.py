"""Contract tests for the isolated AIOPS TKE base manifests.

This test is intentionally independent from the application test conftest.  It
only validates the Kubernetes contract and never contacts a cluster.
"""

from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "deploy" / "kubernetes" / "base"
NAMESPACE = "aiops-demo"


def _documents() -> list[dict]:
    kustomization = yaml.safe_load((BASE / "kustomization.yaml").read_text())
    docs: list[dict] = []
    for resource in kustomization["resources"]:
        path = BASE / resource
        docs.extend(doc for doc in yaml.safe_load_all(path.read_text()) if doc)
    return docs


def _by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [doc for doc in docs if doc.get("kind") == kind]


def _deployment(docs: list[dict], name: str) -> dict:
    return next(doc for doc in _by_kind(docs, "Deployment") if doc["metadata"]["name"] == name)


def test_kustomization_has_isolated_namespace_and_expected_resources() -> None:
    assert BASE.is_dir()
    kustomization = yaml.safe_load((BASE / "kustomization.yaml").read_text())
    assert kustomization["apiVersion"] == "kustomize.config.k8s.io/v1beta1"
    assert kustomization["namespace"] == NAMESPACE
    assert "namespace.yaml" in kustomization["resources"]
    docs = _documents()
    assert {doc["kind"] for doc in docs} >= {
        "Namespace",
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "Deployment",
        "Service",
        "Ingress",
        "NetworkPolicy",
        "PodDisruptionBudget",
        "ConfigMap",
    }
    assert _by_kind(docs, "Namespace")[0]["metadata"]["name"] == NAMESPACE


def test_all_namespaced_resources_are_isolated() -> None:
    docs = _documents()
    cluster_scoped = {"Namespace", "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition"}
    for doc in docs:
        if doc["kind"] not in cluster_scoped:
            assert doc["metadata"].get("namespace") == NAMESPACE, doc
    names = {(doc["kind"], doc["metadata"]["name"]) for doc in docs}
    assert ("Service", "api") in names
    assert ("Service", "frontend") in names
    assert ("Service", "scheduler") not in names


def test_deployments_have_release_identity_and_health_contract() -> None:
    docs = _documents()
    backend = _deployment(docs, "backend")
    frontend = _deployment(docs, "frontend")
    scheduler = _deployment(docs, "scheduler")
    assert backend["spec"]["replicas"] == 2
    assert frontend["spec"]["replicas"] == 2
    assert scheduler["spec"]["replicas"] == 1
    assert scheduler["spec"]["strategy"]["type"] == "Recreate"
    for deployment in (backend, frontend, scheduler):
        pod = deployment["spec"]["template"]
        assert pod["metadata"]["labels"]["app.kubernetes.io/part-of"] == "aiops"
        container = pod["spec"]["containers"][0]
        assert "@sha256:" in container["image"]
        env_names = {item["name"] for item in container.get("env", [])}
        assert {"RELEASE_SHA", "SCHEMA_RELEASE", "ARTIFACT_MANIFEST_SHA256"} <= env_names
        assert "readinessProbe" in container
        assert "livenessProbe" in container
        assert "startupProbe" in container
    assert backend["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"] == "/api/health/ready"
    assert backend["spec"]["template"]["spec"]["containers"][0]["startupProbe"]["httpGet"]["path"] == "/api/health/startup"
    assert frontend["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"] == "/release-info.json"
    assert scheduler["spec"]["template"]["spec"]["containers"][0]["command"] == ["python", "main.py"]


def test_services_and_ingress_preserve_customer_host_and_api_route() -> None:
    docs = _documents()
    services = {doc["metadata"]["name"]: doc for doc in _by_kind(docs, "Service")}
    assert services["api"]["spec"]["type"] == "ClusterIP"
    # Tencent Cloud's qcloud Ingress only accepts a NodePort or LoadBalancer
    # backend.  The CLB itself is owned by the Ingress, so frontend must be
    # NodePort rather than creating a second public load balancer.
    assert services["frontend"]["spec"]["type"] == "NodePort"
    assert services["api"]["spec"]["ports"] == [{"name": "http", "port": 8000, "protocol": "TCP", "targetPort": 8000}]
    assert services["frontend"]["spec"]["ports"] == [
        {"name": "http", "port": 80, "protocol": "TCP", "targetPort": 8080}
    ]
    ingress = _by_kind(docs, "Ingress")[0]
    assert ingress["spec"]["rules"][0]["host"] == "ops.example.test"
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert paths[0]["path"] == "/api"
    assert paths[0]["backend"]["service"]["name"] == "frontend"
    assert paths[1]["path"] == "/"
    assert paths[1]["backend"]["service"]["name"] == "frontend"


def test_rbac_secret_injection_and_policies_are_non_secret_and_scoped() -> None:
    docs = _documents()
    role = _by_kind(docs, "Role")[0]
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": ["aiops-runtime-config"],
            "verbs": ["get", "patch"],
        },
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": ["aiops-release-info-blue", "aiops-release-info-green"],
            "verbs": ["get"],
        },
    ]
    serialized = "\n".join(
        (BASE / resource).read_text(encoding="utf-8")
        for resource in yaml.safe_load((BASE / "kustomization.yaml").read_text())["resources"]
    ).lower()
    assert "secretproviderclass" not in serialized
    assert "secrets-store.csi.k8s.io" not in serialized
    for forbidden in ("password:", "access_key", "secret_key", "sk-"):
        assert forbidden not in serialized
    deployments = _by_kind(docs, "Deployment")
    for deployment in deployments:
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        if deployment["metadata"]["name"] in {"backend", "scheduler"}:
            assert {"name": "aiops-runtime-secrets", "optional": False} in [
                item["secretRef"] for item in container.get("envFrom", []) if "secretRef" in item
            ]
    policies = _by_kind(docs, "NetworkPolicy")
    assert len(policies) >= 1
    for policy in policies:
        assert policy["metadata"]["namespace"] == NAMESPACE
        assert "podSelector" in policy["spec"]
    pdbs = _by_kind(docs, "PodDisruptionBudget")
    assert {pdb["metadata"]["name"] for pdb in pdbs} >= {"backend", "frontend", "scheduler"}


def test_release_entrypoint_exposes_local_only_tke_preview() -> None:
    release = ROOT / "scripts" / "release" / "release.py"
    sha = "0" * 40
    result = subprocess.run(
        [sys.executable, str(release), "--format", "json", "tke-preview", "--release-sha", sha],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "tke_preview_only"
    assert [phase["name"] for phase in payload["phases"]] == [
        "candidate",
        "prepare",
        "rehearse-pull",
        "rehearse-mysql",
        "rehearse-deploy",
        "activate",
        "accept",
        "rollback",
    ]
    assert "migrate-schema" not in [phase["name"] for phase in payload["phases"]]
    assert payload["phases"][3]["requires"] == ["cluster-pull-proof"]
    assert payload["phases"][4]["schedulerMode"] == "standby"
    assert payload["phases"][4]["rendersIngress"] is False
    assert payload["phases"][4]["requires"] == [
        "cluster-pull-proof",
        "mysql-rehearsal-proof",
    ]
    assert payload["phases"][5]["schedulerMode"] == "active"
    assert payload["safety"] == {
        "cloudWritesExecuted": False,
        "kubectlApplyExecuted": False,
        "mutationsAllowed": False,
        "productionTrafficChanged": False,
    }


def test_tke_preview_rejects_execution_flag() -> None:
    from scripts.release.tke_adapter import TkePreviewError, build_preview

    with pytest.raises(TkePreviewError, match="execution is disabled"):
        build_preview(
            release_sha="0" * 40,
            manifests_dir=BASE,
            execute=True,
        )
