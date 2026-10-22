#!/usr/bin/env python3
"""Retired legacy resume command; use query_governance_experiment_job instead."""

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


def _should_resume_task(status: str | None, display_status: str | None = None) -> bool:
    normalized = str(status or "pending").strip().lower()
    normalized_display = str(display_status or "").strip().lower()
    return normalized not in {"completed", "manual_required", "superseded"} and normalized_display != "superseded"


def _set_default_env() -> None:
    defaults = {
        "ENV": "test",
        "TOP_QUERY_PLANNER_TIMEOUT_SECONDS": "5",
        "TOP_QUERY_WORLD_KNOWLEDGE_TIMEOUT_SECONDS": "8",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resume unfinished Top Query rerun tasks for a target date.")
    parser.add_argument("--date", required=True, help="Target data date in YYYY-MM-DD format.")
    parser.add_argument("--brand", help="Optional single brand filter.")
    parser.add_argument("--dry-run", action="store_true", help="Run remaining tasks in dry-run mode.")
    args = parser.parse_args(argv)

    _set_default_env()

    from src.config import BRAND_CONFIGS, get_brand_business_config, is_brand_enabled, reload_config
    from src.persistence import SessionLocal, TopQueryTask, create_task_session, update_task_session
    from src.runtime.process_guard import ProcessLockError, acquire_process_lock
    from src.top_query.service import TopQueryGovernanceService

    target_date = dt.date.fromisoformat(args.date)
    lock_name = f"resume_agent_loop_rerun:{args.date}:{args.brand or 'all'}"
    try:
        acquire_process_lock(lock_name, metadata={"date": args.date, "brand": args.brand or "all"})
    except ProcessLockError as exc:
        print(f"[resume] duplicate_run_blocked lock={lock_name} error={exc}", flush=True)
        return 2

    reload_config()
    service = TopQueryGovernanceService()
    session_id = create_task_session("agent_loop_governance_job")
    summary = {"target_date": target_date.isoformat(), "dry_run": args.dry_run, "brands": {}}

    update_task_session(session_id, phase="Preparing_Resumable_Top_Query_Tasks")
    brands = [args.brand] if args.brand else [brand for brand in BRAND_CONFIGS.keys() if is_brand_enabled(brand)]
    print(f"[resume] session_id={session_id} target_date={target_date} brands={brands}", flush=True)

    try:
        for brand in brands:
            if not is_brand_enabled(brand):
                print(f"[resume] skip brand={brand} reason=disabled", flush=True)
                continue
            business_config = get_brand_business_config(brand)
            if business_config.get("top_query_governance_enabled") is False:
                print(f"[resume] skip brand={brand} reason=top_query_disabled", flush=True)
                continue

            limit = int(business_config.get("query_governance_extract_limit") or 150)
            print(f"[resume] create_daily_tasks brand={brand} limit={limit}", flush=True)
            created = service.create_daily_tasks(brand, target_date, limit=limit)

            db = SessionLocal()
            try:
                tasks = db.query(TopQueryTask).filter(
                    TopQueryTask.brand == brand,
                    TopQueryTask.data_date == target_date,
                ).order_by(TopQueryTask.query_rank.asc(), TopQueryTask.id.asc()).all()
                remaining = [
                    {"id": task.id, "query": task.query_text, "status": str(task.status or "pending")}
                    for task in tasks
                    if _should_resume_task(task.status, task.display_status)
                ]
            finally:
                db.close()

            brand_summary = {
                "created": len(created.get("task_ids", [])),
                "to_process": len(remaining),
                "completed": 0,
                "manual_required": 0,
                "failed": 0,
                "skipped": 0,
            }
            summary["brands"][brand] = brand_summary
            update_task_session(session_id, phase=f"Running_Agent_Loop:{brand}")
            print(f"[resume] brand={brand} remaining={len(remaining)}", flush=True)

            for index, row in enumerate(remaining, start=1):
                print(
                    f"[resume] brand={brand} {index}/{len(remaining)} task_id={row['id']} "
                    f"status={row['status']} query={row['query']}",
                    flush=True,
                )
                try:
                    result = service.run_task(row["id"], dry_run=args.dry_run)
                    final_status = result.get("final_status", "failed")
                    if final_status in brand_summary:
                        brand_summary[final_status] += 1
                    else:
                        brand_summary["failed"] += 1
                    print(
                        f"[resume] brand={brand} task_id={row['id']} final_status={final_status}",
                        flush=True,
                    )
                except Exception as exc:
                    brand_summary["failed"] += 1
                    brand_summary.setdefault("errors", []).append({"task_id": row["id"], "error": str(exc)})
                    print(f"[resume] brand={brand} task_id={row['id']} error={exc}", flush=True)

        update_task_session(session_id, phase="Finished", status="success", summary=summary)
        print("[resume] done", flush=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), flush=True)
        return 0
    except Exception as exc:
        update_task_session(session_id, status="failed", error=str(exc), summary=summary)
        print(f"[resume] fatal_error={exc}", flush=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), flush=True)
        raise


if __name__ == "__main__":
    print("deprecated: use scripts/ops/top_query.py resume", file=sys.stderr)
    from scripts.ops.top_query import main as top_query_main

    raise SystemExit(top_query_main(["resume", *sys.argv[1:]]))
'''
