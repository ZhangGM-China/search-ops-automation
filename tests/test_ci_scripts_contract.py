import ast
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CI_DIR = PROJECT_ROOT / "scripts" / "ci"
DEV_DIR = PROJECT_ROOT / "scripts" / "dev"
FRONTEND_PACKAGE = PROJECT_ROOT / "frontend" / "package.json"
REQUIREMENTS_TXT = PROJECT_ROOT / "requirements.txt"
PLAYWRIGHT_CONFIG = PROJECT_ROOT / "frontend" / "playwright.config.ts"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
FRONTEND_DOCKERIGNORE = PROJECT_ROOT / "frontend" / ".dockerignore"
FRONTEND_DOCKERFILE = PROJECT_ROOT / "frontend" / "Dockerfile"
TRACKED_CREDENTIAL_SCAN = PROJECT_ROOT / "scripts" / "ci" / "check_tracked_credentials.py"
RUNTIME_BASE_IMAGE_GATE = PROJECT_ROOT / "scripts" / "ci" / "check_runtime_base_image.py"
RUNTIME_BASE_IMAGE_POLICY = PROJECT_ROOT / "scripts" / "ci" / "runtime_base_image_policy.json"
TKE_CANDIDATE = PROJECT_ROOT / "scripts" / "release" / "tke_candidate.py"
PRODUCTION_DEPLOYMENT_GUIDE = PROJECT_ROOT / "docs" / "production_deployment_guide.md"
PLANE_CICD = PROJECT_ROOT / ".plane" / "cicd.yaml"
LOCAL_COMPOSE = PROJECT_ROOT / "docker-compose.yml"
XIAOHONGSHU_MCP_IMAGE = (
    "registry.example.test/"
    "xpzouying/xiaohongshu-mcp@sha256:"
    "39bb7d0196ad76dd9c1da60630acd904ca472df1a3be42a5ffad8a66420d5bc6"
)
BACKEND_COPY_INSTRUCTIONS = (
    "COPY requirements.lock /app/requirements.lock",
    "COPY pi-runtime/package.json pi-runtime/package-lock.json /app/pi-runtime/",
    "COPY pi-runtime/src /app/pi-runtime/src",
    "COPY main.py /app/main.py",
    "COPY src /app/src",
    "COPY scripts/db/migrate_prod_202607_assistant_top_query.py /app/scripts/db/migrate_prod_202607_assistant_top_query.py",
)
FRONTEND_COPY_INSTRUCTIONS = (
    "COPY package.json package-lock.json ./",
    "COPY tsconfig.json tsconfig.app.json tsconfig.node.json vite.config.ts index.html ./",
    "COPY src ./src",
    "COPY public ./public",
    "COPY nginx.conf.template /etc/aiops-nginx/default.conf.template",
    "COPY nginx-entrypoint.sh /usr/local/bin/aiops-nginx-entrypoint",
    "COPY --from=build /app/dist /usr/share/nginx/html",
    "COPY --from=build /app/release-info.json /usr/share/nginx/html/release-info.json",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_python_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backend_image_uses_base_image_timezone_without_package_network():
    dockerfile = _read(DOCKERFILE)

    assert "mirrors.aliyun.com" not in dockerfile
    assert "apt-get" not in dockerfile
    assert "test -e /usr/share/zoneinfo/$TZ" in dockerfile


def test_backend_image_does_not_include_retired_festival_runtime():
    dockerfile = _read(DOCKERFILE)
    requirements = _read(PROJECT_ROOT / "requirements.runtime.in")

    assert "uv tool install" not in dockerfile
    assert "uvx" not in dockerfile
    assert "china-festival-mcp" not in requirements
    assert "fastmcp" not in requirements
    assert "mcp==" not in requirements


def test_tke_candidate_requires_audited_runtime_base_image_gate_before_build():
    assert RUNTIME_BASE_IMAGE_GATE.is_file()
    assert RUNTIME_BASE_IMAGE_POLICY.is_file()

    candidate = _read(TKE_CANDIDATE)
    create_candidate = candidate[candidate.index("def create_candidate(") :]
    assert "check_runtime_base_image.py" in candidate
    assert create_candidate.index("_run_runtime_base_image_gate(") < create_candidate.index(
        'for component in ("backend", "frontend"):'
    )


def test_release_dockerfiles_pin_audited_frontend_and_base_image_indexes():
    dockerfile = _read(DOCKERFILE)
    frontend_dockerfile = _read(FRONTEND_DOCKERFILE)
    dockerfile_frontend = (
        "docker/dockerfile:1@sha256:"
        "87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89"
    )
    python_base = (
        "python:3.11-alpine@sha256:"
        "cd04730b8511def3fbf14204d66a0c1536f290b8e896ed5a94cd64cb15ac1356"
    )
    node_base = (
        "node:20-alpine@sha256:"
        "fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293"
    )
    nginx_base = (
        "nginx:alpine@sha256:"
        "62ff2089abf5a9ed33bd232895bef5e22f7bb4b200675cec49a5ebc48e3d4ac8"
    )

    assert dockerfile.splitlines()[0] == f"# syntax={dockerfile_frontend}"
    assert frontend_dockerfile.splitlines()[0] == f"# syntax={dockerfile_frontend}"
    assert f"FROM {python_base}" in dockerfile
    assert f"FROM {node_base} AS build" in frontend_dockerfile
    assert f"FROM {nginx_base}" in frontend_dockerfile


@pytest.mark.parametrize(
    ("base_image", "expected_error"),
    [
        ("python:3.11-slim", "must be digest-pinned"),
        (
            "python:3.11-slim@sha256:"
            "0a772f6e6aad889b7fc3af194db750e110df7743638e2faf6c40fe754e0be28c",
            "CVE-2026-12087",
        ),
        (
            "python:3.11-slim-bookworm@sha256:"
            "77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15",
            "CVE-2026-13221",
        ),
        ("python:3.11-alpine@sha256:" + "e" * 64, "not approved"),
        (
            "evil.example/other@sha256:" + "c" * 64,
            "official Python 3.11 runtime",
        ),
        ("python:3.11-slim@sha256:" + "a" * 64, "not approved"),
    ],
)
def test_runtime_base_image_gate_fails_closed_for_unapproved_or_vulnerable_images(
    tmp_path, base_image, expected_error
):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM {base_image}\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(RUNTIME_BASE_IMAGE_GATE),
            "--dockerfile",
            str(dockerfile),
            "--policy",
            str(RUNTIME_BASE_IMAGE_POLICY),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert expected_error in result.stderr


def test_runtime_base_image_gate_accepts_only_a_policy_approved_digest(tmp_path):
    image = "python:3.11-alpine@sha256:" + "b" * 64
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM {image}\n", encoding="utf-8")
    policy = tmp_path / "runtime-base-policy.json"
    policy.write_text(
        json.dumps({"version": 1, "approved": [image], "blocked": {}}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(RUNTIME_BASE_IMAGE_GATE),
            "--dockerfile",
            str(dockerfile),
            "--policy",
            str(policy),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""


def test_runtime_base_image_gate_rejects_an_approved_non_official_image(tmp_path):
    image = "evil.example/other@sha256:" + "d" * 64
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM {image}\n", encoding="utf-8")
    policy = tmp_path / "runtime-base-policy.json"
    policy.write_text(
        json.dumps({"version": 1, "approved": [image], "blocked": {}}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(RUNTIME_BASE_IMAGE_GATE),
            "--dockerfile",
            str(dockerfile),
            "--policy",
            str(policy),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "official Python 3.11 runtime" in result.stderr


def test_frontend_image_uses_bounded_configurable_npm_registry():
    dockerfile = _read(FRONTEND_DOCKERFILE)

    assert "ARG NPM_REGISTRY=https://registry.npmmirror.com/" in dockerfile
    assert 'npm ci --registry="$NPM_REGISTRY"' in dockerfile
    assert "--fetch-timeout=30000 --fetch-retries=1" in dockerfile


def test_frontend_image_reuses_dependency_layer_across_release_shas():
    dockerfile = _read(FRONTEND_DOCKERFILE)
    dependency_install = dockerfile.index('npm ci --registry="$NPM_REGISTRY"')

    assert dockerfile.index("ARG NPM_REGISTRY=https://registry.npmmirror.com/") < dependency_install
    assert dependency_install < dockerfile.index("ARG RELEASE_SHA")
    assert dependency_install < dockerfile.index("ARG SCHEMA_RELEASE")
    assert dependency_install < dockerfile.index("ARG ARTIFACT_MANIFEST_SHA256")


def test_frontend_image_persists_npm_cache_without_audit_roundtrips():
    dockerfile = _read(FRONTEND_DOCKERFILE)

    assert "RUN --mount=type=cache,target=/root/.npm \\" in dockerfile
    assert "--prefer-offline --no-audit --no-fund" in dockerfile


def test_backend_image_reuses_dependency_layer_across_release_shas():
    dockerfile = _read(DOCKERFILE)
    dependency_install = dockerfile.index("pip install --require-hashes")

    assert dockerfile.index("COPY requirements.lock /app/requirements.lock") < dependency_install
    assert dependency_install < dockerfile.index("ARG RELEASE_SHA")
    assert dependency_install < dockerfile.index("ARG SCHEMA_RELEASE")
    assert dependency_install < dockerfile.index("ARG ARTIFACT_MANIFEST_SHA256")
    assert "RUN --mount=type=cache,target=/app/data/cache/pip \\" in dockerfile
    assert "--no-cache-dir" not in dockerfile


def test_backend_image_uses_bounded_pip_download_recovery():
    dockerfile = _read(DOCKERFILE)

    assert "ARG PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple" in dockerfile
    assert "ARG PIP_RETRIES=5" in dockerfile
    assert "ARG PIP_TIMEOUT=120" in dockerfile
    assert '--retries "$PIP_RETRIES"' in dockerfile
    assert '--timeout "$PIP_TIMEOUT"' in dockerfile
    assert '--index-url "$PIP_INDEX_URL"' in dockerfile


def _load_compose(path: Path) -> dict:
    return yaml.safe_load(_read(path))


def _assert_xiaohongshu_mcp_runtime(service: dict, *, data_volume: str) -> None:
    assert service["container_name"] == "search-ops-xiaohongshu-mcp"
    assert service["platform"] == "linux/amd64"
    assert service["restart"] == "unless-stopped"
    assert service["init"] is True
    assert service["volumes"] == [data_volume]
    assert all("images" not in volume.lower() for volume in service["volumes"])
    assert service["environment"] == {
        "ROD_BROWSER_BIN": "/usr/local/bin/cloak-chromium",
        "COOKIES_PATH": "/app/data/cookies.json",
        "HOME": "/app/data/home",
        "XDG_CACHE_HOME": "/app/data/cache",
        "XDG_CONFIG_HOME": "/app/data/config",
    }
    assert service["ports"] == ["127.0.0.1:18060:18060"]
    assert service["healthcheck"] == {
        "test": ["CMD-SHELL", "curl -fsS http://127.0.0.1:18060/health"],
        "interval": "30s",
        "timeout": "5s",
        "retries": 5,
        "start_period": "20s",
    }
    assert service["logging"] == {
        "driver": "json-file",
        "options": {"max-size": "10m", "max-file": "3"},
    }


def test_local_compose_xiaohongshu_mcp_sidecar_is_persistent_and_private():
    compose = _load_compose(LOCAL_COMPOSE)
    services = compose["services"]
    sidecar = services["xiaohongshu-mcp"]

    assert sidecar["image"] == XIAOHONGSHU_MCP_IMAGE
    _assert_xiaohongshu_mcp_runtime(
        sidecar,
        data_volume="./data/xiaohongshu-mcp:/app/data",
    )
    for service_name in ("scheduler", "api"):
        assert services[service_name]["environment"]["XIAOHONGSHU_MCP_BASE_URL"] == (
            "http://xiaohongshu-mcp:18060"
        )
        assert services[service_name]["environment"]["XIAOHONGSHU_MCP_ENABLED"] == (
            "${XIAOHONGSHU_MCP_ENABLED:-true}"
        )


def test_assistant_module_parses_with_python_311_grammar():
    ast.parse(
        _read(PROJECT_ROOT / "src" / "assistant" / "service.py"),
        filename="src/assistant/service.py",
        feature_version=(3, 11),
    )



def _dockerignore_entries(path: Path) -> set[str]:
    return {
        line.strip()
        for line in _read(path).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _dockerfile_instructions(path: Path) -> list[str]:
    escape_character = "\\"
    instructions = []
    fragments = []
    for raw_line in _read(path).splitlines():
        stripped = raw_line.strip()
        if not fragments and stripped.startswith("#"):
            directive = re.fullmatch(r"#\s*escape\s*=\s*([\\`])", stripped, re.IGNORECASE)
            if directive:
                escape_character = directive.group(1)
            continue
        if not stripped or stripped.startswith("#"):
            continue

        trailing_escapes = len(stripped) - len(stripped.rstrip(escape_character))
        continued = trailing_escapes % 2 == 1
        fragments.append(stripped[:-1].rstrip() if continued else stripped)
        if not continued:
            instructions.append(" ".join(fragment for fragment in fragments if fragment))
            fragments = []

    if fragments:
        instructions.append(" ".join(fragment for fragment in fragments if fragment))
    return instructions


def _dockerfile_copy_instructions(path: Path) -> tuple[str, ...]:
    copies = []
    for instruction in _dockerfile_instructions(path):
        match = re.match(r"copy\b", instruction, re.IGNORECASE)
        if match:
            copies.append(f"COPY{instruction[match.end():]}")
    return tuple(copies)


def _dockerfile_local_copy_sources(path: Path) -> set[str]:
    sources = set()
    for instruction in _dockerfile_copy_instructions(path):
        payload = instruction[len("COPY") :].lstrip()
        flags = []
        while payload.startswith("--"):
            flag, separator, payload = payload.partition(" ")
            flags.append(flag)
            if not separator:
                payload = ""
                break
            payload = payload.lstrip()
        if any(flag.startswith("--from=") for flag in flags):
            continue
        tokens = json.loads(payload) if payload.startswith("[") else shlex.split(payload)
        sources.update(source.removeprefix("./") for source in tokens[:-1])
    return sources


def _assert_local_copy_roots_visible(rules: set[str], sources: set[str]) -> None:
    negations = sorted(rule for rule in rules if rule.startswith("!"))
    assert not negations, f"dockerignore negation rules are forbidden: {negations}"

    hidden_sources = set()
    for source in sources:
        normalized_source = source.removeprefix("./").rstrip("/")
        basename = PurePosixPath(normalized_source).name
        for rule in rules:
            normalized_rule = rule.lstrip("/").rstrip("/")
            if not normalized_rule:
                continue
            if any(character in normalized_rule for character in "*?["):
                matched = fnmatchcase(normalized_source, normalized_rule) or fnmatchcase(
                    basename, normalized_rule
                )
            else:
                matched = normalized_source == normalized_rule or normalized_source.startswith(
                    f"{normalized_rule}/"
                )
            if matched:
                hidden_sources.add(source)
                break

    assert not hidden_sources, f"COPY roots fully ignored: {sorted(hidden_sources)}"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_dockerfile_copy_parser_supports_real_instruction_forms(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        """FROM scratch
  copy --chown=1000:1000 src \\
      package.json /app/
CoPy --chmod=0644 ["config.json", "space name.txt", "/app/"]
COPY --from=build /app/dist /srv
""",
        encoding="utf-8",
    )

    assert _dockerfile_local_copy_sources(dockerfile) == {
        "src",
        "package.json",
        "config.json",
        "space name.txt",
    }


def test_copy_allowlist_detects_an_ignored_source_added_to_backend(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"{_read(DOCKERFILE)}\nCOPY tests /app/tests\n", encoding="utf-8")

    unexpected = set(_dockerfile_copy_instructions(dockerfile)) - set(BACKEND_COPY_INSTRUCTIONS)

    assert unexpected == {"COPY tests /app/tests"}


def test_backend_copy_roots_contract_rejects_hidden_sources(tmp_path):
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text("src/\nrequirements.txt\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="COPY roots fully ignored"):
        _assert_local_copy_roots_visible(
            _dockerignore_entries(dockerignore),
            _dockerfile_local_copy_sources(DOCKERFILE),
        )


def test_frontend_copy_roots_contract_rejects_hidden_src(tmp_path):
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text("src/\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="COPY roots fully ignored"):
        _assert_local_copy_roots_visible(
            _dockerignore_entries(dockerignore),
            _dockerfile_local_copy_sources(FRONTEND_DOCKERFILE),
        )


def test_docker_context_contract_rejects_readme_reinclude():
    with pytest.raises(AssertionError, match="negation rules are forbidden"):
        _assert_local_copy_roots_visible({"*.md", "!README.md"}, set())


def test_ci_scripts_exist_for_layered_test_entrypoints():
    required = [
        CI_DIR / "run_python_offline.sh",
        CI_DIR / "check_background_scheduler.sh",
        CI_DIR / "test_fast.sh",
        CI_DIR / "test_backend.sh",
        CI_DIR / "test_frontend.sh",
        CI_DIR / "test_e2e.sh",
        CI_DIR / "test_release_gate.sh",
        CI_DIR / "test_all.sh",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.exists()]
    assert not missing, f"missing layered ci scripts: {missing}"


def test_fast_gate_is_local_only_and_targets_contract_surfaces():
    script = _read(CI_DIR / "test_fast.sh")
    frontend_bin = "frontend/node_modules/.bin"
    assert "run_python_offline.sh" in script
    assert "test_ci_scripts_contract.py" in script
    assert "test_project_hygiene_contract.py" in script
    assert "src/test/architecture" in script
    for entrypoint in ("vitest", "eslint", "vite", "playwright"):
        assert f"{frontend_bin}/{entrypoint}" in script
    assert "npm ci --" not in script
    assert "curl " not in script


def test_backend_script_enforces_src_coverage_gate():
    script = _read(CI_DIR / "test_backend.sh")
    assert "scripts/ci/run_python_offline.sh" in script
    assert "-m flake8 . --select=F821" in script
    assert "--exclude=frontend,node_modules,.git,venv,.venv,src/top_query" in script
    assert "--cov=src/query_governance" in script
    assert "--cov=src/api/application/query_governance.py" in script
    assert "--cov=src/api/contracts.py" in script
    assert "--cov-report=term-missing" in script
    assert "--cov-fail-under=80" in script


def test_frontend_script_enforces_coverage_lint_and_build():
    script = _read(CI_DIR / "test_frontend.sh")
    assert "npm ci " not in script
    assert "Install locked frontend dependencies before testing." in script
    assert "npm run test:coverage" in script
    assert "npm run lint" in script
    assert "npm run build" in script
    assert "node_modules/.bin/playwright" not in script


def test_frontend_package_exposes_stable_test_entrypoints():
    package = json.loads(_read(FRONTEND_PACKAGE))
    scripts = package["scripts"]
    assert scripts["test"] == "vitest run"
    assert scripts["test:coverage"] == "vitest run --coverage"
    assert scripts["test:e2e"] == "playwright test"


def test_e2e_script_uses_dynamic_ports_and_isolated_runtime_artifacts():
    script = _read(CI_DIR / "test_e2e.sh")
    assert script.count("scripts/ci/run_python_offline.sh") >= 2
    assert 'BACKEND_PORT="${BACKEND_PORT:-$(_find_free_port)}"' in script
    assert 'FRONTEND_PORT="${FRONTEND_PORT:-$(_find_free_port)}"' in script
    assert 'BACKEND_PORT="${BACKEND_PORT:-8011}"' not in script
    assert 'FRONTEND_PORT="${FRONTEND_PORT:-5173}"' not in script
    assert "scripts/db/seed_test_db.py" in script
    assert '"$PROJECT_ROOT/frontend/node_modules/.bin/playwright" test' in script
    assert "npx playwright test" not in script
    assert "--strictPort" in script
    assert 'AUTO_DB_URI_TEST="sqlite:///' in script
    assert 'mktemp -d' in script
    assert 'E2E_ARTIFACT_ROOT="${E2E_ARTIFACT_ROOT:-' in script
    assert 'E2E_RUN_DIR="$E2E_ARTIFACT_ROOT/e2e-runtime-$E2E_RUN_ID"' in script
    assert 'E2E_ARTIFACT_DIR="${E2E_ARTIFACT_DIR:-' not in script
    assert "npm ci " not in script
    assert "Install locked frontend dependencies before testing." in script
    assert "/tmp/backend.log" not in script
    assert "/tmp/frontend.log" not in script


def test_e2e_cleanup_only_targets_processes_started_by_the_current_run():
    script = _read(CI_DIR / "test_e2e.sh")

    assert 'lsof -ti :"$BACKEND_PORT" | xargs kill' not in script
    assert 'lsof -ti :"$FRONTEND_PORT" | xargs kill' not in script
    assert "xargs kill" not in script
    assert 'cleanup_pid "${FRONTEND_PID:-}"' in script
    assert 'cleanup_pid "${BACKEND_PID:-}"' in script
    assert 'rm -rf -- "$E2E_DB_DIR"' in script
    assert '_is_owned_run_dir "$E2E_RUN_DIR"' in script
    assert 'rm -rf -- "$E2E_RUN_DIR"' in script
    assert 'rm -rf -- "$E2E_ARTIFACT_ROOT"' not in script
    assert 'trap cleanup EXIT' in script
    assert script.index('trap cleanup EXIT') < script.index(
        '_require_free_port "$BACKEND_PORT"'
    )
    assert "trap 'exit 130' INT" in script
    assert "trap 'exit 143' TERM" in script
    assert '"$PROJECT_ROOT/frontend/node_modules/.bin/vite"' in script


def test_e2e_listener_checks_require_the_current_process_ids():
    script = _read(CI_DIR / "test_e2e.sh")

    assert 'grep -qx "$BACKEND_PID"' in script
    assert 'grep -qx "$FRONTEND_PID"' in script
    assert 'kill -0 "$BACKEND_PID"' in script
    assert 'kill -0 "$FRONTEND_PID"' in script
    assert "backend process exited before readiness" in script
    assert "frontend process exited before readiness" in script


def test_test_all_delegates_to_layered_scripts_instead_of_inlining_everything():
    script = _read(CI_DIR / "test_all.sh")
    assert "scripts/ci/test_backend.sh" in script
    assert "scripts/ci/test_frontend.sh" in script
    assert "scripts/ci/test_e2e.sh" in script


def test_top_query_frontend_check_uses_current_feature_first_test_paths():
    script = _read(DEV_DIR / "run_query_governance_frontend_checks.sh")
    for path in (
        "src/features/query-governance/components/QueryGovernanceMetricTrend.test.tsx",
        "src/features/query-governance/components/BrandAssistantDrawer.test.tsx",
    ):
        assert path in script
    for retired_path in (
        "src/features/query-governance/pages/BrandOverview/index.test.tsx",
        "src/features/query-governance/pages/BrandDetail/index.test.tsx",
        "src/features/query-governance/components/QueryDetail/index.test.tsx",
        "e2e/top-query-worktree.spec.ts",
    ):
        assert retired_path not in script
    assert "./node_modules/.bin/vitest" in script
    assert "npx vitest" not in script
    assert "playwright" not in script


def test_release_gate_runs_automation_and_safe_verify_scripts():
    script = _read(CI_DIR / "test_release_gate.sh")
    assert "scripts/ci/check_background_scheduler.sh" in script
    assert script.count("scripts/ci/run_python_offline.sh") >= 2
    assert "scripts/ci/test_all.sh" in script
    assert "verify_runtime_watchdog.py" in script
    assert "verify_business_http_governor.py" in script
    assert "verify_stock_module.py" not in script
    assert "verify_top_query_" not in script


def test_offline_runner_installs_sitecustomize_and_rejects_opt_out_by_default():
    script = _read(CI_DIR / "run_python_offline.sh")
    assert "tests/offline_runtime" in script
    assert 'AIOPS_OFFLINE_TEST="1"' in script
    assert 'exec "$PYTHON_BIN" "$@"' in script


def test_scheduler_check_is_read_only_and_never_kills_processes():
    script = _read(CI_DIR / "check_background_scheduler.sh")
    assert "main.py" in script
    assert "WARNING" in script
    assert "kill" not in script


def _run_scheduler_check_harness(tmp_path, *, allow_override=False):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "pgrep", "#!/bin/sh\necho 4242\n")
    _write_executable(
        fake_bin / "lsof",
        "#!/bin/sh\nprintf 'p4242\\nfcwd\\nn%s\\n' \"$FAKE_SCHEDULER_CWD\"\n",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_SCHEDULER_CWD": str(PROJECT_ROOT),
    }
    if allow_override:
        env["AIOPS_RELEASE_GATE_ALLOW_BACKGROUND_SCHEDULER"] = "1"
    return subprocess.run(
        ["/bin/bash", str(CI_DIR / "check_background_scheduler.sh")],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_scheduler_check_fails_closed_with_pid_cwd_and_risk(tmp_path):
    result = _run_scheduler_check_harness(tmp_path)

    assert result.returncode != 0
    assert "PID: 4242" in result.stdout
    assert f"cwd: {PROJECT_ROOT}" in result.stdout
    assert "risk:" in result.stdout


def test_scheduler_check_override_is_explicitly_non_closed(tmp_path):
    result = _run_scheduler_check_harness(tmp_path, allow_override=True)

    assert result.returncode == 0
    assert "PID: 4242" in result.stdout
    assert f"cwd: {PROJECT_ROOT}" in result.stdout
    assert "risk:" in result.stdout
    assert "NON-CLOSED" in result.stdout


def test_scheduler_check_skips_stale_wrapper_pid_before_real_scheduler(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "pgrep", "#!/bin/sh\necho 4241\necho 4242\n")
    _write_executable(
        fake_bin / "lsof",
        """#!/bin/sh
case " $* " in
  *" -p 4241 "*) exit 1 ;;
esac
printf 'p4242\\nfcwd\\nn%s\\n' "$FAKE_SCHEDULER_CWD"
""",
    )
    result = subprocess.run(
        ["/bin/bash", str(CI_DIR / "check_background_scheduler.sh")],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_SCHEDULER_CWD": str(PROJECT_ROOT),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "PID: 4242" in result.stdout
    assert "PID: 4241" not in result.stdout


def test_release_gate_override_never_claims_full_gate_passed(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "bash",
        f"""#!{sys.executable}
import sys

target = sys.argv[1] if len(sys.argv) > 1 else ""
if target.endswith("check_background_scheduler.sh"):
    print("NON-CLOSED: background scheduler override active")
raise SystemExit(0)
""",
    )
    fake_python = fake_bin / "python"
    _write_executable(
        fake_python,
        f"""#!{sys.executable}
import os
import sys

if len(sys.argv) > 1 and sys.argv[1].endswith((
    "check_tracked_credentials.py",
    "check_api_contract.py",
    "check_api_documentation.py",
    "audit_dead_code.py",
)):
    raise SystemExit(0)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
""",
    )
    result = subprocess.run(
        ["/bin/bash", str(CI_DIR / "test_release_gate.sh")],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTHON_BIN": str(fake_python),
            "AIOPS_RELEASE_GATE_ALLOW_BACKGROUND_SCHEDULER": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "NON-CLOSED" in result.stdout
    assert "Release gate passed" not in result.stdout


def test_tracked_credential_gate_rejects_literal_credentials_and_allows_runtime_references(
    tmp_path,
):
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_tracked_credential_scan",
    )
    literal = tmp_path / "literal.env"
    literal.write_text("PROD_SSH_PASS=not-a-runtime-reference\n", encoding="utf-8")
    runtime_reference = tmp_path / "runtime.env"
    runtime_reference.write_text("PROD_SSH_PASS=${PROD_SSH_PASS}\n", encoding="utf-8")

    assert scanner.scan_paths(tmp_path, [literal]) == [
        ("literal.env", ("credential_assignment",))
    ]
    assert scanner.scan_paths(tmp_path, [runtime_reference]) == []


def test_tracked_credential_gate_rejects_unsafe_release_syntax_but_not_generic_examples(
    tmp_path,
):
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_tracked_credential_scan_unsafe_release_syntax",
    )
    release_script = tmp_path / "scripts" / "release" / "unsafe.sh"
    release_script.parent.mkdir(parents=True)
    release_script.write_text(
        "PROD_SSH_PASS=${PROD_SSH_PASS}\n"
        "echo value | sudo -S true\n"
        "ssh -o StrictHostKeyChecking=no prod.invalid\n"
        "expect_user -re password\n"
        "spawn ssh prod.invalid\n"
        "sudo -n -- bash -c true\n",
        encoding="utf-8",
    )
    generic_example = tmp_path / ".env.example"
    generic_example.write_text(
        "PROD_SSH_PASS=${PROD_SSH_PASS}\n"
        "SSH_OPTION=StrictHostKeyChecking=no\n",
        encoding="utf-8",
    )

    assert scanner.scan_paths(tmp_path, [release_script, generic_example]) == [
        (
            "scripts/release/unsafe.sh",
            (
                "expect_password_interaction",
                "legacy_production_entry",
                "password_environment",
                "ssh_host_key_checking_disabled",
                "sudo_stdin",
                "unrestricted_root_shell",
            ),
        )
    ]


def test_tracked_credential_gate_scans_tests_and_keeps_credential_assignment(tmp_path):
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_tracked_credential_scan_all_tracked_paths",
    )
    test_source = tmp_path / "tests" / "credential_fixture.py"
    test_source.parent.mkdir()
    test_source.write_text("SERVICE_TOKEN=credential-value-123\n", encoding="utf-8")

    assert scanner.scan_paths(tmp_path, [test_source]) == [
        ("tests/credential_fixture.py", ("credential_assignment",))
    ]


def test_tracked_credential_gate_rejects_percent_encoded_uriviera_userinfo(tmp_path):
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_tracked_credential_scan_uriviera_userinfo",
    )
    runbook = tmp_path / "docs" / "runbook.md"
    runbook.parent.mkdir()
    runbook.write_text(
        "git push https://release-user:"
        + "encoded%40credential"
        + "@git.invalid/repo.git\n",
        encoding="utf-8",
    )

    assert scanner.scan_paths(tmp_path, [runbook]) == [
        ("docs/runbook.md", ("uriviera_userinfo_credential",))
    ]


def test_tracked_credential_gate_rejects_unquoted_markdown_password_values(tmp_path):
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_tracked_credential_scan_unquoted_markdown_password",
    )
    runbook = tmp_path / "docs" / "runbook.md"
    runbook.parent.mkdir()
    runbook.write_text(
        "username: admin\n"
        "password: live-password-value-123\n",
        encoding="utf-8",
    )

    assert scanner.scan_paths(tmp_path, [runbook]) == [
        ("docs/runbook.md", ("credential_assignment",))
    ]


def test_tracked_credential_gate_rejects_markdown_inline_code_api_key(tmp_path):
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_tracked_credential_scan_markdown_inline_code_api_key",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    literal = docs / "runbook.md"
    literal.write_text(
        "- apiKey `synthetic-live-inline-key-1234567890`\n",
        encoding="utf-8",
    )
    allowed = docs / "credentials.md"
    allowed.write_text(
        "- apiKey `[REDACTED]`\n"
        "- apiKey `placeholder-api-key`\n"
        "The `apiKey` field name is a documentation term.\n",
        encoding="utf-8",
    )

    assert scanner.scan_paths(tmp_path, [literal]) == [
        ("docs/runbook.md", ("credential_assignment",))
    ]
    assert scanner.scan_paths(tmp_path, [allowed]) == []


def test_tracked_credential_gate_rejects_narrative_inline_credentials(tmp_path):
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_tracked_credential_scan_narrative_inline_credentials",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    emphasized = docs / "emphasized.md"
    emphasized.write_text(
        "- **Auth**: service uses apiKey `synthetic-live-inline-key-1234567890`; tenant=1\n",
        encoding="utf-8",
    )
    narrative = docs / "narrative.md"
    narrative.write_text(
        "Narrative prefix: token `synthetic-live-inline-token-1234567890`\n",
        encoding="utf-8",
    )
    allowed = docs / "credentials.mdx"
    allowed.write_text(
        "Narrative prefix: apiKey `[REDACTED]`\n"
        "Narrative prefix: token `placeholder-token`\n"
        "The `apiKey` and `token` names are documentation terms.\n",
        encoding="utf-8",
    )

    assert scanner.scan_paths(tmp_path, [emphasized]) == [
        ("docs/emphasized.md", ("credential_assignment",))
    ]
    assert scanner.scan_paths(tmp_path, [narrative]) == [
        ("docs/narrative.md", ("credential_assignment",))
    ]
    assert scanner.scan_paths(tmp_path, [allowed]) == []


def test_tracked_path_inventory_includes_all_repository_areas():
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_tracked_credential_inventory",
    )
    tracked = {path.as_posix() for path in scanner.tracked_paths(PROJECT_ROOT)}

    assert "tests/test_ci_scripts_contract.py" in tracked
    assert "src/api/app.py" in tracked
    assert "frontend/src/App.tsx" in tracked
    assert "scripts/release/release.py" in tracked
    assert "scripts/release/tke_candidate.py" in tracked


def test_repository_credential_scan_has_no_release_findings_after_restricted_wrapper():
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_repository_credential_scan",
    )
    paths = scanner.tracked_paths(PROJECT_ROOT) + [
        Path("docs/production_deployment_guide.md"),
        Path("scripts/ci/check_tracked_credentials.py"),
    ]

    assert scanner.scan_paths(PROJECT_ROOT, paths) == []


def test_tracked_credential_gate_formats_findings_as_path_and_rule_only():
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_tracked_credential_scan_output",
    )

    assert scanner.format_findings(
        [("scripts/ops/legacy.exp", ("password_environment", "sudo_stdin"))]
    ) == "scripts/ops/legacy.exp: password_environment, sudo_stdin\n"


def test_release_gate_stops_immediately_when_credential_scanner_fails(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    _write_executable(
        fake_python,
        f"""#!{sys.executable}
import sys
if len(sys.argv) > 1 and sys.argv[1].endswith("check_tracked_credentials.py"):
    print("scripts/ops/legacy.exp: password_environment", file=sys.stderr)
    raise SystemExit(23)
raise SystemExit("release gate ran after failed credential scanner")
""",
    )

    result = subprocess.run(
        ["/bin/bash", str(CI_DIR / "test_release_gate.sh")],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHON_BIN": str(fake_python)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 23
    assert "scripts/ops/legacy.exp: password_environment" in result.stderr


def test_production_guide_is_version_controlled_tke_candidate_and_has_no_literal_credentials():
    scanner = _load_python_module(
        TRACKED_CREDENTIAL_SCAN,
        "test_production_guide_credential_scan",
    )

    assert PRODUCTION_DEPLOYMENT_GUIDE.is_file()
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "docs/production_deployment_guide.md"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    assert ignored.returncode == 1
    assert scanner.scan_paths(PROJECT_ROOT, [PRODUCTION_DEPLOYMENT_GUIDE]) == []
    guide = _read(PRODUCTION_DEPLOYMENT_GUIDE)
    assert "TKE 主链固定为" in guide
    assert "tke-preflight" in guide
    assert "tke-pipeline" in guide
    assert "tke-candidate" in guide
    assert "tke-prepare" in guide
    assert "tke-rehearse-pull" in guide
    assert "tke-migrate-schema" not in guide
    assert "tke-rehearse-mysql" in guide
    assert "tke-rehearse-deploy" in guide
    assert "tke-activate" in guide
    assert "tke-accept" in guide
    assert "tke-rollback" in guide
    assert "临时 Kubernetes API 访问配置" in guide
    assert "不等同于后续应用 Ingress/CLB" in guide
    assert "不得绕过入口直接运行 `kubectl apply`" in guide
    assert "Secret/aiops-runtime-secrets" in guide


def test_plane_cicd_contract_declares_tke_only_release_gates_and_evidence():
    contract = yaml.safe_load(_read(PLANE_CICD))

    assert contract["version"] == 2
    assert contract["project"] == "aiops"
    assert contract["scm"]["default_branch"] == "master"
    assert contract["entrypoint"] == "python3 scripts/release/release.py"
    assert "python scripts/release/release.py" not in contract["entrypoint"]
    assert contract["target"] == {
        "cluster_id": "cls-demo0000",
        "context": "cls-demo0000-000000000000-context-default",
        "ingress_class": "qcloud",
        "namespace": "aiops-demo",
        "provider": "tke",
        "registry": "registry.example.test/aiops",
    }
    assert contract["source"]["requires_clean_worktree"] is True
    assert contract["source"]["requires_independent_worktree"] is True
    assert contract["source"]["requires_exact_commit"] is True
    assert list(contract["lifecycle"]) == [
        "preflight",
        "candidate",
        "prepare",
        "rehearse_pull",
        "rehearse_mysql",
        "rehearse_deploy",
        "activate",
        "accept",
        "rollback",
        "cleanup_failed_candidate",
    ]
    lifecycle = contract["lifecycle"]
    assert lifecycle["preflight"]["command"].endswith(" tke-preflight")
    assert lifecycle["preflight"]["read_only"] is True
    assert lifecycle["candidate"]["command"].endswith(" tke-candidate")
    assert lifecycle["candidate"]["mutates"] == ["tcr_backend", "tcr_frontend"]
    assert lifecycle["prepare"]["read_only"] is True
    assert lifecycle["prepare"]["checks"] == [
        "exact_cluster_context",
        "runtime_configmap",
        "runtime_secret_key_names",
        "runtime_rwx_pvc",
    ]
    assert lifecycle["rehearse_pull"]["customer_traffic"] is False
    assert "migrate_schema" not in lifecycle
    assert "diagnose_schema" not in lifecycle
    assert lifecycle["rehearse_mysql"]["command"].endswith(" tke-rehearse-mysql")
    assert lifecycle["rehearse_mysql"]["customer_traffic"] is False
    assert lifecycle["rehearse_mysql"]["requires"] == [
        "immutable_candidate",
        "cluster_pull_proof",
        "exact_release_sha_confirmation",
    ]
    assert lifecycle["rehearse_mysql"]["acceptance"] == [
        "backend_digest_identity",
        "mysql_session_read_only",
        "mysql_read_only_query",
        "temporary_pod_cleanup",
    ]
    assert lifecycle["rehearse_mysql"]["produces"] == ["mysql_rehearsal_proof"]
    assert contract["pipeline"] == {
        "activation_requires_explicit_through": "activate",
        "automatic_retry": False,
        "command": "python3 scripts/release/release.py tke-pipeline",
        "default_through": "standby",
        "stops_on_first_failure": True,
        "timing_state": "tke-stage-timings.json",
    }
    assert lifecycle["rehearse_deploy"]["scheduler_mode"] == "standby"
    assert lifecycle["rehearse_deploy"]["renders_ingress"] is False
    assert "mysql_rehearsal_proof" in lifecycle["rehearse_deploy"]["requires"]
    assert lifecycle["activate"]["requires"] == [
        "immutable_candidate",
        "cluster_pull_proof",
        "mysql_rehearsal_proof",
        "standby_candidate_acceptance",
        "exact_release_sha_confirmation",
    ]
    assert lifecycle["activate"]["scheduler_mode"] == "active"
    assert lifecycle["accept"]["read_only"] is True
    assert lifecycle["rollback"]["target"] == "previous_recorded_immutable_release"
    assert contract["runtime"]["persistent_volume_claim"] == {
        "access_mode": "ReadWriteMany",
        "name": "aiops-runtime-data",
        "required_phase": "Bound",
    }
    assert contract["runtime"]["sensitive_values_persisted"] is False
    assert contract["traffic"]["changed_by_release_pipeline"] is False
    assert contract["traffic"]["cutover_requires_separate_confirmation"] is True
    assert contract["traffic"]["external_https_termination"] is True
    for retired in ("bootstrap", "helper_upgrade", "candidate", "promote", "publish", "ship"):
        assert retired not in contract
    assert set(contract["required_evidence"]) >= {
        "backend_digest",
        "frontend_digest",
        "cluster_pull_proof",
        "mysql_rehearsal_proof",
        "standby_candidate_acceptance",
        "active_release_acceptance",
        "rollback_acceptance",
    }


def test_gitignore_unignores_only_the_release_credential_scanner():
    scanner_ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "scripts/ci/check_tracked_credentials.py"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    unrelated_ci_file_ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "scripts/ci/local-only-helper.py"],
        cwd=PROJECT_ROOT,
        check=False,
    )

    assert scanner_ignored.returncode == 1
    assert unrelated_ci_file_ignored.returncode == 1


def test_playwright_uses_dead_proxy_with_loopback_bypass_for_offline_gate():
    config = _read(PLAYWRIGHT_CONFIG)
    assert "AIOPS_OFFLINE_TEST" in config
    assert "http://127.0.0.1:9" in config
    assert "localhost,127.0.0.1" in config


def test_requirements_declare_pytest_cov_for_backend_coverage_gate():
    requirements = _read(REQUIREMENTS_TXT)
    assert "pytest-cov" in requirements


def test_docker_build_context_excludes_runtime_data_and_local_config():
    ignored = _dockerignore_entries(DOCKERIGNORE)

    required = {
        "data/",
        "logs/",
        "config.yaml",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
        "*.log",
        ".coverage",
        "frontend/coverage/",
        "frontend/coverage-analysis/",
    }

    assert required <= ignored, f"missing Docker build exclusions: {sorted(required - ignored)}"


def test_backend_docker_context_excludes_local_artifacts_and_non_production_sources():
    ignored = _dockerignore_entries(DOCKERIGNORE)
    required = {
        "**/._*",
        "**/.DS_Store",
        "__MACOSX",
        "backups/",
        "test-results/",
        "frontend/",
        "scripts/db/migrate_test_*",
        "scripts/db/seed_*",
        "scripts/db/init_*local*",
        "scripts/db/check_db_data.py",
    }

    assert required <= ignored, f"missing backend Docker exclusions: {sorted(required - ignored)}"
    assert {"coverage/", "**/coverage/"} & ignored, "missing backend coverage exclusion"

    assert _dockerfile_copy_instructions(DOCKERFILE) == BACKEND_COPY_INSTRUCTIONS
    _assert_local_copy_roots_visible(ignored, _dockerfile_local_copy_sources(DOCKERFILE))


def test_frontend_docker_context_excludes_test_and_local_tooling_sources():
    ignored = _dockerignore_entries(FRONTEND_DOCKERIGNORE)
    required = {
        "**/._*",
        "**/.DS_Store",
        "__MACOSX",
        "src/**/*.test.*",
        "src/**/*.spec.*",
        "src/test",
        "src/testUtils",
        "src/mocks",
        "vitest.config.ts",
        "playwright.config.ts",
        "eslint.config.js",
    }

    assert required <= ignored, f"missing frontend Docker exclusions: {sorted(required - ignored)}"

    assert _dockerfile_copy_instructions(FRONTEND_DOCKERFILE) == FRONTEND_COPY_INSTRUCTIONS
    _assert_local_copy_roots_visible(
        ignored, _dockerfile_local_copy_sources(FRONTEND_DOCKERFILE)
    )


def test_readmes_remain_git_tracked_but_outside_production_contexts():
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "README.md"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert tracked.returncode == 0, tracked.stderr
    assert not (PROJECT_ROOT / "frontend" / "README.md").exists()
    assert "*.md" in _dockerignore_entries(DOCKERIGNORE)
    assert "*.md" in _dockerignore_entries(FRONTEND_DOCKERIGNORE)
