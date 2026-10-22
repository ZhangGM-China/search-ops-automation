#!/usr/bin/env python3
"""Collect stable, read-only dead-code candidates and dynamic entrypoints."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
ALLOWLIST_PATH = Path(__file__).with_name("dead_code_allowlist.json")
_ROUTE_METHODS = {"delete", "get", "options", "patch", "post", "put"}


class AuditError(RuntimeError):
    pass


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _command(
    argv: list[str],
    *,
    cwd: Path = ROOT,
    accepted_returncodes: Iterable[int] = (0,),
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode not in set(accepted_returncodes):
        detail = (result.stderr or result.stdout).strip().splitlines()
        summary = detail[-1] if detail else "no diagnostic output"
        raise AuditError(f"command_failed:{Path(argv[0]).name}:{summary}")
    return result


def _version(argv: list[str], *, cwd: Path = ROOT) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
    )
    return (result.stdout or result.stderr).strip().splitlines()[0]


def _parse_json_output(raw: str, tool: str) -> Any:
    """Parse a JSON report while tolerating tool startup logs before it."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(raw):
        if character not in "[{":
            continue
        try:
            payload, end = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if raw[index + end :].strip():
            raise AuditError(f"{tool}_output_invalid")
        return payload
    raise AuditError(f"{tool}_output_invalid")


def _tracked_files(*patterns: str) -> list[Path]:
    result = _command(["git", "ls-files", "--", *patterns])
    return [
        ROOT / value
        for value in result.stdout.splitlines()
        if value and (ROOT / value).is_file()
    ]


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _decorator_route(node: ast.AST) -> str | None:
    call = node if isinstance(node, ast.Call) else None
    func = call.func if call else node
    if not isinstance(func, ast.Attribute) or func.attr not in _ROUTE_METHODS:
        return None
    if not call or not call.args:
        return None
    path = call.args[0]
    return path.value if isinstance(path, ast.Constant) and isinstance(path.value, str) else None


