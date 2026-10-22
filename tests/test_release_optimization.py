import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_local_release_preflight_fails_before_expensive_checks_when_config_is_missing(tmp_path):
    preflight = _load(
        "test_release_preflight_missing_config",
        ROOT / "scripts/release/release_preflight.py",
    )

    (tmp_path / "frontend/node_modules/.bin").mkdir(parents=True)
    for name in ("vitest", "eslint", "playwright"):
        (tmp_path / "frontend/node_modules/.bin" / name).touch()

    with pytest.raises(preflight.ReleasePreflightError, match="config.yaml"):
        preflight.check_local(repo=tmp_path, release_sha="a" * 40)


def test_local_release_preflight_reports_only_safe_metadata(tmp_path):
    preflight = _load(
        "test_release_preflight_safe_metadata",
        ROOT / "scripts/release/release_preflight.py",
    )

    (tmp_path / "frontend/node_modules/.bin").mkdir(parents=True)
    for name in ("vitest", "eslint", "playwright"):
        (tmp_path / "frontend/node_modules/.bin" / name).touch()
    (tmp_path / "config.yaml").write_text(
        "database:\n  password: should-never-be-output\nfeature_flag: true\n",
        encoding="utf-8",
    )

    result = preflight.check_local(repo=tmp_path, release_sha="a" * 40)

    assert result["status"] == "local_preflight_passed"
    assert result["releaseSha"] == "a" * 40
    assert result["configTopLevelFieldCount"] == 2
    assert "password" not in json.dumps(result).lower()
    assert "should-never-be-output" not in json.dumps(result)


def test_stage_timing_appends_safe_elapsed_record(tmp_path):
    timing = _load(
        "test_release_timing",
        ROOT / "scripts/release/release_timing.py",
    )

    timing.record_stage(
        state_dir=tmp_path,
        stage="tke-preflight",
        release_sha="a" * 40,
        status="success",
        started_at="2026-09-20T01:00:00+00:00",
        finished_at="2026-09-20T01:00:02+00:00",
        elapsed_seconds=2.0,
    )
    timing.record_stage(
        state_dir=tmp_path,
        stage="tke-candidate",
        release_sha="a" * 40,
        status="failed",
        started_at="2026-09-20T01:00:02+00:00",
        finished_at="2026-09-20T01:00:03+00:00",
        elapsed_seconds=1.0,
        failure_category="preflight",
    )

    payload = json.loads((tmp_path / "tke-stage-timings.json").read_text())
    assert payload["version"] == 1
    assert [item["stage"] for item in payload["stages"]] == [
        "tke-preflight",
        "tke-candidate",
    ]
    assert payload["stages"][1]["failureCategory"] == "preflight"
    assert payload["stages"][0]["elapsedSeconds"] == 2.0


def test_release_cli_exposes_preflight_and_pipeline_with_safe_default():
    release = _load("test_release_optimization_cli", ROOT / "scripts/release/release.py")
    parser = release.build_parser()

    preflight = parser.parse_args(
        [
            "tke-preflight",
            "--release-sha",
            "a" * 40,
            "--kube-context",
            "cls-demo0000-000000000000-context-default",
        ]
    )
    pipeline = parser.parse_args(
        [
            "tke-pipeline",
            "--release-sha",
            "a" * 40,
            "--kube-context",
            "cls-demo0000-000000000000-context-default",
            "--execute",
            "--confirm-release-sha",
            "a" * 40,
        ]
    )

    assert preflight.action == "tke-preflight"
    assert pipeline.action == "tke-pipeline"
    assert pipeline.through == "standby"
    assert pipeline.schema_release == "zha33"

    candidate = parser.parse_args(
        ["tke-candidate", "--release-sha", "a" * 40]
    )
    assert candidate.schema_release == "zha33"
    assert release.pipeline_stages("standby")[-1] == "tke-rehearse-accept"
    assert release.pipeline_stages("activate")[-1] == "tke-accept"


def test_pipeline_stops_at_first_failed_stage_and_never_continues(monkeypatch, tmp_path):
    release = _load("test_release_pipeline_runner", ROOT / "scripts/release/release.py")
    commands = []

    class Completed:
        def __init__(self, returncode):
            self.returncode = returncode

    def run(command, check=False):
        assert check is False
        commands.append(command)
        action = command[command.index("--format") + 2]
        return Completed(1 if action == "tke-rehearse-mysql" else 0)

    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(release.ReleaseError, match="tke-rehearse-mysql"):
        release._run_pipeline(
            repo=ROOT,
            state_dir=tmp_path,
            release_sha="a" * 40,
            schema_release="zha32",
            kube_context="cls-demo0000-000000000000-context-default",
            through="activate",
            execute=True,
            confirm_release_sha="a" * 40,
        )

    actions = [command[command.index("--format") + 2] for command in commands]
    assert actions == [
        "tke-preflight",
        "tke-candidate",
        "tke-prepare",
        "tke-rehearse-pull",
        "tke-rehearse-mysql",
    ]
