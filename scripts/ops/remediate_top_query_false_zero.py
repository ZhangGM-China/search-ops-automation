#!/usr/bin/env python3
import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_env
from src.top_query.remediation import FalseZeroRemediator, RemediationValidationError
from src.top_query.service import TopQueryGovernanceService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ZHA-41 Top Query 历史假 0 任务只读识别与受控处置",
    )
    parser.add_argument("--mode", choices=["report", "invalidate", "rerun"], default="report")
    parser.add_argument("--fixed-at", help="修复部署时间，ISO-8601；缺失时只能生成只读 report")
    parser.add_argument("--task-id", action="append", type=int, dest="task_ids")
    parser.add_argument("--manifest", help="读取已有 manifest JSON")
    parser.add_argument("--output", help="将 report/result 写入 JSON 文件")
    parser.add_argument("--apply", action="store_true", help="执行 replacement 事务；默认仅预览")
    parser.add_argument("--operator")
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument("--confirm-manifest-digest")
    parser.add_argument("--confirm-prod", action="store_true")
    parser.add_argument("--change-ticket")
    parser.add_argument("--maintenance-window-confirmed", action="store_true")
    parser.add_argument("--allow-external-actions", action="store_true")
    parser.add_argument("--confirm-external-writes")
    return parser


def validate_apply_options(
    args: argparse.Namespace,
    *,
    env: str,
    manifest_digest: str,
    manifest_fixed_at: Optional[str] = None,
) -> None:
    if not args.apply:
        return
    if args.mode == "report":
        raise RemediationValidationError("--apply requires --mode invalidate or rerun")
    if not args.fixed_at:
        raise RemediationValidationError("--apply requires explicit --fixed-at")
    if (
        manifest_fixed_at is not None
        and _parse_fixed_at(args.fixed_at) != _parse_fixed_at(manifest_fixed_at)
    ):
        raise RemediationValidationError("--fixed-at mismatch with manifest fixedAt")
    if not args.operator:
        raise RemediationValidationError("--apply requires --operator")
    if not isinstance(args.max_tasks, int) or args.max_tasks <= 0:
        raise RemediationValidationError("--apply requires positive --max-tasks")
    if args.confirm_manifest_digest != manifest_digest:
        raise RemediationValidationError("--confirm-manifest-digest mismatch")
    if env == "prod":
        if not args.confirm_prod:
            raise RemediationValidationError("production apply requires --confirm-prod")
        if args.change_ticket != "ZHA-41":
            raise RemediationValidationError("production apply requires --change-ticket ZHA-41")
        if not args.maintenance_window_confirmed:
            raise RemediationValidationError(
                "production apply requires --maintenance-window-confirmed"
            )
    if args.allow_external_actions:
        if args.mode != "rerun":
            raise RemediationValidationError("external actions are only valid for rerun mode")
        expected = f"ZHA-41:{manifest_digest}"
        if args.confirm_external_writes != expected:
            raise RemediationValidationError(
                f"external writes require --confirm-external-writes {expected}"
            )


def _parse_fixed_at(value: Optional[str]) -> datetime.datetime:
    if not value:
        return datetime.datetime.now()
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RemediationValidationError("--fixed-at must be ISO-8601") from exc


def _load_manifest(path: str) -> Dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemediationValidationError(f"cannot read manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise RemediationValidationError("manifest file must contain a JSON object")
    return payload


def _emit(payload: Dict[str, Any], output: Optional[str]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    remediator = FalseZeroRemediator()
    try:
        if args.manifest:
            manifest = _load_manifest(args.manifest)
        else:
            manifest = remediator.build_manifest(
                fixed_at=_parse_fixed_at(args.fixed_at),
                task_ids=args.task_ids,
            )
        preview = {
            "action": "report" if not args.apply else args.mode,
            "apply": bool(args.apply),
            "fixedAtExplicit": bool(args.fixed_at),
            "workflowDryRun": not args.allow_external_actions,
            "manifest": manifest,
        }
        if not args.apply:
            _emit(preview, args.output)
            return 0

        env = get_env()
        validate_apply_options(
            args,
            env=env,
            manifest_digest=str(manifest.get("digest") or ""),
            manifest_fixed_at=str(manifest.get("fixedAt") or ""),
        )
        result = remediator.apply_manifest(
            manifest,
            mode=args.mode,
            operator=args.operator,
            max_tasks=args.max_tasks,
            confirm_digest=args.confirm_manifest_digest,
        )
        reruns = []
        if args.mode == "rerun":
            service = TopQueryGovernanceService()
            for task_id in result["replacementTaskIds"]:
                reruns.append(service.run_task(
                    task_id,
                    dry_run=not args.allow_external_actions,
                ))
        _emit({**preview, "result": result, "reruns": reruns}, args.output)
        return 0
    except RemediationValidationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
