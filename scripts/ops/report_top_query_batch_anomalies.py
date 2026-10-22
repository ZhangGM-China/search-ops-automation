#!/usr/bin/env python3
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _set_default_env() -> None:
    os.environ.setdefault("ENV", "test")


def _normalize_status(value: Any) -> str:
    return str(value or "").strip().lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report suspicious Top Query task rows for one batch date.")
    parser.add_argument("--date", required=True, help="Target data date in YYYY-MM-DD format.")
    parser.add_argument("--brand", help="Optional single brand filter.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum suspicious rows to print.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    _set_default_env()

    from src.persistence import SessionLocal, TopQueryAgentEvent, TopQueryStepStatus, TopQueryTask
    from src.top_query.constants import CHECK_ITEM_KEYS

    target_date = date.fromisoformat(args.date)
    db = SessionLocal()
    try:
        task_query = db.query(TopQueryTask).filter(TopQueryTask.data_date == target_date)
        if args.brand:
            task_query = task_query.filter(TopQueryTask.brand == args.brand)
        tasks = task_query.order_by(TopQueryTask.brand.asc(), TopQueryTask.query_rank.asc(), TopQueryTask.id.asc()).all()
        if not tasks:
            payload = {"date": args.date, "brand": args.brand, "count": 0, "rows": []}
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else "No tasks found.")
            return 0

        task_ids = [task.id for task in tasks]

        step_statuses_by_task: dict[int, dict[str, str]] = defaultdict(dict)
        for row in db.query(TopQueryStepStatus).filter(TopQueryStepStatus.task_id.in_(task_ids)).order_by(TopQueryStepStatus.task_id.asc(), TopQueryStepStatus.id.desc()).all():
            task_steps = step_statuses_by_task[int(row.task_id)]
            task_steps.setdefault(str(row.check_key), _normalize_status(row.status))

        event_counts_by_task: dict[int, int] = defaultdict(int)
        for row in db.query(TopQueryAgentEvent.task_id).filter(TopQueryAgentEvent.task_id.in_(task_ids)).all():
            event_counts_by_task[int(row.task_id)] += 1

        suspicious_rows: list[dict[str, Any]] = []
        summary_counts: dict[str, int] = defaultdict(int)
        for task in tasks:
            step_statuses = step_statuses_by_task.get(int(task.id), {})
            event_count = event_counts_by_task.get(int(task.id), 0)

            problem_tags: list[str] = []
            if not step_statuses:
                problem_tags.append("no_step_status_rows")
            else:
                missing_step_checks = [key for key in CHECK_ITEM_KEYS if key not in step_statuses]
                if missing_step_checks:
                    problem_tags.append(f"missing_step_checks:{','.join(missing_step_checks)}")
            if event_count == 0:
                problem_tags.append("no_agent_events")

            if not problem_tags:
                continue

            for tag in problem_tags:
                summary_counts[tag] += 1

            suspicious_rows.append(
                {
                    "taskId": int(task.id),
                    "brand": task.brand,
                    "query": task.query_text,
                    "queryRank": task.query_rank,
                    "status": task.status,
                    "displayStatus": task.display_status,
                    "updatedAt": task.updated_at.isoformat(sep=" ", timespec="seconds") if task.updated_at else None,
                    "problemTags": problem_tags,
                    "stepStatuses": step_statuses,
                    "agentEventCount": event_count,
                }
            )

        suspicious_rows = suspicious_rows[: max(args.limit, 1)]
        payload = {
            "date": args.date,
            "brand": args.brand,
            "count": len(suspicious_rows),
            "summary": dict(sorted(summary_counts.items())),
            "rows": suspicious_rows,
        }

        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        print(f"date={args.date} brand={args.brand or 'all'} suspicious={payload['count']}")
        if payload["summary"]:
            print("summary:")
            for key, value in payload["summary"].items():
                print(f"  - {key}: {value}")
        for row in suspicious_rows:
            print(
                f"[task_id={row['taskId']}] {row['brand']} / rank={row['queryRank']} / "
                f"status={row['status']} / score={row['totalScore']} / query={row['query']}"
            )
            print(f"  problem_tags={','.join(row['problemTags'])}")
            print(f"  dimension_statuses={json.dumps(row['dimensionStatuses'], ensure_ascii=False, sort_keys=True)}")
            print(f"  step_statuses={json.dumps(row['stepStatuses'], ensure_ascii=False, sort_keys=True)}")
            print(f"  agent_event_count={row['agentEventCount']} updated_at={row['updatedAt']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    print("deprecated: use scripts/ops/top_query.py report", file=sys.stderr)
    from scripts.ops.top_query import main as top_query_main

    raise SystemExit(top_query_main(["report", *sys.argv[1:]]))
