import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "release" / "tke_release.py"


def _load():
    spec = importlib.util.spec_from_file_location("test_tke_release", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tke():
    return _load()


SHA = "a" * 40
BACKEND = "registry.example.test/aiops/backend@sha256:" + "1" * 64
FRONTEND = "registry.example.test/aiops/frontend@sha256:" + "2" * 64
CONTEXT = "cls-demo0000-000000000000-context-default"


def test_acceptance_excludes_draining_pods_but_keeps_unready_replacements(tke):
    replacement = {"metadata": {"name": "new"}, "status": {"phase": "Pending"}}
    draining = {"metadata": {"name": "old", "deletionTimestamp": "2026-10-15T00:00:00Z"}}
    def run(argv, **kwargs):
        return tke.CommandResult(0, json.dumps({"items": [draining, replacement]}).encode(), b"")
    assert tke._pod_list_for_component(kube_context=CONTEXT, namespace="aiops-demo",
                                      component="backend", slot="green", runner=run) == [replacement]


def _kustomize_output(tke) -> bytes:
    """Return a deterministic stand-in for ``kubectl kustomize`` output.

    The unit test deliberately keeps the external binary mocked, while adding a
    label that only the Kustomize output (not the raw source files) provides.
    That makes accidental fall-back to hand-reading the source manifests fail.
    """
    root = ROOT / "deploy/kubernetes/base"
    kustomization = tke.yaml.safe_load(
        (root / "kustomization.yaml").read_text(encoding="utf-8")
    )
    documents = []
    for resource in kustomization["resources"]:
        documents.extend(
            item
            for item in tke.yaml.safe_load_all((root / resource).read_text(encoding="utf-8"))
            if item
        )
    for document in documents:
        metadata = document.setdefault("metadata", {})
        labels = metadata.setdefault("labels", {})
        labels["app.kubernetes.io/managed-by"] = "kustomize"
    return tke.yaml.safe_dump_all(documents, allow_unicode=True, sort_keys=False).encode("utf-8")


def _runner_with_kustomize_output(tke, calls):
    rendered = _kustomize_output(tke)

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[:2] == ["kubectl", "kustomize"]:
            return tke.CommandResult(0, rendered, b"")
        return tke.CommandResult(0, b"{}\n", b"")

    return run


def _context_metadata(tke, context=CONTEXT, cluster="cls-demo0000"):
    return tke.CommandResult(
        0,
        json.dumps(
            {
                "clusters": [{"name": cluster}],
                "contexts": [
                    {"name": context, "context": {"cluster": cluster}}
                ],
                "current-context": context,
            }
        ).encode(),
        b"",
    )


def _runner_with_context_and_kustomize(tke, calls):
    rendered = _kustomize_output(tke)

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[:2] == ["kubectl", "kustomize"]:
            return tke.CommandResult(0, rendered, b"")
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        return tke.CommandResult(0, b"{}\n", b"")

    return run


def _accepted_deployments(*, scheduler_mode="active"):
    images = {
        "backend": BACKEND,
        "frontend": FRONTEND,
        "scheduler": BACKEND,
    }
    replicas = {"backend": 2, "frontend": 2, "scheduler": 1}
    items = []
    for name in ("backend", "frontend", "scheduler"):
        container = {"name": name, "image": images[name]}
        if name == "scheduler":
            container["env"] = [
                {"name": "AIOPS_SCHEDULER_MODE", "value": scheduler_mode},
                {"name": "SCHEDULER_EXPECTED_MODE", "value": scheduler_mode},
            ]
        items.append(
            {
                "metadata": {
                    "name": name,
                    "annotations": {"aiops.example.test/release-sha": SHA},
                },
                "spec": {
                    "replicas": replicas[name],
                    "template": {"spec": {"containers": [container]}},
                },
                "status": {
                    "readyReplicas": replicas[name],
                    "updatedReplicas": replicas[name],
                    "availableReplicas": replicas[name],
                },
            }
        )
    return {"items": items}


def _accepted_pods(*, scheduler_mode="active"):
    images = {
        "backend": BACKEND,
        "frontend": FRONTEND,
        "scheduler": BACKEND,
    }
    count = {"backend": 2, "frontend": 2, "scheduler": 1}
    return {
        component: {
            "items": [
                {
                    "metadata": {"name": f"{component}-{index}"},
                    "status": {
                        "containerStatuses": [
                            {
                                "name": component,
                                "ready": True,
                                "image": images[component],
                                "imageID": f"docker-pullable://{images[component]}",
                            }
                        ]
                    },
                }
                for index in range(count[component])
            ]
        }
        for component in ("backend", "frontend", "scheduler")
    }


def _subprocess_runner(tke):
    def run(argv, **kwargs):
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=kwargs.get("timeout_seconds"),
        )
        return tke.CommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )

    return run


def test_render_release_manifests_binds_one_release_and_digest(tke):
    calls = []
    rendered = tke.render_release_manifests(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        release_sha=SHA,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        runner=_runner_with_kustomize_output(tke, calls),
    )

    documents = list(tke.yaml.safe_load_all(rendered))
    deployments = {item["metadata"]["name"]: item for item in documents if item.get("kind") == "Deployment"}
    assert deployments["backend"]["spec"]["template"]["spec"]["containers"][0]["image"] == BACKEND
    assert deployments["scheduler"]["spec"]["template"]["spec"]["containers"][0]["image"] == BACKEND
    assert deployments["frontend"]["spec"]["template"]["spec"]["containers"][0]["image"] == FRONTEND
    config = next(item for item in documents if item.get("kind") == "ConfigMap")
    assert config["data"] == {
        "ENV": "prod",
        "RELEASE_SHA": SHA,
        "SCHEMA_RELEASE": "zha32",
        "ARTIFACT_MANIFEST_SHA256": "3" * 64,
        "XIAOHONGSHU_MCP_ENABLED": "false",
    }
    assert all(
        item["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "kustomize"
        for item in documents
    )
    assert calls[0][0] == ("kubectl", "kustomize", str(ROOT / "deploy/kubernetes/base"))


def test_plan_is_read_only_and_keeps_only_the_runtime_rehearsal_before_deploy(tke, tmp_path):
    calls = []

    result = tke.build_plan(
        repo=ROOT,
        manifests_dir=ROOT / "deploy/kubernetes/base",
        release_sha=SHA,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        runner=_runner_with_kustomize_output(tke, calls),
    )

    assert result["status"] == "tke_plan"
    assert [phase["name"] for phase in result["phases"]] == [
        "candidate",
        "prepare",
        "rehearse-pull",
        "rehearse-mysql",
        "rehearse-deploy",
        "activate",
        "accept",
        "rollback",
    ]
    assert result["phases"][3]["requires"] == ["cluster-pull-proof"]
    assert result["phases"][4]["requires"] == [
        "cluster-pull-proof",
        "mysql-rehearsal-proof",
    ]
    assert result["phases"][4]["schedulerMode"] == "standby"
    assert result["phases"][4]["rendersIngress"] is False
    assert result["phases"][5]["requires"] == [
        "cluster-pull-proof",
        "mysql-rehearsal-proof",
        "standby-candidate-proof",
    ]
    assert result["phases"][5]["schedulerMode"] == "active"
    assert result["safety"]["mutationsAllowed"] is False
    assert not any("apply" in call[0] for call in calls)


def test_execute_requires_explicit_confirmation(tke):
    with pytest.raises(tke.TkeReleaseError, match="confirm-release-sha"):
        tke.execute_deploy(
            manifests_dir=ROOT / "deploy/kubernetes/base",
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            state_path=Path("/tmp/tke-release-state.json"),
            backend_image=BACKEND,
            frontend_image=FRONTEND,
            schema_release="zha32",
            artifact_manifest_sha256="3" * 64,
            execute=True,
            confirm_release_sha=None,
            runner=lambda *args, **kwargs: pytest.fail("must fail before command"),
        )


def test_execute_does_not_apply_when_server_side_dry_run_fails(tke):
    calls = []
    rendered = _kustomize_output(tke)

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[:2] == ["kubectl", "kustomize"]:
            return tke.CommandResult(0, rendered, b"")
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:5] == ["apply", "--server-side"] and "--dry-run=server" in argv:
            return tke.CommandResult(1, b"", b"forbidden")
        return tke.CommandResult(0, b"{}\n", b"")

    with pytest.raises(tke.TkeReleaseError, match="server_dry_run:kubernetes_access_denied"):
        tke.execute_deploy(
            manifests_dir=ROOT / "deploy/kubernetes/base",
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            state_path=Path("/tmp/tke-release-state.json"),
            backend_image=BACKEND,
            frontend_image=FRONTEND,
            schema_release="zha32",
            artifact_manifest_sha256="3" * 64,
            execute=True,
            confirm_release_sha=SHA,
            runner=run,
        )

    assert sum(call[3:5] == ("apply", "--server-side") for call in calls) == 1


