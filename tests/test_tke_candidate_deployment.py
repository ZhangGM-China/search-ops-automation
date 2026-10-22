import hashlib
import importlib.util
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_MODULE_PATH = ROOT / "scripts" / "release" / "tke_candidate.py"
RELEASE_MODULE_PATH = ROOT / "scripts" / "release" / "release.py"

SHA = "a" * 40
TREE_SHA = "b" * 40
SCHEMA_RELEASE = "zha32"
CONTEXT = "cls-demo0000-000000000000-context-default"
BACKEND = "registry.example.test/aiops/backend@sha256:" + "1" * 64
FRONTEND = "registry.example.test/aiops/frontend@sha256:" + "2" * 64


def _load_candidate():
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_deployment", CANDIDATE_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_release():
    spec = importlib.util.spec_from_file_location(
        "test_tke_candidate_deployment_release", RELEASE_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _candidate_record(candidate):
    return {
        "artifactManifestSha256": candidate._artifact_manifest_sha256(
            release_sha=SHA,
            schema_release=SCHEMA_RELEASE,
            source_tree_sha=TREE_SHA,
        ),
        "backendImage": BACKEND,
        "clusterPullVerified": False,
        "frontendImage": FRONTEND,
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "sourceTreeSha": TREE_SHA,
    }


def _candidate_deployment_evidence(slot="green"):
    return {
        "status": "candidate_deployed",
        "releaseSha": SHA,
        "namespace": "aiops-demo",
        "kubeContext": CONTEXT,
        "targetCluster": "cls-demo0000",
        "manifestSha256": hashlib.sha256(b"candidate-render").hexdigest(),
        "slot": slot,
        "deployments": [f"backend-{slot}", f"frontend-{slot}", f"scheduler-{slot}"],
        "services": [f"api-{slot}", f"frontend-{slot}"],
        "schedulerMode": "standby",
    }


def test_candidate_deployment_proof_binds_candidate_images_context_and_standby_phase(tmp_path):
    candidate = _load_candidate()
    record = _candidate_record(candidate)
    evidence = _candidate_deployment_evidence()
    state_path = tmp_path / "tke-candidate-deployment-state.json"

    candidate.record_candidate_deployment(
        state_path=state_path,
        candidate=record,
        kube_context=CONTEXT,
        evidence=evidence,
    )

    proof = candidate.require_candidate_deployment(
        state_path=state_path,
        candidate=record,
        kube_context=CONTEXT,
    )
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert proof["slot"] == "green"

    wrong_context = CONTEXT + "-other"
    with pytest.raises(candidate.TkeCandidateError, match="candidate deployment"):
        candidate.require_candidate_deployment(
            state_path=state_path,
            candidate=record,
            kube_context=wrong_context,
        )


def test_release_cli_runs_and_records_mysql_rehearsal_before_candidate_deployment(
    tmp_path, monkeypatch
):
    release = _load_release()
    parser = release.build_parser()
    rehearsal = parser.parse_args(
        [
            "tke-rehearse-mysql",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
        ]
    )
    assert rehearsal.action == "tke-rehearse-mysql"
    assert not hasattr(rehearsal, "host")

    calls = []
    record = _candidate_record(release.tke_candidate)
    evidence = {
        "backendImage": record["backendImage"],
        "kubeContext": CONTEXT,
        "namespace": "aiops-demo",
        "readOnlyQuery": True,
        "releaseSha": SHA,
        "status": "mysql_rehearsal_verified",
        "targetCluster": "cls-demo0000",
    }
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append(("read", kwargs)) or record,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_cluster_pull_rehearsal",
        lambda **kwargs: calls.append(("pull_proof", kwargs)),
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_mysql_rehearsal",
        lambda **kwargs: calls.append(("mysql", kwargs)) or evidence,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "record_mysql_rehearsal",
        lambda **kwargs: calls.append(("record_mysql", kwargs)),
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
            "tke-rehearse-mysql",
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
        "read",
        "pull_proof",
        "mysql",
        "record_mysql",
    ]
    assert calls[-2][1]["backend_image"] == record["backendImage"]
    assert calls[-1][1]["evidence"] == evidence


