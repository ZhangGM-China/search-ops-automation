import json
import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "ci" / "audit_dead_code.py"
ALLOWLIST = ROOT / "scripts" / "ci" / "dead_code_allowlist.json"


def _audit_module():
    spec = importlib.util.spec_from_file_location("audit_dead_code", AUDIT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collect_entrypoints() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(AUDIT), "--collect-entrypoints-only"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


def test_dead_code_audit_collects_stable_dynamic_entrypoints():
    assert AUDIT.is_file()
    assert ALLOWLIST.is_file()

    first = _collect_entrypoints()
    second = _collect_entrypoints()

    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    report = json.loads(first.stdout)
    assert report["schemaVersion"] == 1
    assert len(report["gitSha"]) == 40
    assert report["mode"] == "entrypoints-only"
    assert set(report["entrypoints"]) == {
        "cli",
        "docker",
        "fastapiRoutes",
        "ormModels",
        "reactRoutes",
        "schedulerJobs",
        "shell",
    }
    assert report["entrypoints"]["fastapiRoutes"]
    assert report["entrypoints"]["ormModels"]
    react_routes = report["entrypoints"]["reactRoutes"]
    assert react_routes
    assert all(route.startswith("frontend/src/app/AppRouter.tsx:") for route in react_routes)
    assert any(route.endswith(":/login") for route in react_routes)
    assert report["entrypoints"]["schedulerJobs"]
    assert json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" == first.stdout


def test_dead_code_audit_is_read_only_and_fail_closed():
    source = AUDIT.read_text()
    for tool in ("ruff", "vulture", "deptry", "knip"):
        assert tool in source
    for forbidden in ("--fix", "--allow-remove-files", "unlink(", "rmtree("):
        assert forbidden not in source
    assert "check=True" in source

    allowlist = json.loads(ALLOWLIST.read_text())
    assert allowlist["schemaVersion"] == 1
    assert isinstance(allowlist["vulture"], list)
    assert all(set(item) == {"file", "name", "kind"} for item in allowlist["vulture"])


def test_dead_code_audit_parses_json_after_tool_log_prefix():
    parser = _audit_module()._parse_json_output
    assert parser("[tool] startup\n{\"issues\": []}\n", "knip") == {"issues": []}
    with __import__("pytest").raises(RuntimeError, match="knip_output_invalid"):
        parser("{\"issues\": []}\ntrailing output", "knip")


def test_dead_code_audit_normalizes_ruff_findings_in_a_stable_order():
    normalize = _audit_module()._normalize_ruff
    raw = json.dumps(
        [
            {
                "code": "F841",
                "filename": str(ROOT / "src" / "z.py"),
                "location": {"row": 4, "column": 2},
                "message": "local variable is assigned to but never used",
            },
            {
                "code": "F401",
                "filename": str(ROOT / "src" / "a.py"),
                "location": {"row": 2, "column": 1},
                "message": "imported but unused",
            },
        ]
    )

    assert normalize(raw) == [
        {
            "code": "F401",
            "file": "src/a.py",
            "line": 2,
            "column": 1,
            "message": "imported but unused",
        },
        {
            "code": "F841",
            "file": "src/z.py",
            "line": 4,
            "column": 2,
            "message": "local variable is assigned to but never used",
        },
    ]


def test_dead_code_audit_normalizes_unordered_tool_findings_recursively():
    normalize = _audit_module()._normalize_json

    assert normalize(
        {
            "issues": [
                {"name": "zeta", "files": ["src/z.ts", "src/a.ts"]},
                {"name": "alpha", "files": ["src/d.ts", "src/c.ts"]},
            ]
        }
    ) == {
        "issues": [
            {"name": "alpha", "files": ["src/c.ts", "src/d.ts"]},
            {"name": "zeta", "files": ["src/a.ts", "src/z.ts"]},
        ]
    }


def test_dead_code_audit_full_report_is_stable():
    runs = [
        subprocess.run(
            [sys.executable, str(AUDIT)],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        for _ in range(2)
    ]

    assert [run.returncode for run in runs] == [0, 0]
    assert [run.stderr for run in runs] == ["", ""]
    assert runs[0].stdout == runs[1].stdout
    report = json.loads(runs[0].stdout)
    assert set(report["tools"]) == {"ruff", "vulture", "deptry", "knip"}
    assert set(report["candidates"]) == {"ruff", "vulture", "deptry", "knip"}


def test_release_gate_runs_dead_code_audit():
    release_gate = (ROOT / "scripts" / "ci" / "test_release_gate.sh").read_text()
    assert "scripts/ci/audit_dead_code.py" in release_gate


def test_retired_direct_python_dependencies_are_absent():
    retired = {"langchain", "pandas", "mypy", "pip-audit"}
    for requirements_file in ("requirements.runtime.in", "requirements.txt"):
        names = {
            line.split("==", 1)[0].split("[", 1)[0].strip().lower()
            for line in (ROOT / requirements_file).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        assert retired.isdisjoint(names)
