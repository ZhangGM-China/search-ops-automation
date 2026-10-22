import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "release" / "tke_candidate.py"

SHA = "a" * 40
TREE_SHA = "b" * 40
SCHEMA_RELEASE = "zha32"
BACKEND_LOCAL = f"aiops/backend:{SHA}"
FRONTEND_LOCAL = f"aiops/frontend:{SHA}"
BACKEND_REMOTE = f"registry.example.test/aiops/backend:{SHA}"
FRONTEND_REMOTE = f"registry.example.test/aiops/frontend:{SHA}"
REGISTRY_CLOUD_ID = "tcr-314n6ws4"


def _load():
    spec = importlib.util.spec_from_file_location("test_tke_candidate", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _image_inspect(*, image_id: str, component: str, artifact_manifest_sha256: str) -> bytes:
    return json.dumps(
        [
            {
                "Architecture": "amd64",
                "Config": {
                    "Labels": {
                        "aiops.artifact-manifest-sha256": artifact_manifest_sha256,
                        "aiops.component": component,
                        "aiops.schema-release": SCHEMA_RELEASE,
                        "org.opencontainers.image.revision": SHA,
                    }
                },
                "Id": image_id,
                "Os": "linux",
            }
        ]
    ).encode()


def test_candidate_preview_binds_clean_source_tree_to_tcr_image_tags():
    candidate = _load()

    preview = candidate.build_candidate_preview(
        release_sha=SHA,
        schema_release=SCHEMA_RELEASE,
        source_tree_sha=TREE_SHA,
    )

    assert preview["status"] == "tke_candidate_preview"
    assert preview["releaseSha"] == SHA
    assert preview["sourceTreeSha"] == TREE_SHA
    assert preview["targetPlatform"] == "linux/amd64"
    assert preview["localImages"] == {
        "backend": BACKEND_LOCAL,
        "frontend": FRONTEND_LOCAL,
    }
    assert preview["remoteTags"] == {
        "backend": BACKEND_REMOTE,
        "frontend": FRONTEND_REMOTE,
    }
    assert len(preview["artifactManifestSha256"]) == 64
    assert "token" not in json.dumps(preview, sort_keys=True).lower()


def test_candidate_requires_exact_confirmation_before_build_or_registry_write():
    candidate = _load()
    preview = candidate.build_candidate_preview(
        release_sha=SHA,
        schema_release=SCHEMA_RELEASE,
        source_tree_sha=TREE_SHA,
    )

    with pytest.raises(candidate.TkeCandidateError, match="confirm-release-sha"):
        candidate.create_candidate(
            repo=ROOT,
            preview=preview,
            execute=True,
            confirm_release_sha=None,
            runner=lambda *args, **kwargs: pytest.fail("must fail before command"),
        )


def test_tcr_login_creates_short_lived_token_from_registry_cloud_id():
    candidate = _load()
    calls = []

    def run(argv, **_kwargs):
        argv = tuple(argv)
        calls.append(argv)
        if argv[:3] == ("tccli", "tcr", "DescribeInstances"):
            return candidate.CommandResult(
                0,
                json.dumps(
                    {
                        "Registries": [
                            {
                                "PublicDomain": "registry.example.test",
                                "RegistryId": REGISTRY_CLOUD_ID,
                                "RegistryName": "searchops-registry",
                                "Status": "Running",
                            }
                        ],
                        "TotalCount": 1,
                    }
                ).encode(),
                b"",
            )
        if argv[:3] == ("tccli", "tcr", "CreateInstanceToken"):
            assert argv[argv.index("--RegistryId") + 1] == REGISTRY_CLOUD_ID
            assert argv[argv.index("--TokenType") + 1] == "temp"
            return candidate.CommandResult(
                0,
                b'{"ExpTime":1735880997750,"Token":"not-a-real-token","TokenId":"","Username":"100000000001"}',
                b"",
            )
        raise AssertionError(argv)

    username, token_input = candidate._tcr_login_token(runner=run)

    assert username == "100000000001"
    assert token_input == b"not-a-real-token\n"
    assert [call[:3] for call in calls] == [
        ("tccli", "tcr", "DescribeInstances"),
        ("tccli", "tcr", "CreateInstanceToken"),
    ]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "TCR instance readback is invalid"),
        (b'{"Registries":"not-a-list"}', "TCR instance readback is invalid"),
        (
            json.dumps(
                {
                    "Registries": [
                        {
                            "PublicDomain": "registry.example.test",
                            "RegistryId": REGISTRY_CLOUD_ID,
                            "RegistryName": "searchops-registry",
                            "Status": "Running",
                        },
                        {
                            "PublicDomain": "registry.example.test",
                            "RegistryId": "tcr-another",
                            "RegistryName": "searchops-registry",
                            "Status": "Running",
                        },
                    ]
                }
            ).encode(),
            "TCR registry identity is not unique and running",
        ),
        (
            json.dumps(
                {
                    "Registries": [
                        {
                            "PublicDomain": "registry.example.test",
                            "RegistryId": "searchops-registry",
                            "RegistryName": "searchops-registry",
                            "Status": "Running",
                        }
                    ]
                }
            ).encode(),
            "TCR registry ID is invalid",
        ),
    ],
)
def test_tcr_registry_id_rejects_invalid_instance_readback(payload, message):
    candidate = _load()

    def run(argv, **_kwargs):
        assert tuple(argv[:3]) == ("tccli", "tcr", "DescribeInstances")
        return candidate.CommandResult(0, payload, b"")

    with pytest.raises(candidate.TkeCandidateError, match=message):
        candidate._tcr_registry_id(runner=run)