def test_release_cli_makes_standby_rehearsal_a_required_phase_before_activation(
    tmp_path, monkeypatch
):
    release = _load_release()
    parser = release.build_parser()
    rehearsal = parser.parse_args(
        [
            "tke-rehearse-deploy",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
        ]
    )
    activation = parser.parse_args(
        [
            "tke-activate",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
        ]
    )
    assert not hasattr(rehearsal, "host")
    assert not hasattr(activation, "identity_file")

    calls = []
    record = _candidate_record(release.tke_candidate)
    evidence = _candidate_deployment_evidence()
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append(("read", kwargs)) or record,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_cluster_pull_rehearsal",
        lambda **kwargs: calls.append(("pull_proof", kwargs)),
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_mysql_rehearsal",
        lambda **kwargs: calls.append(("mysql_proof", kwargs)),
    )
    monkeypatch.setattr(
        release.tke_release,
        "prepare_deploy",
        lambda **kwargs: calls.append(("prepare", kwargs)) or {"status": "prepared"},
    )
    monkeypatch.setattr(
        release.tke_release,
        "sync_runtime_config_schema",
        lambda **kwargs: calls.append(("runtime_config_sync", kwargs))
        or {"status": "runtime_config_synced"},
    )
    monkeypatch.setattr(
        release.tke_release,
        "require_candidate_slot_safe",
        lambda **kwargs: calls.append(("slot_select", kwargs)) or "green",
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_slot_deploy",
        lambda **kwargs: calls.append(("slot_deploy", kwargs)) or evidence,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "record_candidate_deployment",
        lambda **kwargs: calls.append(("record_rehearsal", kwargs)),
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_candidate_deployment",
        lambda **kwargs: calls.append(("rehearsal_proof", kwargs)) or {"slot": "green"},
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_slot_activate",
        lambda **kwargs: calls.append(("slot_activate", kwargs))
        or {"status": "activated", "releaseSha": SHA},
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
            "tke-rehearse-deploy",
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
        "read",
        "pull_proof",
        "mysql_proof",
        "slot_select",
        "prepare",
        "runtime_config_sync",
        "slot_deploy",
        "record_rehearsal",
    ]
    assert calls[-3][1]["template_path"] == ROOT / "config.yaml"
    assert calls[-3][1]["execute"] is True
    assert calls[-3][1]["confirm_release_sha"] == SHA
    assert calls[-2][1]["slot"] == "green"
    assert calls[-2][1]["scheduler_mode"] == "standby"
    assert calls[-2][1]["include_public_entrypoints"] is False

    calls.clear()
    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-activate",
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
        "read",
        "pull_proof",
        "mysql_proof",
        "rehearsal_proof",
        "prepare",
        "slot_activate",
    ]
    assert calls[-1][1]["slot"] == "green"


