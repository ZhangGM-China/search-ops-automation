from pathlib import Path

import pytest

from scripts.release import image_worker_release as worker
from scripts.release.release import build_parser


def test_worker_release_uses_the_existing_entrypoint():
    args = build_parser().parse_args([
        "tke-image-worker", "--phase", "preview", "--release-sha", "a" * 40,
        "--kube-context", worker.CONTEXT,
    ])
    assert args.phase == "preview"


def test_worker_plan_is_scoped_to_three_resources():
    plan = worker.preview("a" * 40)
    assert plan["namespace"] == "aiops-demo"
    assert plan["resources"] == ["Deployment/qg-atlas-browser-worker", "Service/qg-atlas-browser-worker", "NetworkPolicy/qg-atlas-browser-worker"]
    assert plan["platform"] == "linux/amd64"


def test_rejects_mutable_image_and_wrong_target():
    with pytest.raises(ValueError):
        worker.validate_target("other", "aiops-demo")
    with pytest.raises(ValueError):
        worker.validate_image("registry.example.test/aiops/backend:latest")
    assert worker.validate_image("registry.example.test/aiops/qg-atlas-browser-worker@sha256:" + "b" * 64)


def test_render_does_not_modify_application_deployments():
    root = Path(__file__).resolve().parents[1]
    image = "registry.example.test/aiops/qg-atlas-browser-worker@sha256:" + "b" * 64
    resources = worker.render(root, image, "a" * 40)
    assert {r["metadata"]["name"] for r in resources} == {"qg-atlas-browser-worker"}
    deploy = next(r for r in resources if r["kind"] == "Deployment")
    env = {r["name"]: r.get("value") for r in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["QG_BROWSER_DISPLAY_MODE"] == "ozone-headless"
    assert env["QG_BROWSER_REQUIRE_PROXY"] == "false"
    assert deploy["spec"]["template"]["spec"]["containers"][0]["image"] == image


def test_rejects_invalid_probe_sources():
    for url in ["https://static.atlas.example.test/", "https://static.atlas.example.test.evil/assets/x.jpg", "http://static.atlas.example.test/assets/x.jpg", "https://user@static.atlas.example.test/assets/x.jpg", "https://static.atlas.example.test:444/assets/x.jpg"]:
        with pytest.raises(ValueError):
            worker.validate_url(url)


def test_state_permissions_and_sha_are_fail_closed(tmp_path):
    import os
    state = tmp_path / "private" / worker.STATE_FILE
    image = worker.REPOSITORY + "@sha256:" + "b" * 64
    record = {"releaseSha": "a" * 40, "image": image}
    worker.write_state(state, record)
    assert worker.read_state(state, "a" * 40) == record
    with pytest.raises(ValueError):
        worker.read_state(state, "c" * 40)
    os.chmod(state, 0o644)
    with pytest.raises(ValueError):
        worker.read_state(state, "a" * 40)


def test_secret_creation_never_overwrites_existing(monkeypatch):
    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        return '{"metadata":{"name":"qg-atlas-browser-worker"}}'
    monkeypatch.setattr(worker, "kubectl", command)
    with pytest.raises(ValueError, match="already exists"):
        worker.provision_secret()
    assert len(calls) == 1


def test_secret_creation_keeps_credentials_out_of_output(monkeypatch):
    import json
    calls = []
    def command(*args, **kwargs):
        calls.append((args, kwargs))
        return ""
    monkeypatch.setattr(worker, "kubectl", command)
    result = worker.provision_secret()
    payload = json.loads(calls[-1][1]["input_text"])
    assert calls[-1][1]["sensitive"] is True
    assert payload["metadata"]["name"] == worker.NAME
    assert "bridge-token" in payload["data"]
    assert payload["data"]["bridge-token"] not in json.dumps(result)


def test_deploy_dry_run_failure_does_not_apply_or_record(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    image = worker.REPOSITORY + "@sha256:" + "b" * 64
    state = tmp_path / "private" / worker.STATE_FILE
    worker.write_state(state, {"releaseSha": "a" * 40, "image": image})
    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        if args[0] == "get":
            return "present"
        raise worker.ImageWorkerReleaseError("dry-run rejected")
    monkeypatch.setattr(worker, "kubectl", command)
    with pytest.raises(ValueError, match="dry-run"):
        worker.deploy(root, state, "a" * 40, "https://static.atlas.example.test/assets/x.jpg")
    assert len(calls) == 2
    assert "--dry-run=server" in calls[-1]
    assert "deployAttempted" not in worker.read_state(state, "a" * 40)


def test_deploy_missing_secret_cannot_apply(monkeypatch, tmp_path):
    state = tmp_path / "private" / worker.STATE_FILE
    worker.write_state(state, {"releaseSha": "a" * 40, "image": worker.REPOSITORY + "@sha256:" + "b" * 64})
    monkeypatch.setattr(worker, "kubectl", lambda *a, **kw: "")
    with pytest.raises(ValueError, match="Secret is absent"):
        worker.deploy(tmp_path, state, "a" * 40, "https://static.atlas.example.test/assets/x.jpg")


def test_verify_requires_ten_distinct_real_urls(tmp_path):
    state = tmp_path / "private" / worker.STATE_FILE
    worker.write_state(state, {"releaseSha": "a" * 40, "image": worker.REPOSITORY + "@sha256:" + "b" * 64})
    with pytest.raises(ValueError, match="ten distinct"):
        worker.verify(state, "a" * 40, ["https://static.atlas.example.test/assets/x.jpg"] * 10)


def test_render_sets_probe_without_secret_values():
    root = Path(__file__).resolve().parents[1]
    resources = worker.render(root, worker.REPOSITORY + "@sha256:" + "b" * 64, "a" * 40, "https://static.atlas.example.test/assets/x.jpg")
    d = next(r for r in resources if r["kind"] == "Deployment")
    env = d["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {r["name"]: r.get("value") for r in env}["QG_BROWSER_PROBE_URL"] == "https://static.atlas.example.test/assets/x.jpg"
    assert all(r["metadata"]["namespace"] == worker.NAMESPACE for r in resources)
    assert d["spec"]["strategy"]["type"] == "Recreate"


def test_first_install_rollback_scales_only_worker(monkeypatch, tmp_path):
    import json
    image = worker.REPOSITORY + "@sha256:" + "b" * 64
    state = tmp_path / "private" / worker.STATE_FILE
    worker.write_state(state, {"releaseSha": "a" * 40, "image": image, "previousResources": []})
    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        if args[0] == "get":
            return json.dumps({"metadata":{"annotations":{"aiops/release-sha":"a" * 40}}, "spec":{"template":{"spec":{"containers":[{"image":image}]}}}})
        return ""
    monkeypatch.setattr(worker, "kubectl", command)
    assert worker.rollback(state, "a" * 40)["previousRestored"] is False
    assert calls[-1] == ("scale", "deployment/" + worker.NAME, "--replicas=0")
    assert not any("delete" in call for call in calls)


def test_build_rejects_insecure_state_before_registry_writes(monkeypatch, tmp_path):
    import os
    os.chmod(tmp_path, 0o755)
    calls = []
    monkeypatch.setattr(worker, "run", lambda *a, **kw: calls.append(a) or "")
    with pytest.raises(ValueError, match="state directory"):
        worker.build(tmp_path, "a" * 40, tmp_path / worker.STATE_FILE)
    assert calls == []


def test_build_records_immutable_digest(monkeypatch, tmp_path):
    import json
    from contextlib import contextmanager
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    sha = "a" * 40
    digest = "sha256:" + "b" * 64
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "git": return "c" * 40
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Os":"linux", "Architecture":"amd64", "Config":{"Labels":{"org.opencontainers.image.revision":sha}}}])
        return ""
    monkeypatch.setattr(worker, "run", run)
    monkeypatch.setattr(worker.tke_candidate, "_tcr_login_token", lambda **kw: ("user", b"not-real-test-token"))
    @contextmanager
    def environment(): yield {}
    monkeypatch.setattr(worker.tke_candidate, "_temporary_docker_environment", environment)
    monkeypatch.setattr(worker.tke_candidate, "_execute", lambda *a, **kw: worker.tke_candidate.CommandResult(0,b"",b""))
    monkeypatch.setattr(worker.tke_candidate, "_remote_digest", lambda **kw: digest)
    result = worker.build(tmp_path, sha, private / worker.STATE_FILE)
    assert result["image"] == worker.REPOSITORY + "@" + digest
    assert result["sourceTreeSha"] == "c" * 40
    assert result["status"] == "built"
    assert "linux/amd64" in calls[1]


def test_deploy_records_snapshot_and_applies_scoped_list(monkeypatch, tmp_path):
    import json
    sha = "a" * 40
    state = tmp_path / "private" / worker.STATE_FILE
    image = worker.REPOSITORY + "@sha256:" + "b" * 64
    worker.write_state(state, {"releaseSha":sha, "image":image})
    calls = []
    def command(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("get", "secret"): return "present"
        if args[0] == "get": return '{"items":[]}'
        return ""
    monkeypatch.setattr(worker, "kubectl", command)
    result = worker.deploy(Path(__file__).resolve().parents[1], state, sha, "https://static.atlas.example.test/assets/x.jpg")
    assert result["status"] == "deployed"
    assert "previousResources" not in result
    assert worker.read_state(state, sha)["previousResources"] == []
    applied = [json.loads(kw["input_text"]) for args,kw in calls if args[0] == "apply"]
    assert all(p["kind"] == "List" and len(p["items"]) == 3 for p in applied)
    assert calls[-1][0][:3] == ("rollout", "status", "deployment/" + worker.NAME)


def test_verify_real_path_requires_digest_and_redacts_token(monkeypatch, tmp_path):
    import json
    sha = "a" * 40
    digest = "sha256:" + "b" * 64
    image = worker.REPOSITORY + "@" + digest
    state = tmp_path / "private" / worker.STATE_FILE
    worker.write_state(state, {"releaseSha":sha, "image":image})
    urls = [f"https://static.atlas.example.test/assets/{i}.jpg" for i in range(10)]
    calls = []
    def command(*args, **kwargs):
        calls.append((args,kwargs))
        if args[:2] == ("get","deployment"):
            return json.dumps({"metadata":{"annotations":{"aiops/release-sha":sha},"generation":1}, "spec":{"template":{"spec":{"containers":[{"image":image}]}}},"status":{"observedGeneration":1,"readyReplicas":1}})
        if args[0] == "get":
            return json.dumps({"items":[{"metadata":{"name":"pod"},"status":{"phase":"Running","containerStatuses":[{"ready":True,"imageID":image}]}}]})
        return json.dumps({"health":200,"unauth":401,"probe":200,"results":[{"url":u,"status":200,"contentType":"image/jpeg","bytes":5} for u in urls]})
    monkeypatch.setattr(worker, "kubectl", command)
    assert worker.verify(state, sha, urls)["status"] == "source_verified"
    assert "process.env.QG_BROWSER_BRIDGE_TOKEN" in calls[-1][0][-1]
    assert json.loads(calls[-1][1]["input_text"]) == urls


def test_verify_rejects_ready_pod_with_wrong_image(monkeypatch, tmp_path):
    import json
    sha = "a" * 40
    state = tmp_path / "private" / worker.STATE_FILE
    image = worker.REPOSITORY + "@sha256:" + "b" * 64
    worker.write_state(state, {"releaseSha":sha,"image":image})
    monkeypatch.setattr(worker,"kubectl",lambda *a,**kw:json.dumps({"metadata":{"annotations":{"aiops/release-sha":sha},"generation":1},"spec":{"template":{"spec":{"containers":[{"image":"wrong"}]}}},"status":{"readyReplicas":1,"observedGeneration":1}}))
    with pytest.raises(ValueError, match="Deployment differs"):
        worker.verify(state,sha,[f"https://static.atlas.example.test/assets/{i}.jpg" for i in range(10)])


def test_run_errors_redact_sensitive_stdin(monkeypatch):
    import subprocess
    monkeypatch.setattr(worker.subprocess,"run",lambda *a,**kw:subprocess.CompletedProcess(a[0],1,"","fake-secret-value"))
    with pytest.raises(ValueError) as error:
        worker.run(["kubectl","create"],input_text="fake-secret-value",sensitive=True)
    assert "fake-secret-value" not in str(error.value)
    with pytest.raises(ValueError,match="fake-secret-value"):
        worker.run(["kubectl","get"])


def test_run_timeout_is_typed(monkeypatch):
    import subprocess
    def failed(*a,**kw): raise subprocess.TimeoutExpired(a[0],1)
    monkeypatch.setattr(worker.subprocess,"run",failed)
    with pytest.raises(ValueError,match="execution failed"):
        worker.run(["kubectl","get"])


def test_container_build_proxy_is_separate_from_host_client_proxy(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:7890')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7890')
    monkeypatch.setenv('AIOPS_WORKER_BUILD_PROXY', 'http://host.docker.internal:7890')
    assert worker.build_proxy_args() == [
        '--build-arg', 'HTTP_PROXY=http://host.docker.internal:7890',
        '--build-arg', 'HTTPS_PROXY=http://host.docker.internal:7890',
    ]
    assert worker.os.environ['HTTPS_PROXY'] == 'http://127.0.0.1:7890'


def test_build_proxy_rejects_credentials(monkeypatch):
    monkeypatch.setenv('AIOPS_WORKER_BUILD_PROXY', 'http://user:password@proxy:8080')
    with pytest.raises(ValueError, match='build proxy'):
        worker.build_proxy_args()
