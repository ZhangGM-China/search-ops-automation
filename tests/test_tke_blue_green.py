import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "release" / "tke_release.py"

SHA = "a" * 40
BACKEND = "registry.example.test/aiops/backend@sha256:" + "1" * 64
FRONTEND = "registry.example.test/aiops/frontend@sha256:" + "2" * 64
CONTEXT = "cls-demo0000-000000000000-context-default"


def _load():
    spec = importlib.util.spec_from_file_location("test_tke_blue_green", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _runner(tke):
    def run(argv, **kwargs):
        if argv[:2] == ["kubectl", "kustomize"]:
            completed = subprocess.run(argv, capture_output=True, check=False, timeout=30)
            return tke.CommandResult(completed.returncode, completed.stdout, completed.stderr)
        raise AssertionError(argv)

    return run


def _documents(tke, rendered):
    return [item for item in tke.yaml.safe_load_all(rendered) if item is not None]


def _container(deployment, name):
    return next(
        item
        for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == name
    )


def test_candidate_slot_render_isolated_from_active_entrypoints():
    tke = _load()

    rendered = tke.render_slot_manifests(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        release_sha=SHA,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        slot="green",
        scheduler_mode="standby",
        include_public_entrypoints=False,
        runner=_runner(tke),
    )

    documents = _documents(tke, rendered)
    deployments = {
        item["metadata"]["name"]: item
        for item in documents
        if item["kind"] == "Deployment"
    }
    assert set(deployments) == {"backend-green", "frontend-green", "scheduler-green"}
    for component, deployment in deployments.items():
        assert deployment["spec"]["selector"]["matchLabels"]["aiops.example.test/slot"] == "green"
        assert deployment["spec"]["template"]["metadata"]["labels"]["aiops.example.test/slot"] == "green"
        assert deployment["metadata"]["annotations"]["aiops.example.test/release-sha"] == SHA

    services = {
        item["metadata"]["name"]: item
        for item in documents
        if item["kind"] == "Service"
    }
    assert set(services) == {"api-green", "frontend-green"}
    assert services["api-green"]["spec"]["selector"]["app.kubernetes.io/name"] == "backend"
    assert services["api-green"]["spec"]["selector"]["aiops.example.test/slot"] == "green"
    assert services["frontend-green"]["spec"]["selector"]["app.kubernetes.io/name"] == "frontend"
    assert services["frontend-green"]["spec"]["selector"]["aiops.example.test/slot"] == "green"
    assert not any(item["kind"] == "Ingress" for item in documents)

    release_info = next(
        item
        for item in documents
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "aiops-release-info-green"
    )
    assert release_info["data"]["RELEASE_SHA"] == SHA

    runtime_role = next(item for item in documents if item["kind"] == "Role")
    release_config_rule = next(
        item
        for item in runtime_role["rules"]
        if item.get("resourceNames") == ["aiops-release-info-blue", "aiops-release-info-green"]
    )
    assert release_config_rule == {
        "apiGroups": [""],
        "resources": ["configmaps"],
        "resourceNames": ["aiops-release-info-blue", "aiops-release-info-green"],
        "verbs": ["get"],
    }

    for component in ("backend", "scheduler"):
        container = _container(deployments[f"{component}-green"], component)
        assert [item["configMapRef"]["name"] for item in container["envFrom"] if "configMapRef" in item] == [
            "aiops-release-info-green"
        ]
    frontend = _container(deployments["frontend-green"], "frontend")
    environment = {item["name"]: item.get("value") for item in frontend["env"]}
    assert environment["API_UPSTREAM"] == "api-green"
    assert {
        item["valueFrom"]["configMapKeyRef"]["name"]
        for item in frontend["env"]
        if "valueFrom" in item and "configMapKeyRef" in item["valueFrom"]
    } == {"aiops-release-info-green"}


def test_active_slot_render_exposes_only_stable_public_services_and_ingress():
    tke = _load()

    rendered = tke.render_slot_manifests(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        release_sha=SHA,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        slot="blue",
        scheduler_mode="active",
        include_public_entrypoints=True,
        runner=_runner(tke),
    )

    documents = _documents(tke, rendered)
    services = {
        item["metadata"]["name"]: item
        for item in documents
        if item["kind"] == "Service"
    }
    assert set(services) == {"api-blue", "frontend-blue", "api", "frontend"}
    assert services["api"]["spec"]["selector"] == services["api-blue"]["spec"]["selector"]
    assert services["frontend"]["spec"]["selector"] == services["frontend-blue"]["spec"]["selector"]
    ingress = next(item for item in documents if item["kind"] == "Ingress")
    assert {
        item["backend"]["service"]["name"]
        for item in ingress["spec"]["rules"][0]["http"]["paths"]
    } == {"frontend"}


def test_blue_green_state_selects_the_inactive_slot_and_records_the_active_slot(tmp_path):
    tke = _load()
    state_path = tmp_path / "tke-release-state.json"
    record = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": BACKEND,
        "frontendImage": FRONTEND,
        "manifestSha256": "4" * 64,
        "releaseSha": SHA,
        "schemaRelease": "zha32",
        "slot": "green",
    }

    assert tke.candidate_slot(state_path=state_path) == "green"
    tke._record_active_release(state_path=state_path, record=record)
    assert tke.candidate_slot(state_path=state_path) == "blue"
    assert tke._read_release_state(state_path)["active"]["slot"] == "green"


