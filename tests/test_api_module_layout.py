"""Structural contracts for API assembly, application handlers and routers."""

from __future__ import annotations

import ast
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1] / "src" / "api"


def _source(path: str) -> str:
    return (API_ROOT / path).read_text(encoding="utf-8")


def _imported_src_packages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name.split(".")[1]
                for alias in node.names
                if alias.name.startswith("src.")
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 0 and node.module.startswith("src."):
                imported.add(node.module.split(".")[1])
    return imported


def test_api_assembly_is_not_a_route_or_persistence_module():
    app_source = _source("app.py")
    assert "FastAPI(" in app_source
    assert "include_router" in app_source
    assert "@app." not in app_source
    assert "SessionLocal" not in app_source
    assert "from src.persistence" not in app_source


def test_api_layers_have_explicit_application_and_router_modules():
    for path in (
        "lifecycle.py",
        "dependencies.py",
        "application/auth.py",
        "application/admin.py",
        "application/query_governance_legacy.py",
        "application/no_result.py",
        "application/assistant.py",
        "application/dashboard.py",
        "routers/auth.py",
        "routers/admin.py",
        "routers/query_governance.py",
        "routers/no_result.py",
        "routers/assistant.py",
        "routers/dashboard.py",
    ):
        assert (API_ROOT / path).is_file(), path


def test_router_modules_do_not_depend_on_persistence():
    router_root = API_ROOT / "routers"
    violations = {
        str(path.relative_to(API_ROOT)): sorted(_imported_src_packages(path) & {"persistence"})
        for path in router_root.glob("*.py")
        if path.name != "__init__.py" and "persistence" in _imported_src_packages(path)
    }
    assert not violations, violations