def test_execute_deploy_applies_reads_rollouts_and_requires_acceptance(tke, tmp_path, monkeypatch):
    calls = []
    accepted = []
    monkeypatch.setattr(
        tke,
        "accept_deploy",
        lambda **kwargs: accepted.append(kwargs) or {"status": "accepted"},
    )

    result = tke.execute_deploy(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        state_path=tmp_path / "tke-release-state.json",
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        execute=True,
        confirm_release_sha=SHA,
        runner=_runner_with_context_and_kustomize(tke, calls),
    )

    assert result["status"] == "deployed"
    command_lines = [call[0] for call in calls]
    dry_run = next(call for call in command_lines if call[3:5] == ("apply", "--server-side") and "--dry-run=server" in call)
    apply_index = next(
        index
        for index, (call, _kwargs) in enumerate(calls)
        if call[3:5] == ("apply", "--server-side") and "--dry-run=server" not in call
    )
    apply, apply_kwargs = calls[apply_index]
    assert "--force-conflicts" not in dry_run
    assert "--force-conflicts" not in apply
    assert command_lines.index(dry_run) < command_lines.index(apply)
    assert sum(call[3:5] == ("rollout", "status") for call in command_lines) == 3
    assert accepted[0]["expected_scheduler_mode"] == "active"

    rendered = apply_kwargs.get("input_bytes")
    assert isinstance(rendered, bytes)
    resource_names = {
        (item["kind"], item["metadata"]["name"])
        for item in tke.yaml.safe_load_all(rendered)
        if item
    }
    assert ("Namespace", "aiops-demo") not in resource_names
    assert ("ServiceAccount", "aiops-runtime") not in resource_names
    assert ("Role", "aiops-runtime") not in resource_names
    assert ("RoleBinding", "aiops-runtime") not in resource_names


def test_pull_rehearsal_creates_isolated_pods_verifies_digests_and_deletes(tke):
    calls = []
    pod_payloads = {
        "backend": {
            "metadata": {"name": f"aiops-pull-backend-{SHA[:12]}", "uid": "uid-backend"},
            "status": {
                "phase": "Succeeded",
                "containerStatuses": [
                    {
                        "name": "backend",
                        "image": "sha256:" + "a" * 64,
                        "imageID": BACKEND,
                        "state": {"terminated": {"exitCode": 0}},
                    }
                ],
            },
        },
        "frontend": {
            "metadata": {"name": f"aiops-pull-frontend-{SHA[:12]}", "uid": "uid-frontend"},
            "status": {
                "phase": "Succeeded",
                "containerStatuses": [
                    {
                        "name": "frontend",
                        "image": FRONTEND,
                        "imageID": "",
                        "state": {"terminated": {"exitCode": 0}},
                    }
                ],
            },
        },
    }

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:5] == ["auth", "can-i"]:
            return tke.CommandResult(0, b"yes\n", b"")
        if argv[3:6] == ["get", "namespace", "aiops-demo"]:
            return tke.CommandResult(0, b'{"status":{"phase":"Active"}}', b"")
        if argv[3:6] == ["get", "serviceaccount", "aiops-runtime"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime"},"imagePullSecrets":[{"name":"aiops-tcr-pull"}]}',
                b"",
            )
        if argv[3:6] == ["get", "secret", "aiops-tcr-pull"]:
            return tke.CommandResult(0, b"kubernetes.io/dockerconfigjson", b"")
        if argv[3:5] == ["create", "-f"]:
            return tke.CommandResult(0, b"created\n", b"")
        if argv[3] == "wait":
            return tke.CommandResult(0, b"condition met\n", b"")
        if argv[3:5] == ["get", "pod"]:
            component = "backend" if "backend" in argv[5] else "frontend"
            return tke.CommandResult(0, json.dumps(pod_payloads[component]).encode(), b"")
        if argv[3:5] == ["get", "events"]:
            selector = argv[argv.index("--field-selector") + 1]
            uid = selector.removeprefix("involvedObject.uid=")
            component = "backend" if uid == "uid-backend" else "frontend"
            image = BACKEND if component == "backend" else FRONTEND
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "items": [
                            {
                                "type": "Normal",
                                "reason": "Pulled",
                                "message": f'Successfully pulled image "{image}"',
                                "involvedObject": {"uid": uid},
                            }
                        ]
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["delete", "pod"]:
            return tke.CommandResult(0, b"deleted\n", b"")
        raise AssertionError(argv)

    evidence = tke.execute_pull_rehearsal(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        execute=True,
        confirm_release_sha=SHA,
        runner=run,
    )

    assert evidence["status"] == "cluster_pull_verified"
    assert evidence["images"] == {"backend": BACKEND, "frontend": FRONTEND}
    create_call = next(call for call in calls if call[0][3:5] == ("create", "-f"))
    documents = [item for item in tke.yaml.safe_load_all(create_call[1]["input_bytes"]) if item]
    assert {item["metadata"]["name"] for item in documents} == {
        f"aiops-pull-backend-{SHA[:12]}",
        f"aiops-pull-frontend-{SHA[:12]}",
    }
    for pod in documents:
        spec = pod["spec"]
        container = spec["containers"][0]
        assert spec["serviceAccountName"] == "aiops-runtime"
        assert spec["automountServiceAccountToken"] is False
        assert "env" not in container
        assert "envFrom" not in container
        assert container["imagePullPolicy"] == "Always"
    assert sum(call[0][3:5] == ("delete", "pod") for call in calls) == 2
    assert not any(call[0][3:5] == ("get", "events") for call in calls)
    assert not any(call[0][3:5] == ("auth", "can-i") for call in calls)
    assert not any(
        call[0][3:6] == ("get", "namespace", "aiops-demo") for call in calls
    )
    assert not any(
        call[0][3:6] == ("get", "serviceaccount", "aiops-runtime") for call in calls
    )
    assert not any(
        call[0][3:6] == ("get", "secret", "aiops-tcr-pull") for call in calls
    )