def test_candidate_slot_refuses_to_assume_green_when_live_public_services_exist(tmp_path, monkeypatch):
    tke = _load()
    state_path = tmp_path / "tke-release-state.json"
    monkeypatch.setattr(tke, "_verify_target_context", lambda **_kwargs: None)

    def read_resource(*, resource, name, **_kwargs):
        if resource == "service" and name == "api":
            return {"metadata": {"name": "api"}, "spec": {"selector": {tke.SLOT_LABEL: "green"}}}
        return None

    monkeypatch.setattr(tke, "_read_optional_named_resource", read_resource)

    with pytest.raises(tke.TkeReleaseError, match="active release state is missing"):
        tke.require_candidate_slot_safe(
            state_path=state_path,
            namespace="aiops-demo",
            kube_context=CONTEXT,
        )


def test_candidate_slot_requires_recorded_active_slot_to_match_live_services(tmp_path, monkeypatch):
    tke = _load()
    state_path = tmp_path / "tke-release-state.json"
    record = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": BACKEND,
        "frontendImage": FRONTEND,
        "manifestSha256": "4" * 64,
        "releaseSha": SHA,
        "schemaRelease": "zha32",
        "slot": "green",
    }
    tke._record_active_release(state_path=state_path, record=record)
    monkeypatch.setattr(tke, "_verify_target_context", lambda **_kwargs: None)
    def read_resource(*, resource, name, **_kwargs):
        if resource == "service" and name in {"api", "frontend"}:
            return {
                "metadata": {"name": name},
                "spec": {"selector": {
                    tke.SLOT_LABEL: "blue",
                    "app.kubernetes.io/name": "backend" if name == "api" else "frontend",
                }},
            }
        if resource == "ingress":
            return {"metadata": {"name": "aiops"}, "spec": {"rules": [{"http": {"paths": [{"backend": {"service": {"name": "frontend"}}}]}}]}}
        return None
    monkeypatch.setattr(tke, "_read_optional_named_resource", read_resource)

    with pytest.raises(tke.TkeReleaseError, match="recorded active slot does not match"):
        tke.require_candidate_slot_safe(
            state_path=state_path,
            namespace="aiops-demo",
            kube_context=CONTEXT,
        )


def test_candidate_slot_selects_green_only_when_cluster_has_no_public_entrypoints(tmp_path, monkeypatch):
    tke = _load()
    monkeypatch.setattr(tke, "_verify_target_context", lambda **_kwargs: None)
    monkeypatch.setattr(tke, "_read_optional_named_resource", lambda **_kwargs: None)
    assert tke.require_candidate_slot_safe(
        state_path=tmp_path / tke.TKE_STATE_FILENAME,
        namespace="aiops-demo",
        kube_context=CONTEXT,
    ) == "green"


