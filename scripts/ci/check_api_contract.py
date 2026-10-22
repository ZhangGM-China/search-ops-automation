#!/usr/bin/env python3
"""Fail closed when the checked-in external OpenAPI contract drifts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api import app  # noqa: E402
from src.api.contracts import build_external_openapi  # noqa: E402


DEFAULT_CONTRACT_PATH = PROJECT_ROOT / "docs/TSD/api/openapi.json"


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the checked-in external OpenAPI contract for drift.",
    )
    parser.add_argument(
        "--contract-path",
        type=Path,
        default=DEFAULT_CONTRACT_PATH,
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    contract_path = args.contract_path
    if not contract_path.is_absolute():
        contract_path = PROJECT_ROOT / contract_path

    try:
        checked_in = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"API contract drift check failed: unable to read {contract_path}: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1

    runtime = build_external_openapi(app)
    if checked_in != runtime:
        print(
            "API contract drift detected: "
            f"checked_in_sha256={_canonical_sha256(checked_in)} "
            f"runtime_sha256={_canonical_sha256(runtime)}",
            file=sys.stderr,
        )
        return 1

    print(f"API contract drift check passed: {contract_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
