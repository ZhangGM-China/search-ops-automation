import datetime as dt
import importlib.util
import io
import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPLY_CHAIN_GATE = PROJECT_ROOT / "scripts" / "ci" / "supply_chain_gate.py"


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("test_supply_chain_gate_module", SUPPLY_CHAIN_GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_exception_contract_binds_owner_mitigation_package_version_and_expiry(gate, tmp_path):
    exception_file = _write_json(
        tmp_path / "exceptions.json",
        {
            "version": 1,
            "exceptions": [
                {
                    "component": "backend",
                    "vulnerabilityId": "CVE-2026-12345",
                    "packagePurl": "pkg:pypi/example@1.2.3",
                    "packageVersion": "1.2.3",
                    "owner": "security-owner",
                    "reason": "No patched release is available.",
                    "mitigation": "Disable the affected feature.",
                    "expiresOn": "2026-08-12",
                }
            ],
        },
    )

    accepted = gate.validate_exceptions(exception_file, dt.date(2026, 8, 11))
    assert accepted[0]["packageVersion"] == "1.2.3"

    with pytest.raises(gate.SupplyChainGateError, match="expired"):
        gate.validate_exceptions(exception_file, dt.date(2026, 8, 13))


def test_spdx_sbom_is_bound_to_image_and_rejects_forbidden_license(gate, tmp_path):
    image = "aiops-sbom-scan:backend-" + "a" * 16
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "name": image.replace(":", "-", 1),
        "documentNamespace": "https://spdx.local/" + image.replace(":", "-", 1),
        "packages": [
            {
                "name": "example",
                "licenseDeclared": "MIT",
                "licenseConcluded": "MIT",
                "externalRefs": [
                    {"referenceLocator": "pkg:pypi/example@1.2.3"}
                ],
            }
        ],
    }
    sbom_file = _write_json(tmp_path / "sbom.json", sbom)

    evidence = gate.validate_sbom(sbom_file, image)
    assert evidence["format"] == "spdx-json"
    assert evidence["packageCount"] == 1
    assert evidence["packageTypes"] == {"pypi": 1}

    sbom["packages"][0]["licenseDeclared"] = "AGPL-3.0-only"
    _write_json(sbom_file, sbom)
    with pytest.raises(gate.SupplyChainGateError, match="forbidden license"):
        gate.validate_sbom(sbom_file, image)


def test_scout_sarif_requires_versioned_package_purl(gate, tmp_path):
    sarif_file = _write_json(
        tmp_path / "scout.sarif.json",
        {
            "version": "2.1.0",
            "runs": [
                {
                    "results": [
                        {
                            "ruleId": "CVE-2026-12345",
                            "message": {"text": "Package: pkg:pypi/example@1.2.3"},
                        }
                    ]
                }
            ],
        },
    )

    assert gate.sarif_findings(sarif_file) == [
        {
            "vulnerabilityId": "CVE-2026-12345",
            "packagePurl": "pkg:pypi/example@1.2.3",
            "packageVersion": "1.2.3",
        }
    ]


def test_tool_lock_requires_version_and_binary_sha(gate, tmp_path, monkeypatch):
    lock_file = _write_json(
        tmp_path / "tools.json",
        {
            "version": 1,
            "platforms": {
                "darwin/arm64": {
                    "dockerSbom": {
                        "version": "0.6.0",
                        "provider": "syft 0.43.0",
                        "sha256": "1" * 64,
                    },
                    "dockerScout": {"version": "v1.20.3", "sha256": "2" * 64},
                }
            },
        },
    )
    monkeypatch.setattr(gate, "platform_key", lambda: "darwin/arm64")

    locked = gate.validate_tool_lock(lock_file)
    assert locked["dockerSbom"]["sha256"] == "1" * 64
    assert locked["dockerScout"]["version"] == "v1.20.3"


def test_secret_scanner_ignores_private_key_delimiter_constants(gate):
    library_source = b"\n".join(
        (
            b'_SK_START = b"-----BEGIN OPENSSH PRIVATE KEY-----"',
            b'_SK_END = b"-----END OPENSSH PRIVATE KEY-----"',
        )
    )

    assert gate._scan_stream_for_secret(io.BytesIO(library_source)) == (
        False,
        len(library_source),
    )


def test_secret_scanner_rejects_complete_private_key_block(gate):
    private_key = b"\n".join(
        (
            b"-----BEGIN OPENSSH PRIVATE KEY-----",
            b"QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB",
            b"QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB",
            b"-----END OPENSSH PRIVATE KEY-----",
        )
    )

    assert gate._scan_stream_for_secret(io.BytesIO(private_key)) == (
        True,
        len(private_key),
    )