def test_mysql_rehearsal_runs_the_candidate_backend_with_only_the_database_secret_and_cleans_up(tke):
    calls = []
    pod_name = f"aiops-mysql-rehearse-{SHA[:12]}"
    pod_payload = {
        "metadata": {"name": pod_name, "uid": "uid-mysql-rehearsal"},
        "status": {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "name": "mysql-rehearsal",
                    "image": "sha256:" + "1" * 64,
                    "imageID": BACKEND,
                    "state": {"terminated": {"exitCode": 0}},
                }
            ],
        },
    }

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:5] == ["auth", "can-i"]:
            return tke.CommandResult(0, b"yes\n", b"")
        if argv[3:6] == ["get", "namespace", "aiops-demo"]:
            return tke.CommandResult(0, b'{"status":{"phase":"Active"}}', b"")
        if argv[3:5] == ["get", "secret"] and argv[5] == "aiops-runtime-secrets":
            return tke.CommandResult(
                0,
                b"aiops-runtime-secrets\nAUTO_DB_URI_PROD\n",
                b"",
            )
        if argv[3:6] == ["get", "serviceaccount", "aiops-runtime"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime"},"imagePullSecrets":[{"name":"aiops-tcr-pull"}]}',
                b"",
            )
        if argv[3:6] == ["get", "secret", "aiops-tcr-pull"]:
            return tke.CommandResult(0, b"kubernetes.io/dockerconfigjson", b"")
        if argv[3:5] == ["create", "-f"]:
            return tke.CommandResult(0, b"created\n", b"")
        if argv[3] == "wait":
            return tke.CommandResult(0, b"condition met\n", b"")
        if argv[3:5] == ["get", "pod"]:
            return tke.CommandResult(0, json.dumps(pod_payload).encode(), b"")
        if argv[3:5] == ["logs", pod_name]:
            return tke.CommandResult(
                0,
                (
                    b'{"readOnlyQuery":true,"status":"mysql_rehearsal_probe"}\n'
                ),
                b"",
            )
        if argv[3:5] == ["delete", "pod"]:
            return tke.CommandResult(0, b"deleted\n", b"")
        raise AssertionError(argv)

    evidence = tke.execute_mysql_rehearsal(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        backend_image=BACKEND,
        execute=True,
        confirm_release_sha=SHA,
        runner=run,
    )

    assert evidence == {
        "backendImage": BACKEND,
        "kubeContext": CONTEXT,
        "namespace": "aiops-demo",
        "readOnlyQuery": True,
        "releaseSha": SHA,
        "status": "mysql_rehearsal_verified",
        "targetCluster": "cls-demo0000",
    }
    create_call = next(call for call in calls if call[0][3:5] == ("create", "-f"))
    pod = tke.yaml.safe_load(create_call[1]["input_bytes"])
    assert pod["metadata"]["name"] == pod_name
    assert pod["metadata"]["labels"] == {
        "app.kubernetes.io/name": "backend",
        "app.kubernetes.io/part-of": "aiops",
        "aiops.example.test/rehearsal": "mysql",
        "aiops.example.test/release-sha": SHA,
    }
    spec = pod["spec"]
    container = spec["containers"][0]
    assert spec["serviceAccountName"] == "aiops-runtime"
    assert spec["automountServiceAccountToken"] is True
    assert spec["restartPolicy"] == "Never"
    assert container["name"] == "mysql-rehearsal"
    assert container["image"] == BACKEND
    assert container["imagePullPolicy"] == "Always"
    assert "envFrom" not in container
    assert container["resources"] == {
        "requests": {"cpu": "10m", "memory": "128Mi"},
        "limits": {"cpu": "100m", "memory": "256Mi"},
    }
    environment = {item["name"]: item for item in container["env"]}
    assert set(environment) == {
        "ENV",
        "AUTO_DB_URI_PROD",
        "AIOPS_CONFIG_PERSISTENCE",
        "AIOPS_CONFIGMAP_NAME",
        "AIOPS_CONFIGMAP_NAMESPACE",
    }
    assert environment["AUTO_DB_URI_PROD"] == {
        "name": "AUTO_DB_URI_PROD",
        "valueFrom": {
            "secretKeyRef": {
                "name": "aiops-runtime-secrets",
                "key": "AUTO_DB_URI_PROD",
                "optional": False,
            }
        },
    }
    assert "verify_release_schema" not in container["command"][2]
    assert "SELECT 1" in container["command"][2]
    assert sum(call[0][3:5] == ("delete", "pod") for call in calls) == 1
    assert not any(call[0][3:5] == ("auth", "can-i") for call in calls)
    assert not any(
        call[0][3:6] == ("get", "namespace", "aiops-demo") for call in calls
    )
    assert not any(
        call[0][3:6] == ("get", "serviceaccount", "aiops-runtime") for call in calls
    )
    assert not any(
        call[0][3:6] == ("get", "secret", "aiops-tcr-pull") for call in calls
    )


def test_mysql_rehearsal_fails_fast_with_a_safe_stage_code_and_cleans_up(tke):
    calls = []
    pod_name = f"aiops-mysql-rehearse-{SHA[:12]}"

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:5] == ["auth", "can-i"]:
            return tke.CommandResult(0, b"yes\n", b"")
        if argv[3:6] == ["get", "namespace", "aiops-demo"]:
            return tke.CommandResult(0, b'{"status":{"phase":"Active"}}', b"")
        if argv[3:5] == ["get", "secret"] and argv[5] == "aiops-runtime-secrets":
            return tke.CommandResult(
                0,
                b"aiops-runtime-secrets\nAUTO_DB_URI_PROD\nKG_AGENT_MCP_TOKEN\nPI_AGENT_INTERNAL_TOKEN\nLANGFUSE_PUBLIC_KEY\nLANGFUSE_SECRET_KEY\nSECRET_KEY\n",
                b"",
            )
        if argv[3:6] == ["get", "serviceaccount", "aiops-runtime"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime"},"imagePullSecrets":[{"name":"aiops-tcr-pull"}]}',
                b"",
            )
        if argv[3:6] == ["get", "secret", "aiops-tcr-pull"]:
            return tke.CommandResult(0, b"kubernetes.io/dockerconfigjson", b"")
        if argv[3:5] == ["create", "-f"]:
            return tke.CommandResult(0, b"created\n", b"")
        if argv[3:5] == ["get", "pod"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {"name": pod_name, "uid": "failed-mysql-rehearsal"},
                        "status": {"phase": "Failed"},
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["logs", pod_name]:
            return tke.CommandResult(
                0,
                b'{"failureCodes":["read_only_query"],"status":"mysql_rehearsal_probe_failed"}\n',
                b"",
            )
        if argv[3] == "wait":
            return tke.CommandResult(1, b"", b"pod failed")
        if argv[3:5] == ["delete", "pod"]:
            return tke.CommandResult(0, b"deleted\n", b"")
        raise AssertionError(argv)

    with pytest.raises(
        tke.TkeReleaseError,
        match="TKE MySQL rehearsal failed: read_only_query",
    ):
        tke.execute_mysql_rehearsal(
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            backend_image=BACKEND,
            execute=True,
            confirm_release_sha=SHA,
            runner=run,
        )

    assert not any(call[0][3] == "wait" for call in calls)
    assert sum(call[0][3:5] == ("delete", "pod") for call in calls) == 1


def test_rehearsal_pods_are_pinned_to_shared_runtime_nodes(tke):
    pull_pod = tke._pull_rehearsal_pod(
        component="backend",
        image=BACKEND,
        namespace="aiops-demo",
        release_sha=SHA,
    )
    mysql_pod = tke._mysql_rehearsal_pod(
        namespace="aiops-demo",
        release_sha=SHA,
        backend_image=BACKEND,
    )

    for pod in (pull_pod, mysql_pod):
        assert pod["spec"]["nodeSelector"] == {
            "aiops.example.test/runtime": "shared"
        }
        assert "tolerations" not in pod["spec"]


def test_mysql_rehearsal_script_forces_every_mysql_connection_read_only(tke):
    assert all(
        token in tke.MYSQL_REHEARSAL_SCRIPT
        for token in (
            "from sqlalchemy import event, text",
            "from src.persistence.session import engine as database_engine",
            'event.listens_for(engine, "connect")',
            'cursor.execute("SET SESSION TRANSACTION READ ONLY")',
        )
    )
    assert "src.database" not in tke.MYSQL_REHEARSAL_SCRIPT



def test_pull_rehearsal_accepts_tke_digest_only_image_id(tke):
    payload = {
        "metadata": {"name": f"aiops-pull-backend-{SHA[:12]}", "uid": "uid-backend"},
        "status": {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "name": "backend",
                    "image": BACKEND,
                    "imageID": "docker-pullable://sha256:" + "1" * 64,
                    "state": {"terminated": {"exitCode": 0}},
                }
            ],
        },
    }

    tke._validate_pull_rehearsal_pod(
        payload=payload,
        component="backend",
        image=BACKEND,
        expected_name=f"aiops-pull-backend-{SHA[:12]}",
    )


