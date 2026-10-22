#!/usr/bin/env python3
"""Run the inventory-recovery migration rehearsal against a test database.

Production execution is intentionally refused here.  Production schema/data
migration must be scheduled through the release process after an explicit
approval and a completed backup/restore rehearsal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import get_env
from src.persistence.session import get_engine
from src.migrations.inventory_recovery import (
    backfill_inventory_recovery,
    ensure_inventory_recovery_target_schema,
    get_inventory_recovery_migration_state,
    rollback_inventory_recovery,
    set_inventory_recovery_phase,
    validate_inventory_recovery_migration,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=("prepare", "backfill", "validate", "dual_write", "cutover", "rollback"),
    )
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    if get_env() != "test":
        raise SystemExit("库存恢复迁移演练脚本仅允许 ENV=test；生产迁移必须走发布流程")
    engine = get_engine()
    if args.phase == "prepare":
        result = ensure_inventory_recovery_target_schema(engine)
    elif args.phase == "backfill":
        result = backfill_inventory_recovery(engine, batch_size=args.batch_size)
    elif args.phase == "validate":
        result = validate_inventory_recovery_migration(engine)
    elif args.phase == "dual_write":
        result = set_inventory_recovery_phase(engine, "dual_write")
    elif args.phase == "cutover":
        result = set_inventory_recovery_phase(engine, "target_read")
    else:
        result = rollback_inventory_recovery(engine)
    print(json.dumps({"result": result, "state": get_inventory_recovery_migration_state(engine)}, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