def test_tcr_login_accepts_tccli_response_wrapper_and_rejects_invalid_token():
    candidate = _load()

    def run(argv, **_kwargs):
        argv = tuple(argv)
        if argv[:3] == ("tccli", "tcr", "DescribeInstances"):
            return candidate.CommandResult(
                0,
                json.dumps(
                    {
                        "Response": {
                            "Registries": [
                                {
                                    "PublicDomain": "registry.example.test",
                                    "RegistryId": REGISTRY_CLOUD_ID,
                                    "RegistryName": "searchops-registry",
                                    "Status": "Running",
                                }
                            ]
                        }
                    }
                ).encode(),
                b"",
            )
        if argv[:3] == ("tccli", "tcr", "CreateInstanceToken"):
            return candidate.CommandResult(
                0,
                b'{"Response":{"Token":"","TokenId":"","Username":"100000000001"}}',
                b"",
            )
        raise AssertionError(argv)

    with pytest.raises(candidate.TkeCandidateError, match="TCR token readback is invalid"):
        candidate._tcr_login_token(runner=run)


def test_tcr_login_rejects_long_lived_token_response():
    candidate = _load()

    def run(argv, **_kwargs):
        argv = tuple(argv)
        if argv[:3] == ("tccli", "tcr", "DescribeInstances"):
            return candidate.CommandResult(
                0,
                json.dumps(
                    {
                        "Registries": [
                            {
                                "PublicDomain": "registry.example.test",
                                "RegistryId": REGISTRY_CLOUD_ID,
                                "RegistryName": "searchops-registry",
                                "Status": "Running",
                            }
                        ]
                    }
                ).encode(),
                b"",
            )
        if argv[:3] == ("tccli", "tcr", "CreateInstanceToken"):
            return candidate.CommandResult(
                0,
                b'{"ExpTime":2051237057817,"Token":"not-a-real-token","TokenId":"long-lived-token","Username":"100000000001"}',
                b"",
            )
        raise AssertionError(argv)

    with pytest.raises(candidate.TkeCandidateError, match="TCR token readback is invalid"):
        candidate._tcr_login_token(runner=run)