def test_workload_pod_identity_accepts_tke_runtime_image_status_forms(tke):
    backend_pod = {
        "status": {
            "containerStatuses": [
                {
                    "name": "backend",
                    "ready": True,
                    "image": "sha256:" + "a" * 64,
                    "imageID": BACKEND,
                }
            ]
        }
    }
    frontend_pod = {
        "status": {
            "containerStatuses": [
                {
                    "name": "frontend",
                    "ready": True,
                    "image": FRONTEND,
                    "imageID": "",
                }
            ]
        }
    }

    tke._pod_container_identity(
        pod=backend_pod,
        container_name="backend",
        expected_image=BACKEND,
    )
    tke._pod_container_identity(
        pod=frontend_pod,
        container_name="frontend",
        expected_image=FRONTEND,
    )


def test_pull_rehearsal_cleans_up_when_digest_readback_fails(tke):
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:5] == ["auth", "can-i"]:
            return tke.CommandResult(0, b"yes\n", b"")
        if argv[3:6] == ["get", "namespace", "aiops-demo"]:
            return tke.CommandResult(0, b'{"status":{"phase":"Active"}}', b"")
        if argv[3:6] == ["get", "serviceaccount", "aiops-runtime"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime"},"imagePullSecrets":[{"name":"aiops-tcr-pull"}]}',
                b"",
            )
        if argv[3:6] == ["get", "secret", "aiops-tcr-pull"]:
            return tke.CommandResult(0, b"kubernetes.io/dockerconfigjson", b"")
        if argv[3:5] == ["create", "-f"] or argv[3] == "wait":
            return tke.CommandResult(0, b"ok\n", b"")
        if argv[3:5] == ["get", "pod"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {"name": argv[5], "uid": "uid"},
                        "status": {
                            "phase": "Succeeded",
                            "containerStatuses": [
                                {
                                    "name": "backend",
                                    "image": BACKEND,
                                    "imageID": "docker-pullable://example.invalid/other@sha256:" + "9" * 64,
                                    "state": {"terminated": {"exitCode": 0}},
                                }
                            ],
                        },
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["delete", "pod"]:
            return tke.CommandResult(0, b"deleted\n", b"")
        raise AssertionError(argv)

    with pytest.raises(tke.TkeReleaseError, match="image identity"):
        tke.execute_pull_rehearsal(
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            backend_image=BACKEND,
            frontend_image=FRONTEND,
            execute=True,
            confirm_release_sha=SHA,
            runner=run,
        )

    assert sum(call[0][3:5] == ("delete", "pod") for call in calls) == 2


def test_pull_rehearsal_attempts_cleanup_after_create_result_is_unknown(tke):
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:5] == ["auth", "can-i"]:
            return tke.CommandResult(0, b"yes\n", b"")
        if argv[3:6] == ["get", "namespace", "aiops-demo"]:
            return tke.CommandResult(0, b'{"status":{"phase":"Active"}}', b"")
        if argv[3:6] == ["get", "serviceaccount", "aiops-runtime"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime"},"imagePullSecrets":[{"name":"aiops-tcr-pull"}]}',
                b"",
            )
        if argv[3:6] == ["get", "secret", "aiops-tcr-pull"]:
            return tke.CommandResult(0, b"kubernetes.io/dockerconfigjson", b"")
        if argv[3:5] == ["create", "-f"]:
            return tke.CommandResult(1, b"", b"connection reset")
        if argv[3:5] == ["delete", "pod"]:
            return tke.CommandResult(0, b"deleted\n", b"")
        raise AssertionError(argv)

    with pytest.raises(tke.TkeReleaseError, match="pull_rehearsal_create"):
        tke.execute_pull_rehearsal(
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            backend_image=BACKEND,
            frontend_image=FRONTEND,
            execute=True,
            confirm_release_sha=SHA,
            runner=run,
        )

    assert sum(call[0][3:5] == ("delete", "pod") for call in calls) == 2


def test_real_kustomize_render_keeps_kustomize_labels_and_excludes_bootstrap(tke):
    rendered = tke.render_release_manifests(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        release_sha=SHA,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        runner=_subprocess_runner(tke),
    )

    documents = [item for item in tke.yaml.safe_load_all(rendered) if item]
    assert {item["kind"] for item in documents}.isdisjoint(
        {"Namespace", "ServiceAccount", "Role", "RoleBinding"}
    )
    backend = next(
        item for item in documents
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "backend"
    )
    assert backend["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "kustomize"
    assert (
        backend["spec"]["selector"]["matchLabels"]["app.kubernetes.io/managed-by"]
        == "kustomize"
    )


def test_execute_rollback_uses_runtime_only_render_without_conflict_takeover(tke, tmp_path, monkeypatch):
    calls = []
    state_path = tmp_path / "tke-release-state.json"
    previous = {
        "artifactManifestSha256": "4" * 64,
        "backendImage": BACKEND,
        "frontendImage": FRONTEND,
        "manifestSha256": "5" * 64,
        "releaseSha": "b" * 40,
        "schemaRelease": "zha31",
        "slot": "blue",
    }
    active = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": BACKEND,
        "frontendImage": FRONTEND,
        "manifestSha256": "6" * 64,
        "releaseSha": SHA,
        "schemaRelease": "zha32",
        "slot": "green",
    }
    tke._record_active_release(state_path=state_path, record=previous)
    tke._record_active_release(state_path=state_path, record=active)
    monkeypatch.setattr(tke, "accept_deploy", lambda **kwargs: {"status": "accepted"})

    rolled_back = tke.execute_rollback(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        state_path=state_path,
        execute=True,
        confirm_release_sha=SHA,
        runner=_runner_with_context_and_kustomize(tke, calls),
    )

    assert rolled_back["status"] == "rolled_back"
    command_lines = [call[0] for call in calls]
    dry_run = next(call for call in command_lines if call[3:5] == ("apply", "--server-side") and "--dry-run=server" in call)
    apply = next(call for call in command_lines if call[3:5] == ("apply", "--server-side") and "--dry-run=server" not in call)
    assert "--force-conflicts" not in dry_run
    assert "--force-conflicts" not in apply
    assert command_lines.index(dry_run) < command_lines.index(apply)


def test_prepare_fails_closed_when_required_tke_preflight_is_not_complete(tke):
    calls = []

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:6] == ["auth", "can-i", "get"]:
            return tke.CommandResult(0, b"yes\n", b"")
        if argv[3:6] == ["get", "secret", "aiops-runtime-secrets"]:
            return tke.CommandResult(0, b'{"data":{"SECRET_KEY":"redacted"}}', b"")
        if argv[3:6] == ["get", "configmap", "aiops-runtime-config"]:
            return tke.CommandResult(0, b'{"data":{"ENV":"prod"}}', b"")
        return tke.CommandResult(0, b"{}\n", b"")

    with pytest.raises(tke.TkeReleaseError, match="preflight"):
        tke.prepare_deploy(
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            runner=run,
        )

    assert not any(call[3:5] == ("apply", "--server-side") for call in calls)