def test_import_active_release_state_requires_live_entrypoints_and_acceptance(tmp_path, monkeypatch):
    tke = _load()
    source_state_path = tmp_path / "source" / tke.TKE_STATE_FILENAME
    target_state_path = tmp_path / "target" / tke.TKE_STATE_FILENAME
    active = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": BACKEND,
        "frontendImage": FRONTEND,
        "manifestSha256": "4" * 64,
        "releaseSha": SHA,
        "schemaRelease": "zha32",
        "slot": "blue",
    }
    tke._record_active_release(state_path=source_state_path, record=active)
    monkeypatch.setattr(tke, "_verify_target_context", lambda **_kwargs: None)

    def read_resource(*, resource, name, **_kwargs):
        if resource == "service" and name in {"api", "frontend"}:
            return {
                "metadata": {"name": name},
                "spec": {"selector": {
                    tke.SLOT_LABEL: "blue",
                    "app.kubernetes.io/name": "backend" if name == "api" else "frontend",
                }},
            }
        if resource == "ingress" and name == "aiops":
            return {
                "metadata": {"name": "aiops"},
                "spec": {"rules": [{"http": {"paths": [
                    {"backend": {"service": {"name": "frontend"}}}
                ]}}]},
            }
        return None

    monkeypatch.setattr(tke, "_read_optional_named_resource", read_resource)
    monkeypatch.setattr(tke, "accept_slot_deploy", lambda **kwargs: {
        "status": "accepted",
        "images": {"backend": BACKEND, "frontend": FRONTEND},
        "releaseSha": kwargs["release_sha"],
        "slot": kwargs["slot"],
    })

    result = tke.import_active_release_state(
        source_state_path=source_state_path,
        target_state_path=target_state_path,
        expected_active_release_sha=SHA,
        execute=True,
        confirm_release_sha=SHA,
        namespace="aiops-demo",
        kube_context=CONTEXT,
    )

    assert result["status"] == "active_release_state_imported"
    assert result["activeReleaseSha"] == SHA
    assert tke._read_release_state(target_state_path)["active"] == active


def test_import_active_release_state_refuses_wrong_images(tmp_path, monkeypatch):
    tke = _load()
    source_state_path = tmp_path / "source" / tke.TKE_STATE_FILENAME
    target_state_path = tmp_path / "target" / tke.TKE_STATE_FILENAME
    active = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": BACKEND,
        "frontendImage": FRONTEND,
        "manifestSha256": "4" * 64,
        "releaseSha": SHA,
        "schemaRelease": "zha32",
        "slot": "blue",
    }
    tke._record_active_release(state_path=source_state_path, record=active)
    monkeypatch.setattr(tke, "_verify_target_context", lambda **_kwargs: None)
    monkeypatch.setattr(tke, "_read_optional_named_resource", lambda *, resource, name, **_kwargs: {
        "metadata": {"name": name}, "spec": {"selector": {tke.SLOT_LABEL: "blue"}}
    } if resource == "service" else {
        "metadata": {"name": "aiops"},
        "spec": {"rules": [{"http": {"paths": [{"backend": {"service": {"name": "frontend"}}}]}}]},
    })
    monkeypatch.setattr(tke, "accept_slot_deploy", lambda **_kwargs: {
        "status": "accepted", "images": {"backend": FRONTEND, "frontend": FRONTEND},
        "releaseSha": SHA, "slot": "blue",
    })

    with pytest.raises(tke.TkeReleaseError, match="image identity mismatched"):
        tke.import_active_release_state(
            source_state_path=source_state_path,
            target_state_path=target_state_path,
            expected_active_release_sha=SHA,
            execute=True,
            confirm_release_sha=SHA,
            namespace="aiops-demo",
            kube_context=CONTEXT,
        )
    assert not target_state_path.exists()