def test_candidate_build_uses_default_docker_config_and_tcr_credentials_stay_isolated(
    monkeypatch,
):
    candidate = _load()
    monkeypatch.setenv("SEARCHOPS_MCP_TOKEN", "test-only-secret")
    preview = candidate.build_candidate_preview(
        release_sha=SHA,
        schema_release=SCHEMA_RELEASE,
        source_tree_sha=TREE_SHA,
    )
    calls = []
    remote_payloads = {
        "backend": b'{"mediaType":"application/vnd.oci.image.manifest.v1+json","schemaVersion":2}',
        "frontend": b'{"mediaType":"application/vnd.docker.distribution.manifest.v2+json","schemaVersion":2}',
    }
    remote_digests = {
        component: "sha256:" + hashlib.sha256(payload).hexdigest()
        for component, payload in remote_payloads.items()
    }
    image_ids = {
        "backend": "sha256:" + "1" * 64,
        "frontend": "sha256:" + "2" * 64,
    }
    pushed = set()
    docker_config_paths = set()
    build_config_overrides = []
    unrelated_secret_leaks = []

    def run(argv, **kwargs):
        argv = tuple(argv)
        calls.append((argv, kwargs))
        if argv[0] == candidate.sys.executable:
            assert kwargs["environment"] == {
                key: value
                for key, value in kwargs["environment"].items()
                if key in candidate.DOCKER_ENVIRONMENT_KEYS
            }
            if argv[1].endswith("check_runtime_base_image.py"):
                assert "--dockerfile" in argv
                assert "--policy" in argv
                return candidate.CommandResult(0, b"", b"")
            if argv[1].endswith("supply_chain_gate.py"):
                assert argv[argv.index("--release-sha") + 1] == SHA
                assert argv[argv.index("--artifact-manifest-sha256") + 1] == (
                    preview["artifactManifestSha256"]
                )
                assert set(
                    argv[index + 1]
                    for index, item in enumerate(argv)
                    if item == "--image"
                ) == {
                    f"backend={BACKEND_LOCAL}",
                    f"frontend={FRONTEND_LOCAL}",
                }
                return candidate.CommandResult(0, b"", b"")
            raise AssertionError(argv)
        if argv[0] == "docker":
            environment = kwargs["environment"]
            unrelated_secret_leaks.append("SEARCHOPS_MCP_TOKEN" in environment)
            if argv[:2] in {
                ("docker", "version"),
                ("docker", "build"),
                ("docker", "image"),
            }:
                # The build must retain Docker's normal CLI-plugin discovery.
                # TCR credentials do not exist until after image construction.
                build_config_overrides.append("DOCKER_CONFIG" in environment)
            else:
                docker_config = Path(environment["DOCKER_CONFIG"])
                assert docker_config.is_dir()
                assert docker_config.stat().st_mode & 0o777 == 0o700
                docker_config_paths.add(docker_config)
        if argv[:3] == ("docker", "version", "--format"):
            return candidate.CommandResult(0, b"29.4.0\n", b"")
        if argv[:2] == ("docker", "build"):
            return candidate.CommandResult(0, b"", b"")
        if argv[:3] == ("docker", "image", "inspect"):
            image = argv[-1]
            component = "backend" if "/backend" in image else "frontend"
            return candidate.CommandResult(
                0,
                _image_inspect(
                    image_id=image_ids[component],
                    component=component,
                    artifact_manifest_sha256=preview["artifactManifestSha256"],
                ),
                b"",
            )
        if argv[:3] == ("tccli", "tcr", "DescribeInstances"):
            return candidate.CommandResult(
                0,
                json.dumps(
                    {
                        "Registries": [
                            {
                                "PublicDomain": "registry.example.test",
                                "RegistryId": REGISTRY_CLOUD_ID,
                                "RegistryName": "searchops-registry",
                                "Status": "Running",
                            }
                        ],
                        "TotalCount": 1,
                    }
                ).encode(),
                b"",
            )
        if argv[:3] == ("tccli", "tcr", "CreateInstanceToken"):
            assert argv[argv.index("--RegistryId") + 1] == REGISTRY_CLOUD_ID
            assert argv[argv.index("--TokenType") + 1] == "temp"
            return candidate.CommandResult(
                0,
                b'{"ExpTime":1735880997750,"Token":"not-a-real-token","TokenId":"","Username":"100000000001"}',
                b"",
            )
        if argv[:2] == ("docker", "login"):
            assert kwargs["input_bytes"] == b"not-a-real-token\n"
            return candidate.CommandResult(0, b"Login Succeeded", b"")
        if argv[:2] == ("docker", "tag"):
            return candidate.CommandResult(0, b"", b"")
        if argv[:2] == ("docker", "push"):
            pushed.add("backend" if "/backend:" in argv[-1] else "frontend")
            return candidate.CommandResult(0, b"", b"")
        if argv[:5] == ("docker", "buildx", "imagetools", "inspect", "--raw"):
            remote_tag = argv[-1]
            component = "backend" if "/backend:" in remote_tag else "frontend"
            if component not in pushed:
                return candidate.CommandResult(1, b"", b"manifest unknown")
            return candidate.CommandResult(0, remote_payloads[component], b"")
        if argv[:2] == ("docker", "pull"):
            assert argv[:4] == ("docker", "pull", "--platform", candidate.TARGET_PLATFORM)
            component = "backend" if "/backend@" in argv[4] else "frontend"
            assert argv[4].endswith(remote_digests[component])
            return candidate.CommandResult(0, b"", b"")
        raise AssertionError(argv)

    record = candidate.create_candidate(
        repo=ROOT,
        preview=preview,
        execute=True,
        confirm_release_sha=SHA,
        runner=run,
    )

    assert record == {
        "artifactManifestSha256": preview["artifactManifestSha256"],
        "backendImage": f"registry.example.test/aiops/backend@{remote_digests['backend']}",
        "clusterPullVerified": False,
        "frontendImage": f"registry.example.test/aiops/frontend@{remote_digests['frontend']}",
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
    }
    assert pushed == {"backend", "frontend"}
    runtime_gate = next(
        index
        for index, call in enumerate(calls)
        if call[0][0] == candidate.sys.executable
        and call[0][1].endswith("check_runtime_base_image.py")
    )
    first_build = next(
        index for index, call in enumerate(calls) if call[0][:2] == ("docker", "build")
    )
    supply_chain_gate = next(
        index
        for index, call in enumerate(calls)
        if call[0][0] == candidate.sys.executable
        and call[0][1].endswith("supply_chain_gate.py")
    )
    token_request = next(
        index
        for index, call in enumerate(calls)
        if call[0][:3] == ("tccli", "tcr", "DescribeInstances")
    )
    assert runtime_gate < first_build < supply_chain_gate < token_request
    assert any(call[0][:2] == ("docker", "login") for call in calls)
    assert not any(build_config_overrides), "build Docker config was overridden"
    assert not any(unrelated_secret_leaks), "unrelated process environment leaked"
    assert len(docker_config_paths) == 1
    assert all(not path.exists() for path in docker_config_paths)
    assert all(
        "not-a-real-token" not in json.dumps({"argv": call[0], "kwargs": {key: value for key, value in call[1].items() if key != "input_bytes"}}, default=str)
        for call in calls
    )