def test_rehearse_deploy_failure_prints_explicit_failed_slot_cleanup_command(
    tmp_path, monkeypatch, capsys
):
    release = _load_release()
    record = _candidate_record(release.tke_candidate)

    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_independent_worktree",
        lambda self: None,
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_clean_commit",
        lambda self, expected_sha: expected_sha,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **_kwargs: record,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_cluster_pull_rehearsal",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_mysql_rehearsal",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        release.tke_release,
        "prepare_deploy",
        lambda **_kwargs: {"status": "prepared"},
    )
    monkeypatch.setattr(
        release.tke_release,
        "sync_runtime_config_schema",
        lambda **_kwargs: {"status": "runtime_config_synced"},
    )
    monkeypatch.setattr(
        release.tke_release,
        "require_candidate_slot_safe",
        lambda **_kwargs: "green",
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_slot_deploy",
        lambda **_kwargs: (_ for _ in ()).throw(
            release.tke_release.TkeReleaseError("blue-green rollout failed")
        ),
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-rehearse-deploy",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 1

    stderr = capsys.readouterr().err
    assert "tke-cleanup-candidate" in stderr
    assert SHA in stderr
    assert "--slot green" in stderr
    assert f"--confirm-release-sha {SHA}" in stderr


def test_tke_activate_requires_standby_rehearsal(
    tmp_path, monkeypatch
):
    release = _load_release()
    calls = []
    record = _candidate_record(release.tke_candidate)
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append(("read", kwargs)) or record,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_cluster_pull_rehearsal",
        lambda **kwargs: calls.append(("pull_proof", kwargs)),
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_mysql_rehearsal",
        lambda **kwargs: calls.append(("mysql_proof", kwargs)),
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_candidate_deployment",
        lambda **kwargs: (_ for _ in ()).throw(
            release.tke_candidate.TkeCandidateError(
                "TKE candidate deployment rehearsal is missing"
            )
        ),
    )
    monkeypatch.setattr(
        release.tke_release,
        "prepare_deploy",
        lambda **kwargs: calls.append(("prepare", kwargs)),
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
            "tke-activate",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 1
    assert [name for name, _kwargs in calls] == [
        "independent",
        "clean",
        "read",
        "pull_proof",
        "mysql_proof",
    ]


def test_standby_candidate_render_never_creates_customer_ingress():
    release = _load_release()
    tke = release.tke_release

    def runner(argv, **kwargs):
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

    rendered = tke.render_candidate_manifests(
        manifests_dir=ROOT / "deploy/kubernetes/base",
        release_sha=SHA,
        backend_image=BACKEND,
        frontend_image=FRONTEND,
        schema_release=SCHEMA_RELEASE,
        artifact_manifest_sha256="3" * 64,
        runner=runner,
    )

    kinds = {
        item["kind"]
        for item in tke.yaml.safe_load_all(rendered)
        if item is not None
    }
    assert "Ingress" not in kinds
    assert {"Deployment", "Service", "NetworkPolicy"}.issubset(kinds)


def test_cleanup_cli_allows_only_explicit_legacy_failed_candidate_scope(tmp_path, monkeypatch):
    release = _load_release()
    calls = []

    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_independent_worktree",
        lambda self: calls.append(("independent", {})),
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_clean_commit",
        lambda self, expected_sha=None: calls.append(
            ("clean", {"release_sha": expected_sha})
        ),
    )
    monkeypatch.setattr(
        release.tke_release,
        "cleanup_failed_candidate",
        lambda **kwargs: calls.append(("cleanup", kwargs))
        or {"status": "candidate_cleaned", "releaseSha": SHA},
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-cleanup-candidate",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
            "--legacy-single-slot",
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 0
    assert [name for name, _kwargs in calls] == ["independent", "clean", "cleanup"]
    assert calls[1][1] == {"release_sha": None}
    assert calls[2][1]["legacy_single_slot"] is True
    assert calls[2][1]["confirm_release_sha"] == SHA


def test_cleanup_cli_binds_blue_green_cleanup_to_an_explicit_inactive_slot(tmp_path, monkeypatch):
    release = _load_release()
    calls = []

    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_independent_worktree",
        lambda self: calls.append(("independent", {})),
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_clean_commit",
        lambda self, expected_sha=None: calls.append(
            ("clean", {"release_sha": expected_sha})
        ),
    )
    monkeypatch.setattr(
        release.tke_release,
        "cleanup_failed_candidate",
        lambda **kwargs: calls.append(("cleanup", kwargs))
        or {"status": "candidate_cleaned", "releaseSha": SHA},
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-cleanup-candidate",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
            "--slot",
            "green",
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 0

    assert [name for name, _kwargs in calls] == ["independent", "clean", "cleanup"]
    assert calls[2][1]["legacy_single_slot"] is False
    assert calls[2][1]["slot"] == "green"
    assert calls[2][1]["state_path"] == tmp_path / "tke-release-state.json"


def test_rollback_cli_uses_the_recorded_blue_green_target(tmp_path, monkeypatch):
    release = _load_release()
    calls = []

    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_independent_worktree",
        lambda self: calls.append(("independent", {})),
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_clean_commit",
        lambda self, expected_sha: calls.append(("clean", {"release_sha": expected_sha})) or expected_sha,
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_slot_rollback",
        lambda **kwargs: calls.append(("rollback", kwargs))
        or {"status": "rolled_back", "releaseSha": SHA},
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-rollback",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
            "--execute",
            "--confirm-release-sha",
            SHA,
        ]
    ) == 0
    assert [name for name, _kwargs in calls] == ["independent", "clean", "rollback"]
    assert calls[-1][1]["release_sha"] == SHA
    assert calls[-1][1]["state_path"] == tmp_path / "tke-release-state.json"


