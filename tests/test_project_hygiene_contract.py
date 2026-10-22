"""Prevent generated files and retired project material from returning to the tree."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path],
        cwd=ROOT,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode != 128:
        return False

    candidate = Path(path)
    for parent in candidate.parents:
        if parent == Path("."):
            break
        if parent.is_symlink():
            return _is_ignored(parent.as_posix())
    return False


def _tracked_ignored_paths() -> list[str]:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "-ci", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


@pytest.mark.parametrize(
    "path",
    (
        "AGENTS.md",
        ".env.example",
        ".codex/environments/environment.toml",
        "docs/TSD.md",
        "scripts/ci/test_frontend.sh",
        "src/api/app.py",
        "tests/test_mainline_retirement_contract.py",
        "frontend/e2e/dashboard.spec.ts",
        "frontend/playwright.config.ts",
    ),
)
def test_versioned_project_material_is_not_ignored(path: str):
    assert not _is_ignored(path), path


@pytest.mark.parametrize(
    "path",
    (
        ".env",
        "config.yaml",
        "venv/bin/python",
        "frontend/node_modules/.bin/vite",
        "frontend/test-results/failure.png",
        ".pytest_cache/v/cache/nodeids",
        "logs/scheduler.log",
        "data/runtime/main_scheduler.lock",
        "output/report.csv",
    ),
)
def test_local_runtime_artifacts_remain_ignored(path: str):
    assert _is_ignored(path), path


@pytest.mark.parametrize(
    "path",
    (
        "docs/TSD/03-数据存储设计.md",
        "docs/api/01-外部业务系统接口.md",
        "docs/api/02-外部模型与第三方依赖.md",
        "docs/api/09-知识图谱系统接口.md",
    ),
)
def test_credential_governance_documents_remain_outside_git_until_their_separate_review(path: str):
    assert _is_ignored(path), path


def test_only_explicitly_allowed_files_are_tracked_and_ignored():
    assert _tracked_ignored_paths() == []


@pytest.mark.parametrize(
    "path",
    (
        "frontend/failure-should-display-the-abnormal-queries-page-and-filter-data.png",
        "frontend/failure-should-navigate-to-blacklist-logs-and-verify-entries.png",
        "frontend/failure-should-show-tracking-trend-modal.png",
        "frontend/failure-should-verify-abnormal-queries-and-tracking-trend.png",
        "frontend/public/assistant-mascot.png",
        "frontend/public/icons.svg",
        "frontend/src/assets/hero.png",
        "frontend/src/assets/react.svg",
        "frontend/src/assets/vite.svg",
        "frontend/README.md",
        "data/local_db/atlas_top_query_ops_team_demo.db",
    ),
)
def test_retired_frontend_and_demo_artifacts_are_absent(path: str):
    assert not (ROOT / path).exists(), path


@pytest.mark.parametrize(
    "path",
    (
        "output",
        "docs/superpowers",
        "docs/retirement",
        "docs/其他",
        "ISSUES_REAL_RUN_2026-07-04.md",
    ),
)
def test_retired_historical_material_is_absent_from_the_project(path: str):
    assert not (ROOT / path).exists(), path


def test_repository_memory_is_archived_outside_the_project():
    assert not (ROOT / "MEMORY.md").exists()


@pytest.mark.parametrize("path", ("CLAUDE.md", "GEMINI.md"))
def test_client_compatibility_files_delegate_to_agents(path: str):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert "[AGENTS.md](AGENTS.md)" in source
    assert len(source) < 500


@pytest.mark.parametrize(
    "path",
    (
        "frontend/e2e/dashboard.spec.ts",
        "frontend/e2e/operations-log.spec.ts",
    ),
)
def test_failed_e2e_screenshots_are_written_to_playwright_output(path: str):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert "testInfo.outputPath(" in source
    assert "path: `failure-" not in source


def test_readme_describes_the_rebuildable_local_database_not_a_demo_database():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "search_ops_automation_test.db" in readme
    assert "atlas_top_query_ops_team_demo.db" not in readme


def test_env_example_uses_the_default_rebuildable_test_database():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "AUTO_DB_URI_TEST=sqlite:///data/local_db/search_ops_automation_test.db" in env_example
    assert "AUTO_DB_URI=sqlite:///data/local_db/search_ops_automation_test.db" in env_example
