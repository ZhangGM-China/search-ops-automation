from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_ROOT = REPO_ROOT / "deploy" / "kubernetes" / "base"


def _documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def _deployment(name: str) -> dict:
    documents = _documents(MANIFEST_ROOT / f"{name}.yaml")
    return next(item for item in documents if item["kind"] == "Deployment")


def _container(deployment: dict, name: str) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return next(item for item in containers if item["name"] == name)


def _literal_environment(container: dict) -> dict[str, str]:
    return {
        item["name"]: item["value"]
        for item in container.get("env", [])
        if "value" in item
    }


def test_runtime_secrets_use_native_kubernetes_secret_only():
    kustomization = yaml.safe_load(
        (MANIFEST_ROOT / "kustomization.yaml").read_text(encoding="utf-8")
    )
    active_resources = [MANIFEST_ROOT / name for name in kustomization["resources"]]
    rendered_source = "\n".join(path.read_text(encoding="utf-8") for path in active_resources)

    assert "secret-provider-class.yaml" not in kustomization["resources"]
    assert "SecretProviderClass" not in rendered_source
    assert "secrets-store.csi.k8s.io" not in rendered_source
    assert "secretProviderClass" not in rendered_source

    for name in ("backend", "scheduler"):
        deployment = _deployment(name)
        container = _container(deployment, name)
        secret_refs = [item["secretRef"] for item in container["envFrom"] if "secretRef" in item]

        assert secret_refs == [
            {"name": "aiops-runtime-secrets", "optional": False},
            {"name": "qg-image-assets", "optional": False},
        ]


def test_runtime_serviceaccount_uses_only_the_dedicated_tcr_pull_secret():
    service_account = _documents(MANIFEST_ROOT / "service-account.yaml")[0]

    assert service_account["kind"] == "ServiceAccount"
    assert service_account["metadata"]["name"] == "aiops-runtime"
    assert service_account["imagePullSecrets"] == [{"name": "aiops-tcr-pull"}]


def test_multi_replica_backend_uses_preprovisioned_rwx_runtime_data_without_schema_change():
    backend = _deployment("backend")
    scheduler = _deployment("scheduler")

    assert backend["spec"]["replicas"] == 2
    assert backend["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
    }
    assert scheduler["spec"]["replicas"] == 1
    assert scheduler["spec"]["strategy"] == {"type": "Recreate"}

    for deployment, container_name in ((backend, "backend"), (scheduler, "scheduler")):
        pod_spec = deployment["spec"]["template"]["spec"]
        container = _container(deployment, container_name)
        volumes = {item["name"]: item for item in pod_spec["volumes"]}

        assert pod_spec["serviceAccountName"] == "aiops-runtime"
        assert pod_spec["automountServiceAccountToken"] is True
        assert volumes["runtime-data"] == {
            "name": "runtime-data",
            "persistentVolumeClaim": {"claimName": "aiops-runtime-data", "readOnly": False},
        }
        assert volumes["runtime-config"] == {
            "name": "runtime-config",
            "configMap": {
                "name": "aiops-runtime-config",
                "items": [{"key": "config.yaml", "path": "config.yaml"}],
            },
        }
        mounts = {item["mountPath"]: item for item in container["volumeMounts"]}
        assert mounts["/app/data"]["name"] == "runtime-data"
        assert mounts["/app/runtime-config"] == {
            "name": "runtime-config",
            "mountPath": "/app/runtime-config",
            "readOnly": True,
        }
        assert mounts["/tmp"]["name"] == "tmp"

        environment = _literal_environment(container)
        expected_environment = {
            "AIOPS_CONFIG_PATH": "/app/runtime-config/config.yaml",
            "AIOPS_CONFIG_PERSISTENCE": "kubernetes_configmap",
            "AIOPS_CONFIGMAP_NAME": "aiops-runtime-config",
            "AIOPS_CONFIGMAP_NAMESPACE": "aiops-demo",
            "BUSINESS_HTTP_STATE_DIR": "/app/data/runtime/business_http",
            "TOP_QUERY_TASK_LOCK_DIR": "/app/data/runtime/top_query_task_locks",
        }
        assert {
            key: environment[key]
            for key in expected_environment
        } == expected_environment


