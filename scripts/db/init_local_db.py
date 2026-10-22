#!/usr/bin/env python3
import os
import sys
from pathlib import Path


def _require_test_env() -> bool:
    env = (os.getenv("ENV") or "test").strip().lower()
    if env != "test":
        print("blocked: init_local_db.py only runs with ENV=test", file=sys.stderr)
        return False
    os.environ["ENV"] = "test"
    return True


if not _require_test_env():
    raise SystemExit(2)


BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.config import get_env
from src.persistence import init_db


def main() -> int:
    env = get_env()
    print(f"🚀 [初始化] 正在准备初始化 {env.upper()} 环境的数据库结构...")
    try:
        init_db()
    except Exception as exc:
        print(f"❌ [失败] 数据库初始化过程中出现错误: {exc}", file=sys.stderr)
        return 1
    print(f"✅ [成功] {env.upper()} 环境数据库初始化完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
