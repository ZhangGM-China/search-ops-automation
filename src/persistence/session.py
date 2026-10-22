import os
import json
import logging
import math
from pathlib import Path
from sqlalchemy import CheckConstraint, create_engine, Column, Integer, String, Float, Date, DateTime, Text, ForeignKey, ForeignKeyConstraint, Index, func, inspect, text, UniqueConstraint
from sqlalchemy import event
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import close_all_sessions, declarative_base, relationship, sessionmaker
import datetime
import hashlib
import re
from typing import List, Any, Dict, Optional
from dotenv import load_dotenv

from src.config import get_env
from .mysql_timeouts import mysql_timeout_connect_args

load_dotenv()

logger = logging.getLogger(__name__)

from .models import Base

# ``session.py`` lives in ``src/persistence``; the repository root is two
# levels above ``src``.  Runtime SQLite paths are defined relative to that
# root so they stay aligned with ``.env.example`` and the checked-in data
# layout.
BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_TEST_DB_PATH = BASE_DIR / "data" / "local_db" / "search_ops_automation_test.db"
def _ensure_sqlite_parent_dir(uri: str) -> str:
    if uri.startswith("sqlite:///") and not uri.startswith("sqlite:///:memory:"):
        db_path = Path(uri.replace("sqlite:///", "", 1))
        if not db_path.is_absolute():
            db_path = (BASE_DIR / db_path).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path}"
    return uri


def _configure_sqlite_engine(engine_obj):
    if engine_obj.dialect.name != "sqlite":
        return engine_obj

    @event.listens_for(engine_obj, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 30000")
            cursor.execute("PRAGMA synchronous = NORMAL")
            if "mode=memory" not in str(getattr(engine_obj, "url", "")):
                cursor.execute("PRAGMA journal_mode = WAL")
        finally:
            cursor.close()
    return engine_obj

def get_engine():
    from src.config import get_env
    env = get_env()

    if env == "prod":
        uri = os.getenv("AUTO_DB_URI_PROD")
        print("\n" + "!" * 50)
        print("🚨 [生产模式] 正在连接生产环境数据库文件!")
        print("!" * 50 + "\n")
    else:
        uri = os.getenv("AUTO_DB_URI_TEST") or os.getenv("AUTO_DB_URI") or f"sqlite:///{DEFAULT_TEST_DB_PATH}"
        print(f"🧪 [测试模式] 正在连接测试环境数据库: {uri}")

    if not uri:
        raise ValueError(f"未能获取到 {env} 环境的数据库配置 (AUTO_DB_URI_{env.upper()})")

    uri = _ensure_sqlite_parent_dir(uri)

    # 增加稳定性参数，解决 "MySQL server has gone away" 问题
    if "mysql" in uri:
        return create_engine(
            uri,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=10,
            max_overflow=20,
            connect_args=mysql_timeout_connect_args("AUTO_DB"),
        )
    sqlite_engine = create_engine(
        uri,
        # Query governance fans out independent network/database operations
        # across worker threads. SQLite connections must therefore permit
        # cross-thread use; WAL and the busy timeout preserve write safety.
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    return _configure_sqlite_engine(sqlite_engine)

engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def reset_engine_for_testing():
    """单元测试专用：重新绑定内存数据库"""
    global engine, SessionLocal
    from sqlalchemy.pool import StaticPool
    if engine is not None:
        close_all_sessions()
        engine.dispose()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    SessionLocal.configure(bind=engine)
    return engine