def test_rwx_runtime_data_is_writable_by_the_non_root_workloads():
    for deployment, container_name in (
        (_deployment("backend"), "backend"),
        (_deployment("scheduler"), "scheduler"),
    ):
        pod_spec = deployment["spec"]["template"]["spec"]
        mounts = {item["mountPath"]: item for item in _container(deployment, container_name)["volumeMounts"]}

        assert mounts["/app/data"]["name"] == "runtime-data"
        assert pod_spec["securityContext"] == {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "fsGroup": 10001,
            "fsGroupChangePolicy": "OnRootMismatch",
            "seccompProfile": {"type": "RuntimeDefault"},
        }


def test_aiops_workloads_are_constrained_to_shared_runtime_nodes():
    for name in ("backend", "frontend", "scheduler"):
        pod_spec = _deployment(name)["spec"]["template"]["spec"]

        assert pod_spec["nodeSelector"] == {
            "aiops.example.test/runtime": "shared"
        }
        assert "tolerations" not in pod_spec


def test_release_identity_and_editable_runtime_configuration_are_separate_configmaps():
    documents = _documents(MANIFEST_ROOT / "configmap.yaml")
    assert len(documents) == 1
    release_info = documents[0]
    assert release_info["metadata"]["name"] == "aiops-release-info"
    assert set(release_info["data"]) == {
        "ENV",
        "RELEASE_SHA",
        "SCHEMA_RELEASE",
        "ARTIFACT_MANIFEST_SHA256",
        "XIAOHONGSHU_MCP_ENABLED",
    }

    for name in ("backend", "scheduler"):
        container = _container(_deployment(name), name)
        refs = [item["configMapRef"] for item in container["envFrom"] if "configMapRef" in item]
        assert refs == [{"name": "aiops-release-info"}]

    frontend = _container(_deployment("frontend"), "frontend")
    frontend_refs = {
        item["name"]: item["valueFrom"]["configMapKeyRef"]
        for item in frontend["env"]
        if "valueFrom" in item and "configMapKeyRef" in item["valueFrom"]
    }
    assert set(frontend_refs) == {
        "RELEASE_SHA",
        "SCHEMA_RELEASE",
        "ARTIFACT_MANIFEST_SHA256",
    }
    assert {reference["name"] for reference in frontend_refs.values()} == {"aiops-release-info"}


def test_scheduler_defaults_to_active_mode_and_has_a_status_contract():
    scheduler = _deployment("scheduler")
    container = _container(scheduler, "scheduler")
    environment = _literal_environment(container)

    assert container["command"] == ["python", "main.py"]
    assert environment["AIOPS_SCHEDULER_MODE"] == "active"
    assert environment["SCHEDULER_EXPECTED_MODE"] == "active"
    assert environment["SCHEDULER_STATUS_FILE"] == "/tmp/aiops-scheduler/scheduler-status.json"
    assert environment["SCHEDULER_RUNTIME_DIR"] == "/tmp/aiops-scheduler"
    assert environment["BUSINESS_HTTP_STATE_DIR"] == "/app/data/runtime/business_http"
    assert container["resources"]["requests"]["memory"] == "512Mi"
    assert container["resources"]["limits"]["memory"] == "1Gi"


def test_scheduler_readiness_rejects_stale_or_other_process_status(tmp_path):
    import json
    import os
    import subprocess
    import sys
    import time

    command = _container(_deployment("scheduler"), "scheduler")["readinessProbe"]["exec"]["command"][2]
    status = tmp_path / "status.json"
    env = {**os.environ, "SCHEDULER_STATUS_FILE": str(status), "SCHEDULER_EXPECTED_MODE": "active"}
    for pid, age, expected in ((1, 0, 0), (1, 120, 1), (2, 0, 1)):
        status.write_text(json.dumps({"ready": True, "mode": "active", "state": "ready",
                                     "pid": pid, "updatedAt": time.time() - age}))
        result = subprocess.run([sys.executable, "-c", command], env=env, capture_output=True)
        assert result.returncode == expected