def _python_entrypoints() -> tuple[list[str], list[str], list[str], list[str]]:
    routes: list[str] = []
    orm_models: list[str] = []
    scheduler_jobs: list[str] = []
    cli: list[str] = []
    for path in _tracked_files("src/*.py", "src/**/*.py", "scripts/*.py", "scripts/**/*.py", "main.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise AuditError(f"python_ast_failed:{_relative(path)}:{exc}") from exc
        relative = _relative(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    route = _decorator_route(decorator)
                    if route is not None:
                        routes.append(f"{relative}:{node.lineno}:{route}:{node.name}")
            if isinstance(node, ast.ClassDef) and any(
                isinstance(base, ast.Name) and base.id == "Base" for base in node.bases
            ):
                orm_models.append(f"{relative}:{node.lineno}:{node.name}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "add_job" and node.args:
                    job = node.args[0]
                    if isinstance(job, ast.Name):
                        scheduler_jobs.append(f"{relative}:{node.lineno}:{job.id}")
                    elif isinstance(job, ast.Attribute):
                        scheduler_jobs.append(f"{relative}:{node.lineno}:{job.attr}")
        if any(
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "__name__"
            and any(
                isinstance(item, ast.Constant) and item.value == "__main__"
                for item in node.comparators
            )
            for node in ast.walk(tree)
        ):
            cli.append(relative)
    return routes, orm_models, scheduler_jobs, cli


def _react_routes() -> list[str]:
    app = FRONTEND / "src" / "app" / "AppRouter.tsx"
    routes = []
    for line_number, line in enumerate(app.read_text(encoding="utf-8").splitlines(), 1):
        for match in re.finditer(r'<Route\s+[^>]*path=(?:"([^"]+)"|\'([^\']+)\')', line):
            route = match.group(1) or match.group(2)
            routes.append(f"frontend/src/app/AppRouter.tsx:{line_number}:{route}")
        if "<Route index " in line:
            routes.append(f"frontend/src/app/AppRouter.tsx:{line_number}:<index>")
    return routes


def _line_entries(paths: list[Path], pattern: re.Pattern[str]) -> list[str]:
    entries = []
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                entries.append(f"{_relative(path)}:{line_number}:{line.strip()}")
    return entries


def collect_entrypoints() -> dict[str, list[str]]:
    routes, models, jobs, cli = _python_entrypoints()
    shell = [_relative(path) for path in _tracked_files("*.sh", "scripts/**/*.sh")]
    docker_files = _tracked_files("Dockerfile", "**/Dockerfile", "docker-compose*.yml")
    docker = _line_entries(
        docker_files,
        re.compile(r"^\s*(?:ENTRYPOINT|CMD|command:|entrypoint:)", re.IGNORECASE),
    )
    return {
        "cli": sorted(set(cli)),
        "docker": sorted(set(docker)),
        "fastapiRoutes": sorted(set(routes)),
        "ormModels": sorted(set(models)),
        "reactRoutes": sorted(set(_react_routes())),
        "schedulerJobs": sorted(set(jobs)),
        "shell": sorted(set(shell)),
    }


def _git_sha() -> str:
    return _command(["git", "rev-parse", "HEAD"]).stdout.strip()


def _load_allowlist() -> dict[str, Any]:
    try:
        payload = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"allowlist_invalid:{exc}") from exc
    if set(payload) != {"schemaVersion", "vulture"} or payload["schemaVersion"] != 1:
        raise AuditError("allowlist_schema_invalid")
    for item in payload["vulture"]:
        if set(item) != {"file", "name", "kind"}:
            raise AuditError("allowlist_entry_invalid")
    return payload


def _knip_binary() -> Path:
    local = FRONTEND / "node_modules" / ".bin" / "knip"
    if local.is_file():
        return local
    worktrees = _command(["git", "worktree", "list", "--porcelain"]).stdout.splitlines()
    for line in worktrees:
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree ")) / "frontend" / "node_modules" / ".bin" / "knip"
        if candidate.is_file():
            return candidate
    npm_cache = Path.home() / ".npm" / "_npx"
    candidates = sorted(
        npm_cache.glob("*/node_modules/.bin/knip"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    raise AuditError("knip_binary_missing")


def _normalize_ruff(raw: str) -> list[dict[str, Any]]:
    try:
        findings = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditError("ruff_output_invalid") from exc
    normalized = [
        {
            "code": item["code"],
            "file": _relative(Path(item["filename"])),
            "line": item["location"]["row"],
            "column": item["location"]["column"],
            "message": item["message"],
        }
        for item in findings
    ]
    return sorted(
        normalized,
        key=lambda item: (
            item["file"],
            item["line"],
            item["column"],
            item["code"],
            item["message"],
        ),
    )


def _normalize_vulture(raw: str, allowlist: dict[str, Any]) -> list[str]:
    allowed = {
        (item["file"], item["name"], item["kind"])
        for item in allowlist["vulture"]
    }
    findings = []
    pattern = re.compile(r"^(.*?):(\d+): unused (\w+) '([^']+)'")
    for line in raw.splitlines():
        match = pattern.match(line)
        if not match:
            if line.strip():
                raise AuditError(f"vulture_output_invalid:{line}")
            continue
        file_name, line_number, kind, name = match.groups()
        relative = _relative(Path(file_name))
        if (relative, name, kind) not in allowed:
            findings.append(f"{relative}:{line_number}:{kind}:{name}")
    return sorted(findings)


def _normalize_json(value: Any) -> Any:
    """Make tool reports deterministic without changing their information."""
    if isinstance(value, dict):
        return {
            key: _normalize_json(value[key])
            for key in sorted(value, key=str)
        }
    if isinstance(value, list):
        normalized = [_normalize_json(item) for item in value]
        return sorted(
            normalized,
            key=_json_sort_key,
        )
    return value


def _json_sort_key(value: Any) -> tuple[str, str, str]:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if isinstance(value, dict):
        for identity_key in ("file", "name", "module", "code", "id"):
            if identity_key in value:
                return identity_key, str(value[identity_key]), canonical
    return "", "", canonical


def run_tools(allowlist: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    versions = {
        "deptry": _version(["uvx", "--offline", "deptry", "--version"]),
        "knip": _version([str(_knip_binary()), "--version"], cwd=FRONTEND),
        "ruff": _version(["uvx", "--offline", "ruff", "--version"]),
        "vulture": _version(["uvx", "--offline", "vulture", "--version"]),
    }
    ruff = _command(
        [
            "uvx", "--offline", "ruff", "check", "src", "scripts", "main.py",
            "--select", "F401,F841", "--output-format", "json", "--exit-zero", "--no-cache",
        ]
    )
    vulture = _command(
        [
            "uvx", "--offline", "vulture", "src", "scripts", "main.py",
            "--min-confidence", "90", "--exclude", "*/migrations/*",
        ],
        accepted_returncodes=(0, 1, 3),
    )
    with tempfile.TemporaryDirectory(prefix="aiops-dead-code-audit-") as temp_dir:
        deptry_output = Path(temp_dir) / "deptry.json"
        deptry = _command(
            [
                "uvx", "--offline", "deptry", "src", "scripts",
                "--requirements-files", "requirements.txt,requirements.runtime.in",
                "--known-first-party", "src", "--no-ansi", "--json-output", str(deptry_output),
            ],
            accepted_returncodes=(0, 1),
        )
        if not deptry_output.is_file():
            raise AuditError(f"deptry_output_missing:{deptry.stderr.strip()}")
        deptry_findings = _normalize_json(_parse_json_output(
            deptry_output.read_text(encoding="utf-8"), "deptry"
        ))
    knip = _command(
        [
            str(_knip_binary()), "--production", "--reporter", "json",
            "--no-progress", "--no-exit-code", "--no-config-hints",
        ],
        cwd=FRONTEND,
    )
    knip_findings = _normalize_json(_parse_json_output(knip.stdout, "knip"))
    candidates = {
        "deptry": deptry_findings,
        "knip": knip_findings,
        "ruff": _normalize_ruff(ruff.stdout),
        "vulture": _normalize_vulture(vulture.stdout, allowlist),
    }
    return versions, candidates


def build_report(*, entrypoints_only: bool) -> dict[str, Any]:
    entrypoints = collect_entrypoints()
    report: dict[str, Any] = {
        "entrypoints": entrypoints,
        "gitSha": _git_sha(),
        "mode": "entrypoints-only" if entrypoints_only else "full",
        "schemaVersion": 1,
    }
    if not entrypoints_only:
        allowlist = _load_allowlist()
        versions, candidates = run_tools(allowlist)
        report.update(
            {
                "allowlist": allowlist,
                "candidates": candidates,
                "tools": versions,
            }
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect-entrypoints-only", action="store_true")
    args = parser.parse_args()
    try:
        print(_canonical(build_report(entrypoints_only=args.collect_entrypoints_only)), end="")
    except (AuditError, subprocess.CalledProcessError) as exc:
        print(f"dead_code_audit_failed:{exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
