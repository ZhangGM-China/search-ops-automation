"""生产环境正式迁移脚本。

用途：
1. 幂等创建当前版本缺失表
2. 幂等补齐生产库缺失字段与索引
3. 初始化管理员账号
4. 读取 ASSISTANT_BOOTSTRAP_JSON 预置品牌空间、默认 agent binding、管理员授权
"""
import json
import os
import sys
from pathlib import Path

from sqlalchemy import inspect

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_env
from src.persistence import SessionLocal, engine, init_admin_user
from src.migrations.release_schema import (
    apply_assistant_bootstrap,
    ensure_release_schema,
    mask_uri,
    parse_assistant_bootstrap,
    validate_release_schema_verification,
    verify_release_schema,
)


def _table_snapshot() -> dict:
    inspector = inspect(engine)
    table_names = sorted(inspector.get_table_names())
    focus_tables = [
        "assistant_brand_space",
        "assistant_agent_binding",
        "assistant_user_binding",
        "assistant_session_meta",
        "assistant_action_request",
    ]
    focus_columns = {}
    for table_name in focus_tables:
        if table_name not in table_names:
            continue
        focus_columns[table_name] = [column["name"] for column in inspector.get_columns(table_name)]
    return {
        "tables": table_names,
        "focusColumns": focus_columns,
    }


def _verification_summary(verification: dict) -> dict:
    def summarize(checks) -> dict:
        values = list(checks.values()) if isinstance(checks, dict) else []
        return {
            "passed": sum(value is True for value in values),
            "total": len(values),
        }

    column_checks = []
    columns = verification.get("columns")
    if isinstance(columns, dict):
        for checks in columns.values():
            if isinstance(checks, dict):
                column_checks.extend(
                    result.get("ok") is True if isinstance(result, dict) else False
                    for result in checks.values()
                )
    return {
        "ok": verification.get("ok") is True,
        "tables": summarize(verification.get("tables")),
        "primaryKeys": summarize(verification.get("primaryKeys")),
        "columns": {
            "passed": sum(column_checks),
            "total": len(column_checks),
        },
        "uniqueConstraints": summarize(verification.get("uniqueConstraints")),
        "indexes": summarize(verification.get("indexes")),
        "foreignKeys": summarize(verification.get("foreignKeys")),
    }


def _print_json(label: str, payload) -> None:
    print(f"{label}: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}")


def main() -> None:
    env = get_env()
    print("ENV:", env)
    print("DB_URI:", mask_uri(str(engine.url)))
    if env != "prod":
        raise SystemExit("blocked: migrate_prod_202607_assistant_top_query.py only runs with ENV=prod")

    try:
        before = _table_snapshot()
        _print_json("BEFORE_SCHEMA", before)

        migration_summary = ensure_release_schema(engine)
        verification = validate_release_schema_verification(
            verify_release_schema(engine)
        )
        verification_summary = _verification_summary(verification)
        _print_json("RELEASE_SCHEMA_CHANGES", migration_summary)
        _print_json("ZHA26_VERIFICATION", verification_summary)

        init_admin_user()
        bootstrap = parse_assistant_bootstrap(os.getenv("ASSISTANT_BOOTSTRAP_JSON"))
        bootstrap_summary = apply_assistant_bootstrap(SessionLocal, bootstrap)

        after = _table_snapshot()
        _print_json("AFTER_SCHEMA", after)
        _print_json("BOOTSTRAP_SUMMARY", bootstrap_summary)
    except Exception as exc:
        _print_json(
            "MIGRATION_ERROR",
            {"ok": False, "errorType": type(exc).__name__},
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