def test_frontend_service_uses_node_port_for_qcloud_ingress():
    services = _documents(MANIFEST_ROOT / "services.yaml")
    frontend = next(item for item in services if item["metadata"]["name"] == "frontend")

    assert frontend["spec"]["type"] == "NodePort"
    assert frontend["spec"]["ports"] == [
        {
            "name": "http",
            "port": 80,
            "protocol": "TCP",
            "targetPort": 8080,
        }
    ]


def test_qcloud_ingress_sends_all_customer_paths_to_frontend_proxy_only():
    kustomization = yaml.safe_load(
        (MANIFEST_ROOT / "kustomization.yaml").read_text(encoding="utf-8")
    )
    ingress = _documents(MANIFEST_ROOT / "ingress.yaml")[0]

    assert "ingress.yaml" in kustomization["resources"]
    assert ingress["spec"]["ingressClassName"] == "qcloud"
    assert ingress["metadata"]["annotations"] == {
        "kubernetes.io/ingress.class": "qcloud"
    }
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert [item["path"] for item in paths] == ["/api", "/"]
    assert {
        item["backend"]["service"]["name"]
        for item in paths
    } == {"frontend"}


def test_network_policy_allows_external_traffic_only_to_frontend_and_keeps_api_internal():
    policies = _documents(MANIFEST_ROOT / "network-policy.yaml")
    frontend = next(item for item in policies if item["metadata"]["name"] == "allow-frontend-ingress")
    backend = next(item for item in policies if item["metadata"]["name"] == "allow-backend-ingress-egress")

    assert frontend["spec"]["ingress"] == [
        {
            "from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
            "ports": [{"port": 8080, "protocol": "TCP"}],
        }
    ]
    assert backend["spec"]["ingress"] == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "frontend"}
                    }
                }
            ],
            "ports": [{"port": 8000, "protocol": "TCP"}],
        }
    ]


def test_browser_worker_connection_requires_both_network_policy_sides():
    policies = _documents(MANIFEST_ROOT / 'network-policy.yaml')
    policy = next(p for p in policies if p['metadata']['name'] == 'allow-main-image-worker-egress')
    assert policy['spec']['podSelector']['matchExpressions'] == [
        {'key': 'app.kubernetes.io/name', 'operator': 'In', 'values': ['backend', 'scheduler']}
    ]
    assert policy['spec']['egress'] == [
        {'to': [{'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'qg-atlas-browser-worker',
                                                'app.kubernetes.io/part-of': 'aiops-image-sync'}}}],
         'ports': [{'port': 8790, 'protocol': 'TCP'}]}
    ]
    policy = next(p for p in policies if p['metadata']['name'] == 'allow-image-worker-clients')
    assert policy['spec']['podSelector']['matchLabels']['app.kubernetes.io/name'] == 'qg-atlas-browser-worker'
    assert policy['spec']['policyTypes'] == ['Ingress']
    assert policy['spec']['ingress'][0]['ports'] == [{'port':8790,'protocol':'TCP'}]


def test_rendered_worker_network_selectors_match_the_independent_worker_labels():
    from scripts.release.tke_release import render_release_manifests

    rendered = render_release_manifests(
        manifests_dir=MANIFEST_ROOT,
        release_sha='a' * 40,
        backend_image='registry.example.test/aiops/backend@sha256:' + 'b' * 64,
        frontend_image='registry.example.test/aiops/frontend@sha256:' + 'c' * 64,
        schema_release='zha33', artifact_manifest_sha256='d' * 64,
    )
    policies = {item['metadata']['name']: item for item in yaml.safe_load_all(rendered)
                if item['kind'] == 'NetworkPolicy'}
    # The independently released Worker does not carry the main application's labels.
    worker_labels = {'app.kubernetes.io/name': 'qg-atlas-browser-worker',
                     'app.kubernetes.io/part-of': 'aiops-image-sync'}
    ingress_selector = policies['allow-image-worker-clients']['spec']['podSelector']['matchLabels']
    assert all(worker_labels.get(key) == value for key, value in ingress_selector.items())
    egress_selector = policies['allow-main-image-worker-egress']['spec']['egress'][0]['to'][0]['podSelector']['matchLabels']
    assert all(worker_labels.get(key) == value for key, value in egress_selector.items())
    for name in ('allow-backend-ingress-egress', 'allow-scheduler-egress'):
        assert len(policies[name]['spec']['egress']) == 1
