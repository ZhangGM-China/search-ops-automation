#!/usr/bin/env python3
"""Emit a canonical, strictly read-only inventory of retired experiments."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        from src.persistence import engine
        from src.migrations.legacy_experiment_retirement import inventory_legacy_experiments

    payload = inventory_legacy_experiments(engine)
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