def test_adopt_live_active_release_state_reconstructs_state_from_verified_slot(tmp_path, monkeypatch):
    tke = _load()
    target_state_path = tmp_path / "target" / tke.TKE_STATE_FILENAME
    artifact = "3" * 64
    monkeypatch.setattr(tke, "_verify_target_context", lambda **_kwargs: None)

    def read_resource(*, resource, name, **_kwargs):
        if resource == "service" and name in {"api", "frontend"}:
            return {
                "metadata": {"name": name},
                "spec": {"selector": {
                    tke.SLOT_LABEL: "blue",
                    "app.kubernetes.io/name": "backend" if name == "api" else "frontend",
                }},
            }
        if resource == "ingress":
            return {
                "metadata": {"name": "aiops"},
                "spec": {"rules": [{"http": {"paths": [
                    {"backend": {"service": {"name": "frontend"}}}
                ]}}]},
            }
        if resource == "configmap":
            return {
                "metadata": {
                    "name": "aiops-release-info-blue",
                    "labels": {tke.SLOT_LABEL: "blue"},
                },
                "data": {
                    "RELEASE_SHA": SHA,
                    "SCHEMA_RELEASE": "zha33",
                    "ARTIFACT_MANIFEST_SHA256": artifact,
                },
            }
        if resource == "deployment":
            return {
                "metadata": {
                    "name": name,
                    "annotations": {
                        "aiops.example.test/release-sha": SHA,
                        "aiops.example.test/artifact-manifest-sha256": artifact,
                    },
                }
            }
        raise AssertionError((resource, name))

    monkeypatch.setattr(tke, "_read_named_resource", read_resource)
    monkeypatch.setattr(tke, "accept_slot_deploy", lambda **kwargs: {
        "status": "accepted",
        "slot": "blue",
        "images": {"backend": BACKEND, "frontend": FRONTEND},
    })
    monkeypatch.setattr(tke, "render_slot_manifests", lambda **_kwargs: b"rendered")

    result = tke.adopt_live_active_release_state(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        target_state_path=target_state_path,
        expected_active_release_sha=SHA,
        execute=True,
        confirm_release_sha=SHA,
        namespace="aiops-demo",
        kube_context=CONTEXT,
    )

    assert result["status"] == "active_release_state_adopted"
    assert result["slot"] == "blue"
    assert tke._read_release_state(target_state_path)["active"] == {
        "artifactManifestSha256": artifact,
        "backendImage": BACKEND,
        "frontendImage": FRONTEND,
        "manifestSha256": tke._manifest_sha256(b"rendered"),
        "releaseSha": SHA,
        "schemaRelease": "zha33",
        "slot": "blue",
    }


def test_slot_candidate_deploy_applies_only_the_standby_slot(monkeypatch):
    tke = _load()
    calls = []
    accepted = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[:2] == ["kubectl", "kustomize"]:
            completed = subprocess.run(argv, capture_output=True, check=False, timeout=30)
            return tke.CommandResult(completed.returncode, completed.stdout, completed.stderr)
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-demo0000"}],
                        "contexts": [{"name": CONTEXT, "context": {"cluster": "cls-demo0000"}}],
                    }
                ).encode(),
                b"",
            )
        return tke.CommandResult(0, b"{}\n", b"")

    monkeypatch.setattr(
        tke,
        "accept_slot_deploy",
        lambda **kwargs: accepted.append(kwargs) or {"status": "accepted"},
    )

    result = tke.execute_slot_deploy(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        slot="green",
        scheduler_mode="standby",
        include_public_entrypoints=False,
        execute=True,
        confirm_release_sha=SHA,
        runner=run,
    )

    assert result["status"] == "candidate_deployed"
    assert result["slot"] == "green"
    assert result["deployments"] == ["backend-green", "frontend-green", "scheduler-green"]
    assert result["services"] == ["api-green", "frontend-green"]
    assert result["schedulerMode"] == "standby"
    assert accepted == [{
        "namespace": "aiops-demo",
        "release_sha": SHA,
        "kube_context": CONTEXT,
        "slot": "green",
        "expected_scheduler_mode": "standby",
        "include_public_entrypoints": False,
        "runner": run,
    }]

    apply = next(
        kwargs["input_bytes"]
        for argv, kwargs in calls
        if argv[3:5] == ("apply", "--server-side") and "--dry-run=server" not in argv
    )
    documents = _documents(tke, apply)
    assert not any(item["kind"] == "Ingress" for item in documents)
    assert {item["metadata"]["name"] for item in documents if item["kind"] == "Service"} == {
        "api-green",
        "frontend-green",
    }
    rollout_names = [argv[5] for argv, _kwargs in calls if argv[3:5] == ("rollout", "status")]
    assert rollout_names == ["deployment/backend-green", "deployment/frontend-green", "deployment/scheduler-green"]


