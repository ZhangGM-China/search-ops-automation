#!/usr/bin/env python3
"""Check API route inventory drift and human-readable documentation coverage."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = PROJECT_ROOT / "docs/TSD/api/internal-api-inventory.md"
DOCS_PATH = PROJECT_ROOT / "docs/TSD/api"
GENERATOR_PATH = PROJECT_ROOT / "scripts/ci/generate_api_inventory.py"
ROW_PATTERN = re.compile(r"^\| \d+ \| ([A-Z]+) `([^`]+)` \|")
TOPIC_ROUTE_PATTERNS = (
    re.compile(r"\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`(/api/[^`]+)`"),
    re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b\s+`(/api/[^`]+)`"),
)


def _routes(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ROW_PATTERN.match(line)
        if match:
            rows.append((match.group(1), match.group(2)))
    return rows


def _documented_routes() -> set[tuple[str, str]]:
    documented: set[tuple[str, str]] = set()
    for path in DOCS_PATH.glob("*.md"):
        if path.name == INVENTORY_PATH.name:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            for pattern in TOPIC_ROUTE_PATTERNS:
                match = pattern.search(line)
                if match:
                    documented.add((match.group(1), match.group(2)))
                    break
    return documented


def _check_inventory_drift() -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="aiops-api-inventory-") as directory:
        generated = Path(directory) / "internal-api-inventory.md"
        result = subprocess.run(
            [
                sys.executable,
                str(GENERATOR_PATH),
                "--inventory-path",
                str(generated),
                "--metadata-path",
                str(INVENTORY_PATH),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            return False, result.stderr.strip() or result.stdout.strip()
        if generated.read_text(encoding="utf-8") != INVENTORY_PATH.read_text(encoding="utf-8"):
            return False, "checked-in API inventory differs from registered routes"
    return True, ""


def main() -> int:
    if not INVENTORY_PATH.is_file():
        print(f"API documentation check failed: missing {INVENTORY_PATH}", file=sys.stderr)
        return 1

    inventory_routes = _routes(INVENTORY_PATH)
    if len(inventory_routes) != len(set(inventory_routes)):
        print("API documentation check failed: duplicate method/path in inventory", file=sys.stderr)
        return 1

    inventory_ok, inventory_error = _check_inventory_drift()
    if not inventory_ok:
        print(f"API documentation check failed: {inventory_error}", file=sys.stderr)
        return 1

    missing = sorted(set(inventory_routes) - _documented_routes())
    if missing:
        print(
            "API documentation check failed: routes missing from topic documents: "
            + ", ".join(f"{method} {path}" for method, path in missing),
            file=sys.stderr,
        )
        return 1

    print(f"API documentation check passed: {len(inventory_routes)} routes covered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
