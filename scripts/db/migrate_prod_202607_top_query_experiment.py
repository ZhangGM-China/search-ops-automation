"""生产环境 Top Query 品牌 A/B 实验模式迁移脚本。

用途：
1. 幂等创建实验相关新表
2. 幂等补齐实验相关索引
3. 打印迁移前后快照，便于发版核对
"""
import json
import sys
from pathlib import Path

from sqlalchemy import inspect

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_env
from src.persistence import engine
from src.migrations.release_schema import ensure_release_schema, mask_uri


FOCUS_TABLES = [
    "top_query_experiment",
    "top_query_experiment_sample",
    "top_query_experiment_daily_metric",
]


def _snapshot() -> dict:
    inspector = inspect(engine)
    table_names = sorted(inspector.get_table_names())
    focus_columns = {}
    focus_indexes = {}
    for table_name in FOCUS_TABLES:
        if table_name not in table_names:
            continue
        focus_columns[table_name] = [column["name"] for column in inspector.get_columns(table_name)]
        focus_indexes[table_name] = [index["name"] for index in inspector.get_indexes(table_name)]
    return {
        "tables": table_names,
        "focusColumns": focus_columns,
        "focusIndexes": focus_indexes,
    }


def main() -> None:
    env = get_env()
    print("ENV:", env)
    print("DB_URI:", mask_uri(str(engine.url)))
    if env != "prod":
        raise SystemExit("blocked: migrate_prod_202607_top_query_experiment.py only runs with ENV=prod")

    before = _snapshot()
    print("BEFORE_TABLES:", json.dumps(before["tables"], ensure_ascii=False))
    print("BEFORE_FOCUS_COLUMNS:", json.dumps(before["focusColumns"], ensure_ascii=False))
    print("BEFORE_FOCUS_INDEXES:", json.dumps(before["focusIndexes"], ensure_ascii=False))

    ensure_release_schema(engine)

    after = _snapshot()
    print("AFTER_TABLES:", json.dumps(after["tables"], ensure_ascii=False))
    print("AFTER_FOCUS_COLUMNS:", json.dumps(after["focusColumns"], ensure_ascii=False))
    print("AFTER_FOCUS_INDEXES:", json.dumps(after["focusIndexes"], ensure_ascii=False))


if __name__ == "__main__":
    main()
