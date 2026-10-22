"""Structural contract for the persistence package responsibilities."""

from __future__ import annotations

import ast
from pathlib import Path


PERSISTENCE = Path(__file__).resolve().parents[1] / "src" / "persistence"


def _module(path: str) -> ast.Module:
    return ast.parse((PERSISTENCE / path).read_text(encoding="utf-8"))


def _defined_names(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_persistence_responsibilities_are_split_into_dedicated_modules():
    models = _module("models.py")
    session = _module("session.py")
    schema = _module("schema.py")
    repositories = _module("repositories.py")
    database = _module("database.py")

    assert {"User", "SchedulerJobRun"} <= _defined_names(models)
    assert {"get_engine", "reset_engine_for_testing"} <= _defined_names(session)
    assert {"init_db", "init_admin_user", "_ensure_schema_upgrades"} <= _defined_names(schema)
    assert {"create_task_session", "save_operation_log"} <= _defined_names(repositories)
    assert not any(isinstance(node, ast.ClassDef) for node in database.body)