def test_accept_cli_reads_the_recorded_active_blue_green_slot(tmp_path, monkeypatch):
    release = _load_release()
    calls = []
    active = {
        "artifactManifestSha256": "3" * 64,
        "backendImage": BACKEND,
        "frontendImage": FRONTEND,
        "manifestSha256": "4" * 64,
        "releaseSha": SHA,
        "schemaRelease": SCHEMA_RELEASE,
        "slot": "green",
    }
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_independent_worktree",
        lambda self: calls.append(("independent", {})),
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_clean_commit",
        lambda self, expected_sha: calls.append(("clean", {"release_sha": expected_sha})) or expected_sha,
    )
    monkeypatch.setattr(
        release.tke_release,
        "read_active_release",
        lambda **kwargs: calls.append(("active", kwargs)) or active,
    )
    monkeypatch.setattr(
        release.tke_release,
        "accept_slot_deploy",
        lambda **kwargs: calls.append(("accept", kwargs))
        or {"status": "accepted", "releaseSha": SHA},
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-accept",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
        ]
    ) == 0
    assert [name for name, _kwargs in calls] == ["independent", "clean", "active", "accept"]
    assert calls[-1][1]["slot"] == "green"
    assert calls[-1][1]["expected_scheduler_mode"] == "active"
    assert calls[-1][1]["include_public_entrypoints"] is True


def test_rehearse_accept_cli_revalidates_the_recorded_standby_slot_without_deploying(
    tmp_path, monkeypatch
):
    release = _load_release()
    parser = release.build_parser()
    parsed = parser.parse_args(
        [
            "tke-rehearse-accept",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
        ]
    )
    assert parsed.action == "tke-rehearse-accept"
    assert not hasattr(parsed, "execute")

    calls = []
    record = _candidate_record(release.tke_candidate)
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_independent_worktree",
        lambda self: calls.append(("independent", {})),
    )
    monkeypatch.setattr(
        release.ReleaseOrchestrator,
        "_require_clean_commit",
        lambda self, expected_sha=None: calls.append(("clean", {"release_sha": expected_sha}))
        or expected_sha,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append(("candidate", kwargs)) or record,
    )
    monkeypatch.setattr(
        release.tke_candidate,
        "require_candidate_deployment",
        lambda **kwargs: calls.append(("deployment_proof", kwargs)) or {"slot": "green"},
    )
    monkeypatch.setattr(
        release.tke_release,
        "accept_slot_deploy",
        lambda **kwargs: calls.append(("accept", kwargs))
        or {"status": "accepted", "releaseSha": SHA},
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_slot_deploy",
        lambda **_kwargs: pytest.fail("standby re-acceptance must not deploy"),
    )

    assert release.main(
        [
            "--state-dir",
            str(tmp_path),
            "tke-rehearse-accept",
            "--release-sha",
            SHA,
            "--kube-context",
            CONTEXT,
        ]
    ) == 0
    assert [name for name, _kwargs in calls] == [
        "independent",
        "clean",
        "candidate",
        "deployment_proof",
        "accept",
    ]
    assert calls[1][1] == {"release_sha": None}
    assert calls[-1][1]["slot"] == "green"
    assert calls[-1][1]["expected_scheduler_mode"] == "standby"
    assert calls[-1][1]["include_public_entrypoints"] is False