def test_prepare_reads_only_required_runtime_materials_without_permission_probes(tke):
    calls = []

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:6] == ["get", "configmap", "aiops-runtime-config"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime-config"},"data":{"config.yaml":"safety_config: {}\\n"}}',
                b"",
            )
        if argv[3:5] == ["get", "secret"] and argv[5] == "aiops-runtime-secrets":
            return tke.CommandResult(
                0,
                b"aiops-runtime-secrets\nAUTO_DB_URI_PROD\nKG_AGENT_MCP_TOKEN\nPI_AGENT_INTERNAL_TOKEN\nLANGFUSE_PUBLIC_KEY\nLANGFUSE_SECRET_KEY\nSECRET_KEY\n",
                b"",
            )
        if argv[3:6] == ["get", "pvc", "aiops-runtime-data"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime-data"},"spec":{"accessModes":["ReadWriteMany"],"volumeName":"pvc-1"},"status":{"phase":"Bound"}}',
                b"",
            )
        raise AssertionError(argv)

    prepared = tke.prepare_deploy(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        runner=run,
    )

    assert prepared == {
        "status": "prepared",
        "releaseSha": SHA,
        "namespace": "aiops-demo",
        "kubeContext": CONTEXT,
        "targetCluster": "cls-demo0000",
        "secretKeys": ["AUTO_DB_URI_PROD", "KG_AGENT_MCP_TOKEN", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "PI_AGENT_INTERNAL_TOKEN", "SECRET_KEY"],
        "runtimeConfigMap": "aiops-runtime-config",
        "runtimePvc": "aiops-runtime-data",
    }
    assert not any(call[3:5] == ("auth", "can-i") for call in calls)
    assert not any(call[3:6] == ("get", "namespace", "aiops-demo") for call in calls)
    assert not any(
        call[3:6] == ("get", "serviceaccount", "aiops-runtime") for call in calls
    )
    assert not any(
        call[3:6] == ("get", "secret", "aiops-tcr-pull") for call in calls
    )


def test_runtime_config_schema_sync_preserves_production_values_and_aligns_fields(tke):
    template = {
        "system_config": {"interval_minutes": 5, "new_feature_enabled": False},
        "brand_configs": {
            "atlas": {
                "enabled": False,
                "top_query_check_modes": {"recall": "enabled"},
            }
        },
    }
    production = {
        "global_config": {"legacy_interval": 60},
        "system_config": {"interval_minutes": 30, "retired_setting": "remove"},
        "brand_configs": {
            "atlas": {
                "enabled": True,
                "top_query_check_modes": {
                    "recall": "disabled",
                    "expert_ranking": "enabled",
                },
                "top_query_expert_ranking_top_n": 10,
            },
            "legacy_brand": {"enabled": True},
        },
    }

    synchronized, summary = tke.synchronize_runtime_config_schema(
        template=template,
        production=production,
    )

    assert synchronized == {
        "system_config": {"interval_minutes": 30, "new_feature_enabled": False},
        "brand_configs": {
            "atlas": {
                "enabled": True,
                "top_query_check_modes": {"recall": "disabled"},
            }
        },
    }
    assert summary == {
        "addedFields": ["system_config.new_feature_enabled"],
        "preservedFieldCount": 3,
        "removedFields": [
            "brand_configs.atlas.top_query_check_modes.expert_ranking",
            "brand_configs.atlas.top_query_expert_ranking_top_n",
            "brand_configs.legacy_brand.enabled",
            "global_config.legacy_interval",
            "system_config.retired_setting",
        ],
    }


def test_runtime_config_schema_sync_rejects_type_changes_without_exposing_values(tke):
    with pytest.raises(
        tke.TkeReleaseError,
        match=r"runtime config schema type conflict at system_config.interval_minutes",
    ) as exc_info:
        tke.synchronize_runtime_config_schema(
            template={"system_config": {"interval_minutes": 5}},
            production={"system_config": {"interval_minutes": "thirty"}},
        )

    assert "thirty" not in str(exc_info.value)


def test_runtime_config_schema_sync_skips_write_when_structure_is_already_aligned(
    tke, tmp_path
):
    template_path = tmp_path / "config.yaml"
    template_path.write_text("system_config:\n  interval_minutes: 5\n", encoding="utf-8")
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:6] == ["get", "configmap", "aiops-runtime-config"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime-config"},"data":{"config.yaml":"system_config: \\n  interval_minutes: 30\\n"}}',
                b"",
            )
        raise AssertionError(argv)

    result = tke.sync_runtime_config_schema(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        template_path=template_path,
        execute=True,
        confirm_release_sha=SHA,
        runner=run,
    )

    assert result["status"] == "runtime_config_aligned"
    assert result["changed"] is False
    assert result["addedFields"] == []
    assert result["removedFields"] == []
    assert not any(call[0][3:5] == ("apply", "--server-side") for call in calls)


def test_runtime_config_schema_sync_applies_only_configmap_and_reads_back(tke, tmp_path):
    template_path = tmp_path / "config.yaml"
    template_path.write_text(
        "system_config:\n  interval_minutes: 5\n  new_feature_enabled: false\n",
        encoding="utf-8",
    )
    calls = []
    applied = False

    def configmap_payload(raw_config):
        return tke.CommandResult(
            0,
            json.dumps(
                {
                    "metadata": {"name": "aiops-runtime-config"},
                    "data": {"config.yaml": raw_config},
                }
            ).encode(),
            b"",
        )

    def run(argv, **kwargs):
        nonlocal applied
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:6] == ["get", "configmap", "aiops-runtime-config"]:
            if not applied:
                return configmap_payload(
                    "system_config:\n  interval_minutes: 30\n  retired_setting: do-not-leak-prod-value\n"
                )
            return configmap_payload(
                "system_config:\n  interval_minutes: 30\n  new_feature_enabled: false\n"
            )
        if argv[3:5] == ["apply", "--server-side"]:
            assert kwargs["input_bytes"]
            manifest = tke.yaml.safe_load(kwargs["input_bytes"])
            assert manifest["kind"] == "ConfigMap"
            assert manifest["metadata"] == {
                "name": "aiops-runtime-config",
                "namespace": "aiops-demo",
            }
            assert "keep-production-value" not in kwargs["input_bytes"].decode()
            if "--dry-run=server" not in argv:
                applied = True
            return tke.CommandResult(0, b"configmap/aiops-runtime-config configured\\n", b"")
        raise AssertionError(argv)

    result = tke.sync_runtime_config_schema(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        template_path=template_path,
        execute=True,
        confirm_release_sha=SHA,
        runner=run,
    )

    assert result == {
        "status": "runtime_config_synced",
        "releaseSha": SHA,
        "namespace": "aiops-demo",
        "kubeContext": CONTEXT,
        "targetCluster": "cls-demo0000",
        "runtimeConfigMap": "aiops-runtime-config",
        "changed": True,
        "addedFields": ["system_config.new_feature_enabled"],
        "removedFields": ["system_config.retired_setting"],
        "preservedFieldCount": 1,
        "configSchemaSha256": result["configSchemaSha256"],
    }
    assert len(result["configSchemaSha256"]) == 64
    assert "do-not-leak-prod-value" not in json.dumps(result)
    apply_calls = [call for call in calls if call[0][3:5] == ("apply", "--server-side")]
    assert len(apply_calls) == 2
    assert all("--force-conflicts" in call[0] for call in apply_calls)
    assert all("keep-production-value" not in " ".join(call[0]) for call in apply_calls)


def test_accept_requires_exact_workload_identity_readiness_and_frontend_api_proxy(tke):
    deployment = _accepted_deployments()
    pods = _accepted_pods()
    calls = []

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:6] == ["get", "deployments", "--namespace"]:
            return tke.CommandResult(0, json.dumps(deployment).encode(), b"")
        if argv[3:6] == ["get", "endpoints", argv[5]]:
            return tke.CommandResult(0, b'{"subsets":[{}]}', b"")
        if argv[3:5] == ["get", "pods"]:
            component = argv[argv.index("--selector") + 1].split("=", 1)[1]
            return tke.CommandResult(0, json.dumps(pods[component]).encode(), b"")
        raw_path = argv[-1]
        if raw_path.endswith("/api/health/live"):
            return tke.CommandResult(0, b'{"live":true}', b"")
        if raw_path.endswith("/api/health/startup"):
            return tke.CommandResult(0, b'{"ready":true}', b"")
        if raw_path.endswith("/api/health/ready"):
            return tke.CommandResult(0, b'{"ready":true,"releaseSha":"' + SHA.encode() + b'"}', b"")
        if raw_path.endswith("/release-info.json"):
            return tke.CommandResult(0, b'{"releaseSha":"' + SHA.encode() + b'"}', b"")
        if raw_path.endswith("/api/health/ready"):
            return tke.CommandResult(0, b'{"ready":true,"releaseSha":"' + SHA.encode() + b'"}', b"")
        raise AssertionError(argv)

    accepted = tke.accept_deploy(namespace="aiops-demo", release_sha=SHA, kube_context=CONTEXT, runner=run)

    assert accepted["status"] == "accepted"
    assert accepted["releaseSha"] == SHA
    assert any(call[3:5] == ("get", "deployments") for call in calls)
    assert (
        "kubectl",
        "--context",
        CONTEXT,
        "get",
        "--raw",
        "/api/v1/namespaces/aiops-demo/services/http:api:8000/proxy/api/health/ready",
    ) in calls
    assert (
        "kubectl",
        "--context",
        CONTEXT,
        "get",
        "--raw",
        "/api/v1/namespaces/aiops-demo/services/http:frontend:80/proxy/api/health/ready",
    ) in calls


