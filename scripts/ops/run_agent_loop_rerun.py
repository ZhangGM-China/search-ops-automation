#!/usr/bin/env python3
"""Retired legacy command; use query_governance_experiment_job instead."""

import sys


def main(argv: list[str] | None = None) -> int:
    print("top_query_agent_loop_retired: use query_governance_experiment_job", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

''' Legacy implementation retained below for source reference only.
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _set_default_env() -> None:
    defaults = {
        "ENV": "test",
        "TOP_QUERY_PLANNER_TIMEOUT_SECONDS": "5",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local Top Query agent loop rerun with tested timeout settings.")
    parser.add_argument("--date", required=True, help="Target data date in YYYY-MM-DD format.")
    parser.add_argument("--brand", help="Optional single brand filter.")
    parser.add_argument("--dry-run", action="store_true", help="Run without real writes.")
    args = parser.parse_args(argv)

    _set_default_env()

    from src.persistence import init_db
    from src.scheduler import agent_loop_governance_job

    target_date = dt.date.fromisoformat(args.date)
    init_db()
    result = agent_loop_governance_job(
        brand_filter=args.brand,
        target_date=target_date,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    print("deprecated: use scripts/ops/top_query.py run", file=sys.stderr)
    from scripts.ops.top_query import main as top_query_main

    raise SystemExit(top_query_main(["run", *sys.argv[1:]]))
'''