def test_activate_stops_the_previous_slot_scheduler_before_exposing_the_candidate(tmp_path, monkeypatch):
    tke = _load()
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
    tke._record_active_release(state_path=state_path, record=previous)
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-demo0000"}],
                        "contexts": [{"name": CONTEXT, "context": {"cluster": "cls-demo0000"}}],
                    }
                ).encode(),
                b"",
            )
        return tke.CommandResult(0, b"ok\n", b"")

    deployed = []
    monkeypatch.setattr(
        tke,
        "execute_slot_deploy",
        lambda **kwargs: deployed.append(kwargs)
        or {"status": "deployed", "manifestSha256": "6" * 64},
    )

    result = tke.execute_slot_activate(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        state_path=state_path,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release="zha32",
        artifact_manifest_sha256="3" * 64,
        slot="green",
        execute=True,
        confirm_release_sha=SHA,
        runner=run,
    )

    assert result["status"] == "activated"
    assert result["previousSlot"] == "blue"
    assert deployed == [{
        "manifests_dir": ROOT / "deploy/kubernetes/base",
        "namespace": "aiops-demo",
        "release_sha": SHA,
        "kube_context": CONTEXT,
        "backend_image": BACKEND,
        "frontend_image": FRONTEND,
        "schema_release": "zha32",
        "artifact_manifest_sha256": "3" * 64,
        "slot": "green",
        "scheduler_mode": "active",
        "include_public_entrypoints": True,
        "execute": True,
        "confirm_release_sha": SHA,
        "runner": run,
    }]
    assert calls[1][0] == (
        "kubectl", "--context", CONTEXT, "scale", "deployment/scheduler-blue", "--replicas=0", "--namespace", "aiops-demo"
    )
    assert tke._read_release_state(state_path)["active"]["slot"] == "green"


def test_rollback_reactivates_only_the_recorded_previous_slot(tmp_path, monkeypatch):
    tke = _load()
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

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-demo0000"}],
                        "contexts": [{"name": CONTEXT, "context": {"cluster": "cls-demo0000"}}],
                    }
                ).encode(),
                b"",
            )
        return tke.CommandResult(0, b"ok\n", b"")

    deployed = []
    monkeypatch.setattr(
        tke,
        "execute_slot_deploy",
        lambda **kwargs: deployed.append(kwargs)
        or {"status": "deployed", "manifestSha256": "7" * 64},
    )

    result = tke.execute_slot_rollback(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        state_path=state_path,
        execute=True,
        confirm_release_sha=SHA,
        runner=run,
    )

    assert result["status"] == "rolled_back"
    assert result["previousReleaseSha"] == "b" * 40
    assert result["slot"] == "blue"
    assert deployed[0]["release_sha"] == "b" * 40
    assert deployed[0]["slot"] == "blue"
    assert deployed[0]["scheduler_mode"] == "active"
    assert deployed[0]["include_public_entrypoints"] is True
    assert deployed[0]["authorization_release_sha"] == SHA
    assert calls[1][0] == (
        "kubectl", "--context", CONTEXT, "scale", "deployment/scheduler-green", "--replicas=0", "--namespace", "aiops-demo"
    )
    assert tke._read_release_state(state_path)["active"]["slot"] == "blue"


def test_frontend_image_resolves_the_slot_specific_api_service_at_runtime():
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    template = (ROOT / "frontend" / "nginx.conf.template").read_text(encoding="utf-8")
    entrypoint = (ROOT / "frontend" / "nginx-entrypoint.sh").read_text(encoding="utf-8")
    base_frontend = (ROOT / "deploy" / "kubernetes" / "base" / "frontend.yaml").read_text(
        encoding="utf-8"
    )

    assert "include /tmp/nginx-conf.d/*.conf;" in dockerfile
    assert "COPY nginx.conf.template /etc/aiops-nginx/default.conf.template" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/aiops-nginx-entrypoint"]' in dockerfile
    assert "proxy_pass http://__API_UPSTREAM__:8000/api/;" in template
    assert "API_UPSTREAM" in entrypoint
    assert "/tmp/nginx-conf.d/default.conf" in entrypoint
    assert "name: API_UPSTREAM" in base_frontend
    assert "value: api" in base_frontend