def test_remote_digest_selects_linux_amd64_child_from_attested_index():
    candidate = _load()
    child_digest = "sha256:" + "a" * 64
    attestation_digest = "sha256:" + "b" * 64
    payload = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"digest": child_digest, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": attestation_digest, "platform": {"os": "unknown", "architecture": "unknown"}},
        ],
    }).encode()

    def run(argv, **_kwargs):
        assert argv[:5] == ["docker", "buildx", "imagetools", "inspect", "--raw"]
        return candidate.CommandResult(0, payload, b"")

    assert candidate._remote_digest(
        remote_tag="registry.example.test/aiops/frontend:abc",
        component="frontend",
        runner=run,
        environment={},
    ) == child_digest


def test_remote_digest_rejects_ambiguous_linux_amd64_children():
    candidate = _load()
    payload = json.dumps({
        "schemaVersion": 2,
        "manifests": [
            {"digest": "sha256:" + digit * 64, "platform": {"os": "linux", "architecture": "amd64"}}
            for digit in ("a", "b")
        ],
    }).encode()

    with pytest.raises(candidate.TkeCandidateError, match="missing or ambiguous"):
        candidate._remote_digest(
            remote_tag="registry.example.test/aiops/frontend:abc",
            component="frontend",
            runner=lambda *_args, **_kwargs: candidate.CommandResult(0, payload, b""),
            environment={},
        )