def test_candidate_render_switches_scheduler_readiness_to_standby(tke):
    calls = []

    rendered = tke.render_candidate_manifests(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        release_sha=SHA,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        runner=_runner_with_kustomize_output(tke, calls),
    )

    documents = [item for item in tke.yaml.safe_load_all(rendered) if item]
    scheduler = next(
        item for item in documents
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "scheduler"
    )
    container = scheduler["spec"]["template"]["spec"]["containers"][0]
    values = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    assert values["AIOPS_SCHEDULER_MODE"] == "standby"
    assert values["SCHEDULER_EXPECTED_MODE"] == "standby"
    assert "os.environ.get('SCHEDULER_EXPECTED_MODE')" in container["readinessProbe"]["exec"]["command"][2]


def test_candidate_deploy_does_not_mark_an_unactivated_release_as_rollback_target(tke, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tke, "accept_deploy", lambda **kwargs: {"status": "accepted"})

    result = tke.execute_deploy(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        state_path=tmp_path / "tke-release-state.json",
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        execute=True,
        confirm_release_sha=SHA,
        candidate=True,
        runner=_runner_with_context_and_kustomize(tke, calls),
    )

    assert result["status"] == "candidate_deployed"
    assert tke._read_release_state(tmp_path / "tke-release-state.json")["active"] is None


def test_deploy_records_active_release_only_after_matching_acceptance(tke, tmp_path, monkeypatch):
    state_path = tmp_path / "tke-release-state.json"
    accepted = []
    monkeypatch.setattr(
        tke,
        "accept_deploy",
        lambda **kwargs: accepted.append(kwargs) or {"status": "accepted", "schedulerMode": "active"},
    )

    result = tke.execute_deploy(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        state_path=state_path,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        execute=True,
        confirm_release_sha=SHA,
        runner=_runner_with_context_and_kustomize(tke, []),
    )

    assert result["status"] == "deployed"
    assert accepted[0]["expected_scheduler_mode"] == "active"
    assert tke._read_release_state(state_path)["active"]["releaseSha"] == SHA


def test_deploy_fails_without_recording_active_when_acceptance_fails(tke, tmp_path, monkeypatch):
    state_path = tmp_path / "tke-release-state.json"
    monkeypatch.setattr(
        tke,
        "accept_deploy",
        lambda **kwargs: (_ for _ in ()).throw(tke.TkeReleaseError("acceptance failed")),
    )

    with pytest.raises(tke.TkeReleaseError, match="acceptance failed"):
        tke.execute_deploy(
            manifests_dir=ROOT / "deploy/kubernetes/base",
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            state_path=state_path,
            backend_image=BACKEND,
            frontend_image=FRONTEND,
            schema_release="zha32",
            artifact_manifest_sha256="3" * 64,
            execute=True,
            confirm_release_sha=SHA,
            runner=_runner_with_context_and_kustomize(tke, []),
        )

    assert tke._read_release_state(state_path)["active"] is None


def test_accept_rejects_scheduler_mode_that_does_not_match_requested_phase(tke):
    deployment = _accepted_deployments(scheduler_mode="standby")
    pods = _accepted_pods(scheduler_mode="standby")

    def run(argv, **kwargs):
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:6] == ["get", "deployments", "--namespace"]:
            return tke.CommandResult(0, json.dumps(deployment).encode(), b"")
        if argv[3:6] == ["get", "endpoints", argv[5]]:
            return tke.CommandResult(0, b'{"subsets":[{}]}', b"")
        if argv[3:5] == ["get", "pods"]:
            component = argv[argv.index("--selector") + 1].split("=", 1)[1]
            return tke.CommandResult(0, json.dumps(pods[component]).encode(), b"")
        raw_path = argv[-1]
        if raw_path.endswith("/api/health/live"):
            return tke.CommandResult(0, b'{"live":true}', b"")
        if raw_path.endswith("/api/health/startup"):
            return tke.CommandResult(0, b'{"ready":true}', b"")
        if raw_path.endswith("/api/health/ready"):
            return tke.CommandResult(0, b'{"ready":true,"releaseSha":"' + SHA.encode() + b'"}', b"")
        if raw_path.endswith("/release-info.json"):
            return tke.CommandResult(0, b'{"releaseSha":"' + SHA.encode() + b'"}', b"")
        raise AssertionError(argv)

    with pytest.raises(tke.TkeReleaseError, match="Scheduler mode"):
        tke.accept_deploy(
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            expected_scheduler_mode="active",
            runner=run,
        )


def test_rollback_requires_a_complete_accept_readback_before_recording_active_release(tke, tmp_path, monkeypatch):
    state_path = tmp_path / "tke-release-state.json"
    previous = {
        "artifactManifestSha256": "4" * 64,
        "backendImage": BACKEND,
        "frontendImage": FRONTEND,
        "manifestSha256": "5" * 64,
        "releaseSha": "b" * 40,
        "schemaRelease": "zha31",
        "slot": "blue",
    }
    active = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": BACKEND,
        "frontendImage": FRONTEND,
        "manifestSha256": "6" * 64,
        "releaseSha": SHA,
        "schemaRelease": "zha32",
        "slot": "green",
    }
    tke._record_active_release(state_path=state_path, record=previous)
    tke._record_active_release(state_path=state_path, record=active)
    calls = []
    monkeypatch.setattr(
        tke,
        "accept_deploy",
        lambda **kwargs: calls.append(kwargs) or {"status": "accepted"},
    )

    restored = tke.execute_rollback(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        state_path=state_path,
        execute=True,
        confirm_release_sha=SHA,
        runner=_runner_with_context_and_kustomize(tke, []),
    )

    assert restored["status"] == "rolled_back"
    assert len(calls) == 1
    assert calls[0]["namespace"] == "aiops-demo"
    assert calls[0]["release_sha"] == "b" * 40
    assert calls[0]["kube_context"] == CONTEXT
    assert calls[0]["expected_scheduler_mode"] == "active"


def test_rollback_requires_previous_immutable_images(tke, tmp_path):
    with pytest.raises(tke.TkeReleaseError, match="recorded"):
        tke.execute_rollback(
            manifests_dir=ROOT / "deploy/kubernetes/base",
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            state_path=tmp_path / "tke-release-state.json",
            execute=True,
            confirm_release_sha=SHA,
            runner=lambda *args, **kwargs: pytest.fail("must fail before command"),
        )


def test_release_cli_exposes_tke_lifecycle_without_ssh_arguments():
    spec = importlib.util.spec_from_file_location(
        "test_tke_release_entry", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)

    parser = release.build_parser()
    activate = parser.parse_args(
        [
            "tke-activate",
            "--release-sha", SHA,
            "--kube-context", CONTEXT,
        ]
    )
    rollback = parser.parse_args(
        [
            "tke-rollback",
            "--release-sha", SHA,
            "--kube-context", CONTEXT,
        ]
    )
    adopt_live = parser.parse_args(
        [
            "tke-adopt-live",
            "--target-state-dir", "/tmp/aiops-release",
            "--release-sha", SHA,
            "--kube-context", CONTEXT,
        ]
    )

    assert activate.action == "tke-activate"
    assert not hasattr(activate, "host")
    assert not hasattr(activate, "backend_image")
    assert rollback.action == "tke-rollback"
    assert adopt_live.action == "tke-adopt-live"
    assert adopt_live.target_state_dir == Path("/tmp/aiops-release")