def test_cleanup_legacy_failed_candidate_requires_exact_release_and_never_targets_runtime_materials():
    tke = _load()
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-demo0000"}],
                        "contexts": [{"name": CONTEXT, "context": {"cluster": "cls-demo0000"}}],
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "ingress"]:
            return tke.CommandResult(1, b"", b"not found")
        if argv[3:5] == ["get", "deployment"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": argv[5],
                            "annotations": {"aiops.example.test/release-sha": SHA},
                        }
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "service"]:
            component = "backend" if argv[5] == "api" else "frontend"
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {"name": argv[5]},
                        "spec": {"selector": {"app.kubernetes.io/name": component}},
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "poddisruptionbudget"]:
            component = argv[5]
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": component,
                            "labels": {
                                "app.kubernetes.io/managed-by": "kustomize",
                                "app.kubernetes.io/part-of": "aiops",
                            },
                        },
                        "spec": {
                            "selector": {
                                "matchLabels": {
                                    "app.kubernetes.io/managed-by": "kustomize",
                                    "app.kubernetes.io/name": component,
                                    "app.kubernetes.io/part-of": "aiops",
                                }
                            }
                        },
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "configmap"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": "aiops-release-info",
                            "labels": {
                                "app.kubernetes.io/managed-by": "kustomize",
                                "app.kubernetes.io/part-of": "aiops",
                            },
                        },
                        "data": {"RELEASE_SHA": SHA},
                    }
                ).encode(),
                b"",
            )
        if tuple(argv[3:5]) in {
            ("delete", "deployment"),
            ("delete", "service"),
            ("delete", "poddisruptionbudget"),
            ("delete", "configmap"),
        }:
            return tke.CommandResult(0, b"deleted\n", b"")
        raise AssertionError(argv)

    result = tke.cleanup_failed_candidate(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        execute=True,
        confirm_release_sha=SHA,
        legacy_single_slot=True,
        runner=run,
    )

    assert result == {
        "status": "candidate_cleaned",
        "releaseSha": SHA,
        "namespace": "aiops-demo",
        "kubeContext": CONTEXT,
        "targetCluster": "cls-demo0000",
        "legacySingleSlot": True,
        "deleted": [
            "deployment/backend",
            "deployment/frontend",
            "deployment/scheduler",
            "service/api",
            "service/frontend",
            "poddisruptionbudget/backend",
            "poddisruptionbudget/frontend",
            "poddisruptionbudget/scheduler",
            "configmap/aiops-release-info",
        ],
    }
    delete_calls = [call for call, _kwargs in calls if call[3] == "delete"]
    assert delete_calls == [
        ("kubectl", "--context", CONTEXT, "delete", "deployment", "backend", "frontend", "scheduler", "--namespace", "aiops-demo", "--wait=true"),
        ("kubectl", "--context", CONTEXT, "delete", "service", "api", "frontend", "--namespace", "aiops-demo", "--wait=true"),
        ("kubectl", "--context", CONTEXT, "delete", "poddisruptionbudget", "backend", "frontend", "scheduler", "--namespace", "aiops-demo", "--wait=true"),
        ("kubectl", "--context", CONTEXT, "delete", "configmap", "aiops-release-info", "--namespace", "aiops-demo", "--wait=true"),
    ]
    assert not any(
        "secret" in call
        or "pvc" in call
        or "aiops-runtime-config" in call
        or call[3:5] == ("delete", "ingress")
        for call, _kwargs in calls
    )