def test_temporary_tcr_config_preserves_buildx_plugin_without_copying_docker_config(
    tmp_path, monkeypatch
):
    candidate = _load()
    default_config = tmp_path / "default-docker"
    plugin_dir = default_config / "cli-plugins"
    plugin_dir.mkdir(parents=True)
    buildx = plugin_dir / "docker-buildx"
    buildx.write_text("test plugin\n", encoding="utf-8")
    buildx.chmod(0o700)
    (default_config / "config.json").write_text(
        '{"auths":{"example.invalid":{"auth":"must-not-copy"}}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(default_config))

    with candidate._temporary_docker_environment() as environment:
        temporary_config = Path(environment["DOCKER_CONFIG"])
        linked_buildx = temporary_config / "cli-plugins" / "docker-buildx"
        assert linked_buildx.is_symlink()
        assert linked_buildx.resolve() == buildx
        assert not (temporary_config / "config.json").exists()

    assert not temporary_config.exists()


def test_candidate_build_context_cannot_include_runtime_config_or_secret_files(tmp_path):
    candidate = _load()
    for name in (".dockerignore", "config.yaml", ".env"):
        (tmp_path / name).write_text("placeholder\n", encoding="utf-8")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / ".dockerignore").write_text("node_modules\n", encoding="utf-8")

    with pytest.raises(candidate.TkeCandidateError, match="build context"):
        candidate.validate_build_context(repo=tmp_path)


def test_candidate_build_context_requires_runtime_config_and_secret_exclusions(tmp_path):
    candidate = _load()
    (tmp_path / ".dockerignore").write_text(
        ".env\n.env.*\nconfig.yaml\nfrontend/\n",
        encoding="utf-8",
    )
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / ".dockerignore").write_text(
        ".env\n.env.*\nconfig.yaml\nnode_modules\n",
        encoding="utf-8",
    )

    candidate.validate_build_context(repo=tmp_path)


def test_candidate_release_state_keeps_only_immutable_identity_and_rejects_drift(tmp_path):
    candidate = _load()
    preview = candidate.build_candidate_preview(
        release_sha=SHA,
        schema_release=SCHEMA_RELEASE,
        source_tree_sha=TREE_SHA,
    )
    record = {
        "artifactManifestSha256": preview["artifactManifestSha256"],
        "backendImage": "registry.example.test/aiops/backend@sha256:" + "1" * 64,
        "clusterPullVerified": False,
        "frontendImage": "registry.example.test/aiops/frontend@sha256:" + "2" * 64,
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
    }
    state_path = tmp_path / "tke-candidate-state.json"

    candidate.record_candidate(state_path=state_path, record=record)

    assert candidate.read_candidate(state_path=state_path, release_sha=SHA) == record
    assert state_path.stat().st_mode & 0o777 == 0o600
    state_path.write_text(json.dumps({**record, "unexpected": True}), encoding="utf-8")
    with pytest.raises(candidate.TkeCandidateError, match="state"):
        candidate.read_candidate(state_path=state_path, release_sha=SHA)


def test_cluster_pull_rehearsal_persists_the_immutable_image_readback_proof(tmp_path):
    candidate = _load()
    preview = candidate.build_candidate_preview(
        release_sha=SHA,
        schema_release=SCHEMA_RELEASE,
        source_tree_sha=TREE_SHA,
    )
    record = {
        "artifactManifestSha256": preview["artifactManifestSha256"],
        "backendImage": "registry.example.test/aiops/backend@sha256:" + "1" * 64,
        "clusterPullVerified": False,
        "frontendImage": "registry.example.test/aiops/frontend@sha256:" + "2" * 64,
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
    }
    kube_context = "cls-demo0000-000000000000-context-default"
    evidence = {
        "status": "cluster_pull_verified",
        "releaseSha": SHA,
        "namespace": "aiops-demo",
        "kubeContext": kube_context,
        "targetCluster": "cls-demo0000",
        "images": {
            "backend": record["backendImage"],
            "frontend": record["frontendImage"],
        },
    }
    state_path = tmp_path / candidate.TKE_REHEARSAL_STATE_FILENAME

    candidate.record_cluster_pull_rehearsal(
        state_path=state_path,
        candidate=record,
        kube_context=kube_context,
        evidence=evidence,
    )

    candidate.require_cluster_pull_rehearsal(
        state_path=state_path,
        candidate=record,
        kube_context=kube_context,
    )
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "backendImage": record["backendImage"],
        "frontendImage": record["frontendImage"],
        "kubeContext": kube_context,
        "releaseSha": SHA,
        "status": "cluster_pull_verified",
    }