def test_release_cli_does_not_expose_schema_migration_or_diagnostic_commands():
    spec = importlib.util.spec_from_file_location(
        "test_tke_schema_migration_entry", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)

    parser = release.build_parser()
    commands = (
        [
            "tke-migrate-schema",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
            "--baseline-release-sha",
            "b" * 40,
        ],
        ["tke-diagnose-schema", "--release-sha", SHA, "--kube-context", CONTEXT],
    )
    for command in commands:
        with pytest.raises(SystemExit):
            parser.parse_args(command)


def test_manifest_contract_keeps_customer_api_path_on_frontend_proxy_and_tmp_writable(tke):
    calls = []
    rendered = tke.render_release_manifests(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        release_sha=SHA,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        runner=_runner_with_kustomize_output(tke, calls),
    )
    documents = list(tke.yaml.safe_load_all(rendered))
    ingress = next(
        item for item in documents
        if item.get("kind") == "Ingress" and item["metadata"]["name"] == "aiops"
    )
    api_path = next(
        item for item in ingress["spec"]["rules"][0]["http"]["paths"]
        if item["path"] == "/api"
    )
    assert api_path["backend"]["service"]["name"] == "frontend"
    for name in ("backend", "frontend", "scheduler"):
        deployment = next(
            item for item in documents
            if item.get("kind") == "Deployment" and item["metadata"]["name"] == name
        )
        mounts = deployment["spec"]["template"]["spec"]["containers"][0].get("volumeMounts", [])
        assert {mount["mountPath"] for mount in mounts} >= {"/tmp"}


def test_tke_lifecycle_cli_requires_an_explicit_kube_context():
    spec = importlib.util.spec_from_file_location(
        "test_tke_context_release_entry", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)

    parser = release.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["tke-prepare", "--release-sha", SHA])

    prepare = parser.parse_args(
        [
            "tke-prepare",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
        ]
    )
    assert prepare.kube_context == CONTEXT


def test_prepare_binds_the_explicit_context_to_the_target_cluster(tke):
    calls = []

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-demo0000"}],
                        "contexts": [
                            {
                                "name": CONTEXT,
                                "context": {"cluster": "cls-demo0000"},
                            }
                        ],
                        "current-context": CONTEXT,
                    }
                ).encode(),
                b"",
            )
        if argv[3:6] == ["auth", "can-i", "get"]:
            return tke.CommandResult(0, b"yes\n", b"")
        if argv[3:5] == ["auth", "can-i"]:
            return tke.CommandResult(0, b"yes\n", b"")
        if argv[3:6] == ["get", "namespace", "aiops-demo"]:
            return tke.CommandResult(0, b'{"status":{"phase":"Active"}}', b"")
        if argv[3:5] == ["get", "secret"] and argv[5] == "aiops-runtime-secrets":
            return tke.CommandResult(
                0,
                b"aiops-runtime-secrets\nAUTO_DB_URI_PROD\nKG_AGENT_MCP_TOKEN\nPI_AGENT_INTERNAL_TOKEN\nLANGFUSE_PUBLIC_KEY\nLANGFUSE_SECRET_KEY\nSECRET_KEY\n",
                b"",
            )
        if argv[3:6] == ["get", "configmap", "aiops-runtime-config"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime-config"},"data":{"config.yaml":"safety_config: {}\\n"}}',
                b"",
            )
        if argv[3:6] == ["get", "pvc", "aiops-runtime-data"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime-data"},"spec":{"accessModes":["ReadWriteMany"],"volumeName":"pvc-1"},"status":{"phase":"Bound"}}',
                b"",
            )
        if argv[3:6] == ["get", "serviceaccount", "aiops-runtime"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime"},"imagePullSecrets":[{"name":"aiops-tcr-pull"}]}',
                b"",
            )
        if argv[3:6] == ["get", "secret", "aiops-tcr-pull"]:
            return tke.CommandResult(0, b"kubernetes.io/dockerconfigjson", b"")
        return tke.CommandResult(0, b"{}\n", b"")

    prepared = tke.prepare_deploy(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        runner=run,
    )

    assert prepared["targetCluster"] == "cls-demo0000"
    assert all(call[:3] == ("kubectl", "--context", CONTEXT) for call in calls)


def test_prepare_rejects_a_context_for_another_cluster_before_runtime_reads(tke):
    calls = []

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-unrelated"}],
                        "contexts": [
                            {
                                "name": CONTEXT,
                                "context": {"cluster": "cls-unrelated"},
                            }
                        ],
                        "current-context": CONTEXT,
                    }
                ).encode(),
                b"",
            )
        return tke.CommandResult(0, b"yes\n", b"")

    with pytest.raises(tke.TkeReleaseError, match="target cluster"):
        tke.prepare_deploy(
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            runner=run,
        )

    assert len(calls) == 1
    assert calls[0][:6] == (
        "kubectl",
        "--context",
        CONTEXT,
        "config",
        "view",
        "--minify",
    )


def test_tke_rollback_requires_a_persisted_release_target(tke, tmp_path):
    with pytest.raises(tke.TkeReleaseError, match="recorded"):
        tke.execute_rollback(
            manifests_dir=ROOT / "deploy/kubernetes/base",
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            state_path=tmp_path / "tke-release-state.json",
            execute=True,
            confirm_release_sha=SHA,
            runner=lambda *args, **kwargs: pytest.fail("must fail before command"),
        )