def test_secret_scanner_ignores_aws_pattern_embedded_in_font_data(gate):
    font_data = b"QAHAGQAAQAAAAAAAwAaAKIAAQAAAAAABAAHAM0AAQAAAAAABQA8AU8A"

    assert gate._scan_stream_for_secret(io.BytesIO(font_data)) == (
        False,
        len(font_data),
    )


def test_secret_scanner_rejects_standalone_aws_access_key(gate):
    access_key = b'aws_access_key_id = "' + b'AKIA' + b'1234567890ABCDEF' + b'"'

    assert gate._scan_stream_for_secret(io.BytesIO(access_key)) == (
        True,
        len(access_key),
    )


def test_sdk_public_example_does_not_hide_other_keys(gate):
    example = b'AccessKeyId: "AKIAIOSFODNN7EXAMPLE"'
    assert gate._scan_stream_for_secret(io.BytesIO(example)) == (False, len(example))
    real = example + b'\n' + b'AKIA' + b'1234567890ABCDEF'
    assert gate._scan_stream_for_secret(io.BytesIO(real))[0] is True


def test_only_production_pi_dependencies_are_allowed(gate):
    assert not gate._rootfs_path_is_forbidden("backend", "app/pi-runtime/node_modules/lib/index.js")
    for path in ("app/node_modules/lib/index.js", "app/pi-runtime/node_modules/lib/tests/a.js",
                 "app/pi-runtime/node_modules/lib/coverage/a.json", "app/pi-runtime/test/a.js"):
        assert gate._rootfs_path_is_forbidden("backend", path)
    assert gate._rootfs_path_is_secret("app/pi-runtime/node_modules/lib/.env")


def test_alpine_runtime_uses_complete_hash_lock():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    lock_path = PROJECT_ROOT / "requirements.lock"
    runtime_requirements = (PROJECT_ROOT / "requirements.runtime.in").read_text(
        encoding="utf-8"
    )

    assert lock_path.is_file()
    lock = lock_path.read_text(encoding="utf-8")
    requirement_lines = [
        line for line in lock.splitlines()
        if line and not line.startswith(("#", " ", "-"))
    ]
    assert requirement_lines
    assert all("==" in line for line in requirement_lines)
    assert "--hash=sha256:" in lock
    assert "greenlet==3.5.6" in lock
    assert "pillow==12.3.0" in lock
    assert "redis==6.4.0" in lock
    assert "redis==6.4.0" in runtime_requirements
    assert "COPY requirements.lock /app/requirements.lock" in dockerfile
    assert "pip install --require-hashes -r requirements.lock" in dockerfile
    assert "apk upgrade --no-cache" in dockerfile
    assert "-name SelfTest -o -name __pycache__" in dockerfile
    assert "-r requirements.txt" not in dockerfile
    assert 'pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple "httpx[socks]"' not in dockerfile


def test_retired_festival_runtime_is_absent():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    config = (PROJECT_ROOT / "src" / "config.py").read_text(encoding="utf-8")
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    runtime_requirements = (PROJECT_ROOT / "requirements.runtime.in").read_text(
        encoding="utf-8"
    )

    assert "china-festival-mcp" not in requirements
    assert "china-festival-mcp" not in runtime_requirements
    assert '"command": "china-festival-mcp"' not in config
    assert '"command": "uvx"' not in config
    assert "uv tool install" not in dockerfile
    assert "aiops.uv-version" not in dockerfile


def test_retired_mcp_runtime_is_absent_from_lock():
    runtime_requirements = (PROJECT_ROOT / "requirements.runtime.in").read_text(
        encoding="utf-8"
    )
    lock = (PROJECT_ROOT / "requirements.lock").read_text(encoding="utf-8")

    assert "china-festival-mcp" not in runtime_requirements
    assert "fastmcp" not in runtime_requirements
    assert "mcp==" not in runtime_requirements
    assert "china-festival-mcp" not in lock
    assert "fastmcp==" not in lock
    assert "mcp==" not in lock


def test_tke_candidate_runs_runtime_and_supply_chain_gates_before_tcr_login():
    candidate = (
        PROJECT_ROOT / "scripts" / "release" / "tke_candidate.py"
    ).read_text(encoding="utf-8")
    create = candidate[candidate.index("def create_candidate("):]

    runtime_gate = create.index("_run_runtime_base_image_gate(")
    first_build = create.index('for component in ("backend", "frontend"):')
    supply_chain_gate = create.index("_run_supply_chain_gate(")
    tcr_login = create.index("_tcr_login_token(")
    assert runtime_gate < first_build < supply_chain_gate < tcr_login
    assert "check_runtime_base_image.py" in candidate
    assert "supply_chain_gate.py" in candidate