def test_mysql_rehearsal_persists_only_the_matching_read_only_connection_proof(tmp_path):
    candidate = _load()
    preview = candidate.build_candidate_preview(
        release_sha=SHA,
        schema_release=SCHEMA_RELEASE,
        source_tree_sha=TREE_SHA,
    )
    record = {
        "artifactManifestSha256": preview["artifactManifestSha256"],
        "backendImage": "registry.example.test/aiops/backend@sha256:" + "1" * 64,
        "clusterPullVerified": False,
        "frontendImage": "registry.example.test/aiops/frontend@sha256:" + "2" * 64,
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
    }
    kube_context = "cls-demo0000-000000000000-context-default"
    evidence = {
        "backendImage": record["backendImage"],
        "kubeContext": kube_context,
        "namespace": "aiops-demo",
        "readOnlyQuery": True,
        "releaseSha": SHA,
        "status": "mysql_rehearsal_verified",
        "targetCluster": "cls-demo0000",
    }
    state_path = tmp_path / candidate.TKE_MYSQL_REHEARSAL_STATE_FILENAME

    candidate.record_mysql_rehearsal(
        state_path=state_path,
        candidate=record,
        kube_context=kube_context,
        evidence=evidence,
    )

    proof = candidate.require_mysql_rehearsal(
        state_path=state_path,
        candidate=record,
        kube_context=kube_context,
    )
    assert proof == {
        "backendImage": record["backendImage"],
        "kubeContext": kube_context,
        "readOnlyQuery": True,
        "releaseSha": SHA,
        "status": "mysql_rehearsal_verified",
    }
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_release_cli_exposes_tke_candidate_without_legacy_ssh_or_image_inputs():
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_release_entry", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)

    parser = release.build_parser()
    preview = parser.parse_args(
        ["tke-candidate", "--release-sha", SHA, "--schema-release", SCHEMA_RELEASE]
    )
    assert preview.action == "tke-candidate"
    assert preview.release_sha == SHA
    assert not hasattr(preview, "host")
    assert not hasattr(preview, "backend_image")