def test_cleanup_legacy_orphans_removes_only_proven_pdbs_and_release_configmap():
    tke = _load()
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-demo0000"}],
                        "contexts": [{"name": CONTEXT, "context": {"cluster": "cls-demo0000"}}],
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "ingress"]:
            return tke.CommandResult(1, b"", b"not found")
        if tuple(argv[3:5]) in {("get", "deployment"), ("get", "service")}:
            return tke.CommandResult(1, b"", b"not found")
        if argv[3:5] == ["get", "poddisruptionbudget"]:
            component = argv[5]
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": component,
                            "labels": {
                                "app.kubernetes.io/managed-by": "kustomize",
                                "app.kubernetes.io/part-of": "aiops",
                            },
                        },
                        "spec": {
                            "selector": {
                                "matchLabels": {
                                    "app.kubernetes.io/managed-by": "kustomize",
                                    "app.kubernetes.io/name": component,
                                    "app.kubernetes.io/part-of": "aiops",
                                }
                            }
                        },
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "configmap"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": "aiops-release-info",
                            "labels": {
                                "app.kubernetes.io/managed-by": "kustomize",
                                "app.kubernetes.io/part-of": "aiops",
                            },
                        },
                        "data": {"RELEASE_SHA": SHA},
                    }
                ).encode(),
                b"",
            )
        if tuple(argv[3:5]) in {
            ("delete", "poddisruptionbudget"),
            ("delete", "configmap"),
        }:
            return tke.CommandResult(0, b"deleted\n", b"")
        raise AssertionError(argv)

    result = tke.cleanup_failed_candidate(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        execute=True,
        confirm_release_sha=SHA,
        legacy_single_slot=True,
        runner=run,
    )

    assert result["deleted"] == [
        "poddisruptionbudget/backend",
        "poddisruptionbudget/frontend",
        "poddisruptionbudget/scheduler",
        "configmap/aiops-release-info",
    ]
    assert not any(
        call[3:5] in {
            ("delete", "deployment"),
            ("delete", "service"),
            ("delete", "secret"),
            ("delete", "pvc"),
        }
        for call, _kwargs in calls
    )


def test_cleanup_legacy_partial_workload_set_fails_closed_before_deleting_anything():
    tke = _load()
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-demo0000"}],
                        "contexts": [{"name": CONTEXT, "context": {"cluster": "cls-demo0000"}}],
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "ingress"]:
            return tke.CommandResult(1, b"", b"not found")
        if argv[3:5] == ["get", "deployment"] and argv[5] == "backend":
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": "backend",
                            "annotations": {"aiops.example.test/release-sha": SHA},
                        }
                    }
                ).encode(),
                b"",
            )
        if tuple(argv[3:5]) in {("get", "deployment"), ("get", "service")}:
            return tke.CommandResult(1, b"", b"not found")
        raise AssertionError(argv)

    with pytest.raises(tke.TkeReleaseError, match="legacy candidate resource set is partial"):
        tke.cleanup_failed_candidate(
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            execute=True,
            confirm_release_sha=SHA,
            legacy_single_slot=True,
            runner=run,
        )

    assert not any(call[3] == "delete" for call, _kwargs in calls)


