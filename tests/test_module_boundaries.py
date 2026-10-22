"""Long-lived, dependency-free guards for the backend package directions."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

# Keep this list deliberately small. Existing behavior tests cover contracts;
# this test only prevents the three dependency directions that make the target
# package structure collapse back into a root-level tangle.
FORBIDDEN_IMPORTS = {
    "api/routers": {"persistence"},
    "integrations": {"api", "top_query", "no_result"},
    "persistence": {"api", "top_query", "no_result", "scheduler"},
}


def _source_files(package_path: str) -> list[Path]:
    package_root = SRC_ROOT / package_path
    return sorted(
        path
        for path in package_root.rglob("*.py")
        if path.name != "__init__.py"
    )


def _imported_src_packages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src."):
                    imported.add(alias.name.split(".")[1])
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 0 and node.module.startswith("src."):
                imported.add(node.module.split(".")[1])
    return imported


@pytest.mark.parametrize(
    ("package_path", "forbidden"),
    FORBIDDEN_IMPORTS.items(),
)
def test_module_directions_do_not_regress(package_path: str, forbidden: set[str]) -> None:
    sources = _source_files(package_path)
    assert sources, f"expected guarded source files below src/{package_path}"

    violations = {
        str(path.relative_to(SRC_ROOT)): sorted(_imported_src_packages(path) & forbidden)
        for path in sources
        if _imported_src_packages(path) & forbidden
    }
    assert not violations, f"forbidden module dependency direction: {violations}"
