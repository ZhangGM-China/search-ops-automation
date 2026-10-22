"""ZHA-36 A/B Test v2 基线字段迁移入口，仅允许隔离测试库执行。"""
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FOCUS_TABLES = (
    "top_query_experiment",
    "top_query_experiment_sample",
)


def main() -> None:
    env = (os.getenv("ENV") or "test").lower()
    print("ENV:", env)
    if env != "test":
        message = (
            "blocked: migrate_test_202607_zha36_ab_test_effect.py "
            "requires ENV=test"
        )
        raise SystemExit(message)

    migration_uri = os.getenv("ZHA36_AB_TEST_MIGRATION_DB_URI")
    if not migration_uri:
        raise SystemExit(
            "blocked: migrate_test_202607_zha36_ab_test_effect.py "
            "requires explicit ZHA36_AB_TEST_MIGRATION_DB_URI"
        )

    from sqlalchemy import create_engine, inspect
    from src.migrations.experiment_schema import (
        ensure_zha36_ab_test_schema,
        validate_zha36_test_migration_target,
    )

    try:
        migration_url = validate_zha36_test_migration_target(migration_uri)
    except RuntimeError as exc:
        raise SystemExit(f"blocked: {exc}") from exc

    os.environ["AUTO_DB_URI_TEST"] = "sqlite://"
    engine = create_engine(migration_uri)

    def snapshot() -> dict:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        return {
            table: [column["name"] for column in inspector.get_columns(table)]
            for table in FOCUS_TABLES
            if table in tables
        }

    print("DB_URI:", migration_url.render_as_string(hide_password=True))
    print("BEFORE_COLUMNS:", json.dumps(snapshot(), ensure_ascii=False))
    result = ensure_zha36_ab_test_schema(engine)
    print(
        "ADDED_COLUMNS:",
        json.dumps(result["addedColumns"], ensure_ascii=False),
    )
    print(
        "LEGACY_COUNTS:",
        json.dumps(result["legacyCounts"], ensure_ascii=False),
    )
    print("AFTER_COLUMNS:", json.dumps(snapshot(), ensure_ascii=False))


if __name__ == "__main__":
    main()