def test_cleanup_failed_blue_green_slot_never_touches_the_active_entrypoint(tmp_path):
    tke = _load()
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-demo0000"}],
                        "contexts": [{"name": CONTEXT, "context": {"cluster": "cls-demo0000"}}],
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "service"] and argv[5] in {"api", "frontend"}:
            return tke.CommandResult(1, b"", b"not found")
        if argv[3:5] == ["get", "ingress"]:
            return tke.CommandResult(1, b"", b"not found")
        if argv[3:5] == ["get", "deployment"]:
            component = argv[5].removesuffix("-green")
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": argv[5],
                            "annotations": {"aiops.example.test/release-sha": SHA},
                            "labels": {"aiops.example.test/slot": "green"},
                        },
                        "spec": {
                            "selector": {
                                "matchLabels": {
                                    "app.kubernetes.io/name": component,
                                    "aiops.example.test/slot": "green",
                                }
                            },
                            "template": {
                                "metadata": {
                                    "labels": {
                                        "app.kubernetes.io/name": component,
                                        "aiops.example.test/slot": "green",
                                    }
                                }
                            },
                        },
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "service"]:
            component = "backend" if argv[5] == "api-green" else "frontend"
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": argv[5],
                            "labels": {"aiops.example.test/slot": "green"},
                        },
                        "spec": {
                            "selector": {
                                "app.kubernetes.io/name": component,
                                "aiops.example.test/slot": "green",
                            }
                        },
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "poddisruptionbudget"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": argv[5],
                            "labels": {"aiops.example.test/slot": "green"},
                        },
                        "spec": {
                            "selector": {
                                "matchLabels": {
                                    "app.kubernetes.io/name": argv[5].removesuffix("-green"),
                                    "aiops.example.test/slot": "green",
                                }
                            }
                        },
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "configmap"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {
                            "name": "aiops-release-info-green",
                            "labels": {"aiops.example.test/slot": "green"},
                        },
                        "data": {"RELEASE_SHA": SHA},
                    }
                ).encode(),
                b"",
            )
        if tuple(argv[3:5]) in {
            ("delete", "deployment"),
            ("delete", "service"),
            ("delete", "poddisruptionbudget"),
            ("delete", "configmap"),
        }:
            return tke.CommandResult(0, b"deleted\n", b"")
        raise AssertionError(argv)

    result = tke.cleanup_failed_candidate(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        execute=True,
        confirm_release_sha=SHA,
        legacy_single_slot=False,
        slot="green",
        state_path=tmp_path / "tke-release-state.json",
        runner=run,
    )

    assert result["legacySingleSlot"] is False
    assert result["slot"] == "green"
    assert result["deleted"] == [
        "deployment/backend-green",
        "deployment/frontend-green",
        "deployment/scheduler-green",
        "service/api-green",
        "service/frontend-green",
        "poddisruptionbudget/backend-green",
        "poddisruptionbudget/frontend-green",
        "poddisruptionbudget/scheduler-green",
        "configmap/aiops-release-info-green",
    ]
    assert not any(
        "secret" in call or "pvc" in call or call[3:5] == ("delete", "ingress")
        for call, _kwargs in calls
    )


def test_cleanup_failed_blue_green_slot_refuses_the_recorded_active_slot(tmp_path):
    tke = _load()
    state_path = tmp_path / "tke-release-state.json"
    tke._record_active_release(
        state_path=state_path,
        record={
            "artifactManifestSha256": "3" * 64,
            "backendImage": BACKEND,
            "frontendImage": FRONTEND,
            "manifestSha256": "4" * 64,
            "releaseSha": SHA,
            "schemaRelease": "zha32",
            "slot": "green",
        },
    )

    def run(argv, **_kwargs):
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-demo0000"}],
                        "contexts": [{"name": CONTEXT, "context": {"cluster": "cls-demo0000"}}],
                    }
                ).encode(),
                b"",
            )
        raise AssertionError(argv)

    with pytest.raises(tke.TkeReleaseError, match="recorded active"):
        tke.cleanup_failed_candidate(
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            execute=True,
            confirm_release_sha=SHA,
            legacy_single_slot=False,
            slot="green",
            state_path=state_path,
            runner=run,
        )


def test_cleanup_failed_blue_green_slot_refuses_a_stable_service_targeting_it(tmp_path):
    tke = _load()

    def run(argv, **_kwargs):
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "clusters": [{"name": "cls-demo0000"}],
                        "contexts": [{"name": CONTEXT, "context": {"cluster": "cls-demo0000"}}],
                    }
                ).encode(),
                b"",
            )
        if argv[3:5] == ["get", "service"] and argv[5] == "api":
            return tke.CommandResult(
                0,
                json.dumps(
                    {
                        "metadata": {"name": "api"},
                        "spec": {
                            "selector": {"aiops.example.test/slot": "green"}
                        },
                    }
                ).encode(),
                b"",
            )
        raise AssertionError(argv)

    with pytest.raises(tke.TkeReleaseError, match="stable Service targets"):
        tke.cleanup_failed_candidate(
            namespace="aiops-demo",
            release_sha=SHA,
            kube_context=CONTEXT,
            execute=True,
            confirm_release_sha=SHA,
            legacy_single_slot=False,
            slot="green",
            state_path=tmp_path / "tke-release-state.json",
            runner=run,
        )