def test_prepare_reads_secret_keys_without_deserializing_secret_values():
    release = _load_release()
    tke = release.tke_release
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[3:6] == ["config", "view", "--minify"]:
            return tke.CommandResult(
                0,
                (
                    '{"clusters":[{"name":"cls-demo0000"}],'
                    '"contexts":[{"name":"'
                    + CONTEXT
                    + '","context":{"cluster":"cls-demo0000"}}]}'
                ).encode(),
                b"",
            )
        if argv[3:5] == ["auth", "can-i"]:
            return tke.CommandResult(0, b"yes\n", b"")
        if argv[3:6] == ["get", "namespace", "aiops-demo"]:
            return tke.CommandResult(0, b'{"status":{"phase":"Active"}}', b"")
        if argv[3:6] == ["get", "configmap", "aiops-runtime-config"]:
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime-config"},"data":{"config.yaml":"safety_config: {}\\n"}}',
                b"",
            )
        if argv[3:5] == ["get", "secret"] and argv[5] == "aiops-runtime-secrets":
            if any(item.startswith("--template=") for item in argv):
                return tke.CommandResult(
                    0,
                    b"aiops-runtime-secrets\nAUTO_DB_URI_PROD\nKG_AGENT_MCP_TOKEN\nPI_AGENT_INTERNAL_TOKEN\nLANGFUSE_PUBLIC_KEY\nLANGFUSE_SECRET_KEY\nSECRET_KEY\n",
                    b"",
                )
            return tke.CommandResult(
                0,
                b'{"metadata":{"name":"aiops-runtime-secrets"},"data":{"AUTO_DB_URI_PROD":"","SECRET_KEY":""}}',
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
        return tke.CommandResult(0, b"{}", b"")

    prepared = tke.prepare_deploy(
        namespace="aiops-demo",
        release_sha=SHA,
        kube_context=CONTEXT,
        runner=runner,
    )

    secret_calls = [call for call in calls if "secret" in call]
    assert secret_calls, calls
    secret_call = secret_calls[0]
    assert any(item.startswith("--template=") for item in secret_call)
    assert prepared["secretKeys"] == ["AUTO_DB_URI_PROD", "KG_AGENT_MCP_TOKEN", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "PI_AGENT_INTERNAL_TOKEN", "SECRET_KEY"]


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        (
            "tke-rehearse-pull",
            [
                "--release-sha",
                SHA,
                "--kube-context",
                CONTEXT,
                "--execute",
                "--confirm-release-sha",
                SHA,
            ],
        ),
        (
            "tke-rollback",
            [
                "--release-sha",
                SHA,
                "--kube-context",
                CONTEXT,
                "--execute",
                "--confirm-release-sha",
                SHA,
            ],
        ),
        (
            "tke-accept",
            [
                "--release-sha",
                SHA,
                "--kube-context",
                CONTEXT,
            ],
        ),
    ],
)
def test_tke_mutation_commands_fail_before_cluster_access_outside_clean_independent_worktree(
    tmp_path, monkeypatch, action, arguments
):
    release = _load_release()
    calls = []

    class FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def _require_independent_worktree(self):
            calls.append("independent")
            raise release.ReleaseError("TKE mutation requires an independent worktree")

    monkeypatch.setattr(release, "ReleaseOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        release.tke_candidate,
        "read_candidate",
        lambda **kwargs: calls.append("candidate"),
    )
    monkeypatch.setattr(
        release.tke_release,
        "execute_rollback",
        lambda **kwargs: calls.append("rollback"),
    )

    assert release.main(
        ["--state-dir", str(tmp_path), action, *arguments]
    ) == 1
    assert calls == ["independent"]