def test_release_cli_persists_candidate_only_after_candidate_creation(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_release_execute", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)
    calls = []

    preview = {
        "artifactManifestSha256": "3" * 64,
        "localImages": {"backend": BACKEND_LOCAL, "frontend": FRONTEND_LOCAL},
        "registry": {"host": "registry.example.test", "id": "searchops-registry", "namespace": "aiops"},
        "releaseSha": SHA,
        "remoteTags": {"backend": BACKEND_REMOTE, "frontend": FRONTEND_REMOTE},
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
        "status": "tke_candidate_preview",
        "targetPlatform": "linux/amd64",
    }
    record = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": "registry.example.test/aiops/backend@sha256:" + "1" * 64,
        "clusterPullVerified": False,
        "frontendImage": "registry.example.test/aiops/frontend@sha256:" + "2" * 64,
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
    }
    monkeypatch.setattr(
        release.tke_candidate,
        "build_candidate_preview",
        lambda **kwargs: calls.append(("preview", kwargs)) or preview,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "create_candidate",
        lambda **kwargs: calls.append(("create", kwargs)) or record,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "record_candidate",
        lambda **kwargs: calls.append(("record", kwargs)),
    )
    release._source_tree_sha = lambda repo, release_sha: TREE_SHA
    release.ReleaseOrchestrator._require_independent_worktree = lambda self: calls.append(("independent", {}))
    release.ReleaseOrchestrator._require_clean_commit = lambda self, expected_sha: calls.append(("clean", {"release_sha": expected_sha})) or expected_sha

    assert release.main(
        [
            "--state-dir",
            str(ROOT / ".candidate-state-test"),
            "tke-candidate",
            "--release-sha",
            SHA,
            "--schema-release",
            SCHEMA_RELEASE,
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 0

    assert [name for name, _kwargs in calls] == ["independent", "clean", "preview", "create", "record"]
    assert calls[-1][1]["record"] == record


def test_tke_activate_cli_does_not_accept_manual_image_or_schema_identity():
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_deploy_entry", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)

    parser = release.build_parser()
    parsed = parser.parse_args(
        [
            "tke-activate",
            "--release-sha",
            SHA,
            "--kube-context",
            "cls-demo0000-000000000000-context-default",
        ]
    )
    assert parsed.action == "tke-activate"
    assert not hasattr(parsed, "backend_image")
    assert not hasattr(parsed, "frontend_image")
    assert not hasattr(parsed, "schema_release")
    assert not hasattr(parsed, "artifact_manifest_sha256")


def test_tke_candidate_phase_cannot_run_from_a_dirty_or_primary_worktree(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_execute_worktree", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)
    calls = []

    class FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def _require_independent_worktree(self):
            calls.append("independent")
            raise release.ReleaseError("candidate requires an independent worktree")

    release.ReleaseOrchestrator = FakeOrchestrator
    monkeypatch.setattr(
        release.tke_candidate,
        "create_candidate",
        lambda **kwargs: calls.append("create"),
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-candidate",
            "--release-sha",
            SHA,
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 1
    assert calls == ["independent"]


def test_tke_activate_cli_activates_exact_rehearsed_candidate_slot(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_deploy_execute", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)
    calls = []
    record = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": "registry.example.test/aiops/backend@sha256:" + "1" * 64,
        "clusterPullVerified": False,
        "frontendImage": "registry.example.test/aiops/frontend@sha256:" + "2" * 64,
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
    }
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append(("read", kwargs)) or record,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_cluster_pull_rehearsal",
        lambda **kwargs: calls.append(("proof", kwargs)),
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_mysql_rehearsal",
        lambda **kwargs: calls.append(("mysql_proof", kwargs)),
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_candidate_deployment",
        lambda **kwargs: calls.append(("deployment_proof", kwargs)) or {"slot": "green"},
    )
    monkeypatch.setattr(
        release.tke_release,
        "prepare_deploy",
        lambda **kwargs: calls.append(("prepare", kwargs)) or {"status": "prepared"},
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_slot_activate",
        lambda **kwargs: calls.append(("activate", kwargs)) or {"status": "activated"},
    )
    release.ReleaseOrchestrator._require_independent_worktree = lambda self: calls.append(("independent", {}))
    release.ReleaseOrchestrator._require_clean_commit = lambda self, expected_sha: calls.append(("clean", {"release_sha": expected_sha})) or expected_sha

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-activate",
            "--release-sha",
            SHA,
            "--kube-context",
            "cls-demo0000-000000000000-context-default",
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 0

    assert [name for name, _kwargs in calls] == ["independent", "clean", "read", "proof", "mysql_proof", "deployment_proof", "prepare", "activate"]
    activate = calls[-1][1]
    assert activate["backend_image"] == record["backendImage"]
    assert activate["frontend_image"] == record["frontendImage"]
    assert activate["schema_release"] == SCHEMA_RELEASE
    assert activate["artifact_manifest_sha256"] == "3" * 64
    assert activate["slot"] == "green"


def test_tke_activate_rejects_a_dirty_or_primary_worktree_before_cluster_operations(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "test_tke_deploy_worktree_gate", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)
    calls = []

    class FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def _require_independent_worktree(self):
            calls.append("independent")
            raise release.ReleaseError("candidate requires an independent worktree")

    release.ReleaseOrchestrator = FakeOrchestrator
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append("read"),
    )
    monkeypatch.setattr(
        release.tke_release,
        "prepare_deploy",
        lambda **kwargs: calls.append("prepare"),
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-activate",
            "--release-sha",
            SHA,
            "--kube-context",
            "cls-demo0000-000000000000-context-default",
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 1
    assert calls == ["independent"]


def test_tke_pull_rehearsal_cli_records_proof_only_after_cluster_readback(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_pull_rehearsal", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)
    calls = []
    record = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": "registry.example.test/aiops/backend@sha256:" + "1" * 64,
        "clusterPullVerified": False,
        "frontendImage": "registry.example.test/aiops/frontend@sha256:" + "2" * 64,
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
    }
    evidence = {
        "status": "cluster_pull_verified",
        "releaseSha": SHA,
        "namespace": "aiops-demo",
        "kubeContext": "cls-demo0000-000000000000-context-default",
        "targetCluster": "cls-demo0000",
        "images": {"backend": record["backendImage"], "frontend": record["frontendImage"]},
    }
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append(("read", kwargs)) or record,
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_pull_rehearsal",
        lambda **kwargs: calls.append(("rehearse", kwargs)) or evidence,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "record_cluster_pull_rehearsal",
        lambda **kwargs: calls.append(("record", kwargs)),
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_independent_worktree",
        lambda self: calls.append(("independent", {})),
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_clean_commit",
        lambda self, expected_sha: calls.append(
            ("clean", {"release_sha": expected_sha})
        )
        or expected_sha,
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-rehearse-pull",
            "--release-sha",
            SHA,
            "--kube-context",
            "cls-demo0000-000000000000-context-default",
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 0

    assert [name for name, _kwargs in calls] == [
        "independent",
        "clean",
        "read",
        "rehearse",
        "record",
    ]
    assert calls[-1][1]["evidence"] == evidence


def test_tke_activate_cli_requires_cluster_pull_proof_before_apply(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_deploy_rehearsal", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)
    calls = []
    record = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": "registry.example.test/aiops/backend@sha256:" + "1" * 64,
        "clusterPullVerified": False,
        "frontendImage": "registry.example.test/aiops/frontend@sha256:" + "2" * 64,
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
    }
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append(("read", kwargs)) or record,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_cluster_pull_rehearsal",
        lambda **kwargs: (_ for _ in ()).throw(
            release.tke_candidate.TkeCandidateError(
                "TKE cluster image pull rehearsal is missing"
            )
        ),
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_deploy",
        lambda **kwargs: calls.append(("deploy", kwargs)) or {"status": "deployed"},
    )
    release.ReleaseOrchestrator._require_independent_worktree = lambda self: calls.append(("independent", {}))
    release.ReleaseOrchestrator._require_clean_commit = lambda self, expected_sha: calls.append(("clean", {"release_sha": expected_sha})) or expected_sha

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-activate",
            "--release-sha",
            SHA,
            "--kube-context",
            "cls-demo0000-000000000000-context-default",
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 1

    assert [name for name, _kwargs in calls] == ["independent", "clean", "read"]


def test_tke_candidate_rejects_dirty_or_primary_worktree_before_registry_write(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_worktree_gate", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)
    calls = []

    class FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def _require_independent_worktree(self):
            calls.append("independent")
            raise release.ReleaseError("candidate requires an independent worktree")

    release.ReleaseOrchestrator = FakeOrchestrator
    monkeypatch.setattr(
        release.tke_candidate,
        "create_candidate",
        lambda **kwargs: calls.append("create"),
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-candidate",
            "--release-sha",
            SHA,
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 1
    assert calls == ["independent"]


def test_tke_accept_cli_requires_a_recorded_active_slot(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_accept_execute", ROOT / "scripts/release/release.py"
    )
    release = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(release)
    calls = []
    record = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": "registry.example.test/aiops/backend@sha256:" + "1" * 64,
        "clusterPullVerified": False,
        "frontendImage": "registry.example.test/aiops/frontend@sha256:" + "2" * 64,
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
    }
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append(("read", kwargs)) or record,
    )
    monkeypatch.setattr(
        release.tke_release,
        "read_active_release",
        lambda **kwargs: (_ for _ in ()).throw(
            release.tke_release.TkeReleaseError(
                "recorded active TKE blue-green release target is missing"
            )
        ),
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_independent_worktree",
        lambda self: calls.append(("independent", {})),
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_clean_commit",
        lambda self, expected_sha: calls.append(
            ("clean", {"release_sha": expected_sha})
        )
        or expected_sha,
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-accept",
            "--release-sha",
            SHA,
            "--kube-context",
            "cls-demo0000-000000000000-context-default",
        ]
    ) == 1
    assert [name for name, _kwargs in calls] == ["independent", "clean"]