def test_cfs_runtime_init_job_is_limited_to_runtime_directory_and_non_root_probe(tke):
    job = tke._cfs_runtime_init_job(
        namespace="aiops-demo",
        release_sha=SHA,
        backend_image=BACKEND,
    )

    assert job["apiVersion"] == "batch/v1"
    assert job["kind"] == "Job"
    assert job["metadata"] == {
        "name": "aiops-cfs-runtime-init",
        "namespace": "aiops-demo",
        "labels": {
            "app.kubernetes.io/name": "cfs-runtime-init",
            "app.kubernetes.io/part-of": "aiops",
            "aiops.example.test/release-sha": SHA,
        },
    }
    spec = job["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] == tke.CFS_RUNTIME_INIT_TIMEOUT_SECONDS
    assert spec["ttlSecondsAfterFinished"] == 60

    pod_spec = spec["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["serviceAccountName"] == "aiops-runtime"
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["nodeSelector"] == {"aiops.example.test/runtime": "shared"}
    assert "tolerations" not in pod_spec
    assert pod_spec["securityContext"] == {
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert pod_spec["volumes"] == [
        {
            "name": "runtime-data",
            "persistentVolumeClaim": {
                "claimName": "aiops-runtime-data",
                "readOnly": False,
            },
        }
    ]
    assert "imagePullSecrets" not in pod_spec
    assert "ephemeralContainers" not in pod_spec

    init = pod_spec["initContainers"]
    assert len(init) == 1
    assert init[0] == {
        "name": "runtime-data-init",
        "image": BACKEND,
        "imagePullPolicy": "Always",
        "command": ["python", "-c", tke.CFS_RUNTIME_INIT_SCRIPT],
        "resources": {
            "requests": {"cpu": "25m", "memory": "64Mi"},
            "limits": {"cpu": "100m", "memory": "128Mi"},
        },
        "securityContext": {
            "runAsNonRoot": False,
            "runAsUser": 0,
            "runAsGroup": 0,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"], "add": ["CHOWN"]},
        },
        "volumeMounts": [{"name": "runtime-data", "mountPath": "/app/data"}],
    }

    verify = pod_spec["containers"]
    assert len(verify) == 1
    assert verify[0] == {
        "name": "runtime-data-verify",
        "image": BACKEND,
        "imagePullPolicy": "Always",
        "command": ["python", "-c", tke.CFS_RUNTIME_VERIFY_SCRIPT],
        "resources": {
            "requests": {"cpu": "25m", "memory": "64Mi"},
            "limits": {"cpu": "100m", "memory": "128Mi"},
        },
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [{"name": "runtime-data", "mountPath": "/app/data"}],
    }
    assert "/app/data/runtime" in tke.CFS_RUNTIME_INIT_SCRIPT
    assert "os.chown" in tke.CFS_RUNTIME_INIT_SCRIPT
    assert "os.open" in tke.CFS_RUNTIME_VERIFY_SCRIPT
    assert "os.unlink" in tke.CFS_RUNTIME_VERIFY_SCRIPT
    compile(tke.CFS_RUNTIME_INIT_SCRIPT, "<cfs-runtime-init>", "exec")
    compile(tke.CFS_RUNTIME_VERIFY_SCRIPT, "<cfs-runtime-verify>", "exec")


def test_cfs_runtime_init_executes_one_job_checks_non_root_write_and_deletes_it(tke):
    calls = []
    name = "aiops-cfs-runtime-init"
    job = tke._cfs_runtime_init_job(
        namespace="aiops-demo", release_sha=SHA, backend_image=BACKEND
    )
    readback_job = {
        "status": {"conditions": [{"type": "Complete", "status": "True"}]}
    }

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:6] == ["get", "pvc", "aiops-runtime-data"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime-data"},"spec":{"accessModes":["ReadWriteMany"],"volumeName":"pvc-1"},"status":{"phase":"Bound"}}',
                b"",
            )
        if argv[3:6] == ["get", "job", name]:
            return tke.CommandResult(0, json.dumps(readback_job).encode(), b"")
        if argv[3:7] == ["create", "--dry-run=server", "-f", "-"]:
            return tke.CommandResult(0, b"created (server dry run)\n", b"")
        if argv[3:5] == ["create", "-f"]:
            return tke.CommandResult(0, b"created\n", b"")
        if argv[3:5] == ["logs", f"job/{name}"]:
            assert tuple(argv[-2:]) == ("--container", "runtime-data-verify")
            return tke.CommandResult(
                0,
                b'{"gid":10001,"mode":"0770","status":"runtime_data_non_root_write_verified","uid":10001}\n',
                b"",
            )
        if argv[3:5] == ["delete", "job"]:
            assert argv[5] == name
            return tke.CommandResult(0, b"deleted\n", b"")
        raise AssertionError(argv)

    evidence = tke.execute_cfs_runtime_init(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        backend_image=BACKEND,
        execute=True,
        confirm_release_sha=SHA,
        runner=run,
    )

    assert evidence == {
        "backendImage": BACKEND,
        "jobName": name,
        "kubeContext": CONTEXT,
        "namespace": "aiops-demo",
        "nonRootWriteVerified": True,
        "releaseSha": SHA,
        "runtimeDataGid": 10001,
        "runtimeDataMode": "0770",
        "runtimeDataPath": "/app/data/runtime",
        "runtimeDataUid": 10001,
        "status": "cfs_runtime_init_verified",
        "targetCluster": "cls-demo0000",
    }
    dry_run_call = next(
        call
        for call in calls
        if call[0][3:7] == ("create", "--dry-run=server", "-f", "-")
    )
    assert tke.yaml.safe_load(dry_run_call[1]["input_bytes"])["kind"] == "Job"
    create_call = next(call for call in calls if call[0][3:5] == ("create", "-f"))
    assert tke.yaml.safe_load(create_call[1]["input_bytes"])["spec"] == job["spec"]
    assert any(call[0][3:5] == ("delete", "job") for call in calls)
    assert not any(call[0][3:5] == ("auth", "can-i") for call in calls)
    assert not any(
        call[0][3:6] == ("get", "serviceaccount", "aiops-runtime")
        for call in calls
    )
    assert not any(
        call[0][3:6] == ("get", "secret", "aiops-tcr-pull") for call in calls
    )
    assert not any(
        call[0][3:6] == ("get", "namespace", "aiops-demo") for call in calls
    )
    assert sum(call[0][3:6] == ("get", "job", name) for call in calls) == 1


def test_cfs_runtime_init_fails_closed_for_root_squash_and_still_deletes_its_job(tke):
    calls = []
    name = "aiops-cfs-runtime-init"
    job = {"status": {"conditions": [{"type": "Failed", "status": "True"}]}}

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return _context_metadata(tke)
        if argv[3:6] == ["get", "pvc", "aiops-runtime-data"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime-data"},"spec":{"accessModes":["ReadWriteMany"],"volumeName":"pvc-1"},"status":{"phase":"Bound"}}',
                b"",
            )
        if argv[3:6] == ["get", "job", name]:
            return tke.CommandResult(0, json.dumps(job).encode(), b"")
        if argv[3:7] == ["create", "--dry-run=server", "-f", "-"]:
            return tke.CommandResult(0, b"created (server dry run)\n", b"")
        if argv[3:5] == ["create", "-f"]:
            return tke.CommandResult(0, b"created\n", b"")
        if argv[3:5] == ["logs", f"job/{name}"]:
            assert tuple(argv[-2:]) == ("--container", "runtime-data-init")
            return tke.CommandResult(
                0,
                b'{"failureCode":"ownership","status":"runtime_data_init_failed"}\n',
                b"",
            )
        if argv[3:5] == ["delete", "job"]:
            return tke.CommandResult(0, b"deleted\n", b"")
        raise AssertionError(argv)

    with pytest.raises(
        tke.TkeReleaseError, match="TKE CFS runtime initialization failed: ownership"
    ):
        tke.execute_cfs_runtime_init(
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            backend_image=BACKEND,
            execute=True,
            confirm_release_sha=SHA,
            runner=run,
        )

    assert any(call[0][3:5] == ("delete", "job") for call in calls)


def test_release_cli_exposes_and_routes_cfs_runtime_init_using_candidate_pull_proof(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "test_tke_cfs_runtime_init_entry", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)

    parser = release.build_parser()
    parsed = parser.parse_args(
        [
            "tke-init-runtime-data",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    )
    assert parsed.action == "tke-init-runtime-data"
    assert not hasattr(parsed, "host")

    calls = []
    record = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": BACKEND,
        "clusterPullVerified": False,
        "frontendImage": FRONTEND,
        "releaseSha": SHA,
        "schemaRelease": "zha32",
        "sourceTreeSha": "b" * 40,
    }
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_independent_worktree",
        lambda self: calls.append(("independent", {})),
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_clean_commit",
        lambda self: calls.append(("clean", {})) or "c" * 40,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append(("candidate", kwargs)) or record,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_cluster_pull_rehearsal",
        lambda **kwargs: calls.append(("pull_proof", kwargs)),
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_cfs_runtime_init",
        lambda **kwargs: calls.append(("runtime_init", kwargs))
        or {"status": "cfs_runtime_init_verified"},
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--format",
            "json",
            "tke-init-runtime-data",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 0
    assert [name for name, _kwargs in calls] == [
        "independent",
        "clean",
        "candidate",
        "pull_proof",
        "runtime_init",
    ]
    runtime_init = calls[-1][1]
    assert runtime_init["backend_image"] == BACKEND
    assert runtime_init["confirm_release_sha"] == SHA
    assert runtime_init["execute"] is True


def test_release_cli_returns_masked_blocked_json_for_tke_release_error(
    tke, tmp_path, monkeypatch, capsys
):
    spec = importlib.util.spec_from_file_location(
        "test_tke_error_release_entry", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)
    monkeypatch.setattr(
        release.tke_release,
        "prepare_deploy",
        lambda **kwargs: (_ for _ in ()).throw(
            release.tke_release.TkeReleaseError(
                "TKE preflight runtime ConfigMap is missing or invalid"
            )
        ),
    )

    assert release.main(
        [
            "--format",
            "json",
            "--state-dir",
            str(tmp_path / "state"),
            "tke-prepare",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
        ]
    ) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "error": "TKE preflight runtime ConfigMap is missing or invalid",
        "ok": False,
        "status": "blocked",
    }
