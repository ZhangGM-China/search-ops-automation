import os
import json
import logging
import math
from pathlib import Path
from sqlalchemy import CheckConstraint, create_engine, Column, Integer, String, Float, Date, DateTime, Text, ForeignKey, ForeignKeyConstraint, Index, func, inspect, text, UniqueConstraint
from sqlalchemy import event
from sqlalchemy.schema import CreateTable
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

from . import session as _session
from .models import *  # noqa: F401,F403

logger = logging.getLogger(__name__)

TEST_DEFAULT_ADMIN_PASSWORD = "test-admin-password"
DEFAULT_ADMIN_PAGE_PERMISSIONS = [
    "/",
    "/ops-dashboard",
    "/smart-assistant",
    "/blacklist",
    "/demotion",
    "/stock-tracking",
    "/no-result-governance",
    "/account-management",
    "/config",
]
DEFAULT_ADMIN_BRAND_PERMISSIONS = ["atlas", "meadow", "harbor", "summit", "riviera", "lumen"]

def get_initial_admin_password() -> str:
    from src.config import get_env
    if get_env() != "prod":
        return TEST_DEFAULT_ADMIN_PASSWORD

    password = os.getenv("INITIAL_ADMIN_PASSWORD") or os.getenv("ADMIN_INITIAL_PASSWORD")
    if password:
        return password
    raise ValueError("生产环境必须配置 INITIAL_ADMIN_PASSWORD")

def init_db():
    print("正在连接数据库并创建表结构...")
    # Legacy Top Query ORM classes remain importable as reference code, but
    # they must not recreate retired tables when the application starts.
    from src.query_governance.database import active_schema_tables

    Base.metadata.create_all(bind=_session.engine, tables=active_schema_tables())
    _ensure_schema_upgrades()
    print("表结构创建成功。")
    init_admin_user()


def _ensure_sqlite_indexes(conn) -> None:
    sqlite_indexes = {
        "ix_brand_daily_metric_brand_data_date_id": (
            "brand_daily_metric",
            "CREATE INDEX IF NOT EXISTS ix_brand_daily_metric_brand_data_date_id "
            "ON brand_daily_metric (brand, data_date, id)",
        ),
    }
    for _index_name, (table_name, ddl) in sqlite_indexes.items():
        table_rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        if table_rows:
            conn.execute(text(ddl))


def _ensure_mysql_indexes(conn) -> None:
    mysql_indexes = {
        "ix_brand_daily_metric_brand_data_date_id": (
            "brand_daily_metric",
            "CREATE INDEX ix_brand_daily_metric_brand_data_date_id "
            "ON brand_daily_metric (brand, data_date, id)",
        ),
    }
    existing_rows = conn.execute(text("""
        SELECT TABLE_NAME, INDEX_NAME
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME IN (
              'brand_daily_metric'
          )
    """)).fetchall()
    existing = {(row[0], row[1]) for row in existing_rows}
    for index_name, (table_name, ddl) in mysql_indexes.items():
        if (table_name, index_name) not in existing:
            conn.execute(text(ddl))

def _ensure_schema_upgrades():
    """补齐 create_all 不会自动添加的轻量兼容字段。"""
    if _session.engine.dialect.name == "sqlite":
        with _session.engine.begin() as conn:
            user_rows = conn.execute(text("PRAGMA table_info(users)")).fetchall()
            user_columns = {row[1] for row in user_rows}
            if user_rows and "role" not in user_columns:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN role VARCHAR(30) DEFAULT '{BRAND_MEMBER_ROLE}'"))
            if user_rows and "action_permissions" not in user_columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN action_permissions TEXT"))
            binding_rows = conn.execute(text("PRAGMA table_info(assistant_agent_binding)")).fetchall()
            binding_columns = {row[1] for row in binding_rows}
            if binding_rows and "model_name" not in binding_columns:
                conn.execute(text("ALTER TABLE assistant_agent_binding ADD COLUMN model_name VARCHAR(120)"))
            if binding_rows and "available_models_json" not in binding_columns:
                conn.execute(text("ALTER TABLE assistant_agent_binding ADD COLUMN available_models_json TEXT"))
            if binding_rows and "supports_model_switch" not in binding_columns:
                conn.execute(text("ALTER TABLE assistant_agent_binding ADD COLUMN supports_model_switch INTEGER DEFAULT 0"))
            inventory_rows = conn.execute(text("PRAGMA table_info(inventory_recovery_task)")).fetchall()
            inventory_columns = {row[1] for row in inventory_rows}
            action_rows = conn.execute(text("PRAGMA table_info(automation_action_log)")).fetchall()
            action_columns = {row[1] for row in action_rows}
            if action_rows and "image_url" not in action_columns:
                conn.execute(text("ALTER TABLE automation_action_log ADD COLUMN image_url TEXT"))
            if inventory_rows and "last_action_request_id" not in inventory_columns:
                conn.execute(text("ALTER TABLE inventory_recovery_task ADD COLUMN last_action_request_id VARCHAR(255)"))
            if inventory_rows and "last_action_generation" not in inventory_columns:
                conn.execute(text("ALTER TABLE inventory_recovery_task ADD COLUMN last_action_generation INTEGER NOT NULL DEFAULT 0"))
            query_metric_rows = conn.execute(text("PRAGMA table_info(query_metric_snapshot)")).fetchall()
            query_metric_columns = {row[1] for row in query_metric_rows}
            if query_metric_rows and "add_to_cart_uv" not in query_metric_columns:
                conn.execute(text("ALTER TABLE query_metric_snapshot ADD COLUMN add_to_cart_uv FLOAT NOT NULL DEFAULT 0"))
            session_meta_rows = conn.execute(text("PRAGMA table_info(assistant_session_meta)")).fetchall()
            session_meta_columns = {row[1] for row in session_meta_rows}
            if session_meta_rows and "parent_session_id" not in session_meta_columns:
                conn.execute(text("ALTER TABLE assistant_session_meta ADD COLUMN parent_session_id VARCHAR(120)"))
            _migrate_legacy_assistant_model_channel(conn)
            _ensure_sqlite_indexes(conn)
        return
    if _session.engine.dialect.name in {"mysql", "mariadb"}:
        with _session.engine.begin() as conn:
            user_rows = conn.execute(text("""
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'users'
            """)).fetchall()
            user_columns = {row[0] for row in user_rows}
            if user_rows and "role" not in user_columns:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN role VARCHAR(30) DEFAULT '{BRAND_MEMBER_ROLE}'"))
            if user_rows and "action_permissions" not in user_columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN action_permissions TEXT"))
            binding_rows = conn.execute(text("""
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'assistant_agent_binding'
            """)).fetchall()
            binding_columns = {row[0] for row in binding_rows}
            if binding_rows and "model_name" not in binding_columns:
                conn.execute(text("ALTER TABLE assistant_agent_binding ADD COLUMN model_name VARCHAR(120)"))
            if binding_rows and "available_models_json" not in binding_columns:
                conn.execute(text("ALTER TABLE assistant_agent_binding ADD COLUMN available_models_json TEXT"))
            if binding_rows and "supports_model_switch" not in binding_columns:
                conn.execute(text("ALTER TABLE assistant_agent_binding ADD COLUMN supports_model_switch INTEGER DEFAULT 0"))
            inventory_rows = conn.execute(text("""
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'inventory_recovery_task'
            """)).fetchall()
            inventory_columns = {row[0] for row in inventory_rows}
            query_metric_rows = conn.execute(text("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'query_metric_snapshot'
            """)).fetchall()
            query_metric_columns = {row[0] for row in query_metric_rows}
            action_rows = conn.execute(text("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'automation_action_log'
            """)).fetchall()
            action_columns = {row[0] for row in action_rows}
            if action_rows and "image_url" not in action_columns:
                conn.execute(text("ALTER TABLE automation_action_log ADD COLUMN image_url TEXT"))
            if inventory_rows and "last_action_request_id" not in inventory_columns:
                conn.execute(text("ALTER TABLE inventory_recovery_task ADD COLUMN last_action_request_id VARCHAR(255)"))
            if inventory_rows and "last_action_generation" not in inventory_columns:
                conn.execute(text("ALTER TABLE inventory_recovery_task ADD COLUMN last_action_generation INTEGER NOT NULL DEFAULT 0"))
            if query_metric_rows and "add_to_cart_uv" not in query_metric_columns:
                conn.execute(text("ALTER TABLE query_metric_snapshot ADD COLUMN add_to_cart_uv FLOAT NOT NULL DEFAULT 0"))
            task_snapshot_rows = conn.execute(text("""
                SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'query_governance_task'
                  AND COLUMN_NAME = 's0_snapshot'
            """)).fetchall()
            # Super-generic queries recall tens of thousands of product ids;
            # the 64KB TEXT column overflows and fails those Queries daily.
            if task_snapshot_rows and str(task_snapshot_rows[0][0]).lower() != "mediumtext":
                conn.execute(text("ALTER TABLE query_governance_task MODIFY COLUMN s0_snapshot MEDIUMTEXT"))
            session_meta_rows = conn.execute(text("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'assistant_session_meta'
            """)).fetchall()
            if session_meta_rows and "parent_session_id" not in {row[0] for row in session_meta_rows}:
                conn.execute(text("ALTER TABLE assistant_session_meta ADD COLUMN parent_session_id VARCHAR(120)"))
            _migrate_legacy_assistant_model_channel(conn)
            _ensure_mysql_indexes(conn)


def _migrate_legacy_assistant_model_channel(conn) -> int:
    """Convert the retired FastClaw assistant channel in persisted bundles."""
    if conn.dialect.name == "sqlite":
        rows = conn.execute(text("PRAGMA table_info(node_model_config_entry)")).fetchall()
    else:
        rows = conn.execute(text("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'node_model_config_entry'
        """)).fetchall()
    if not rows:
        return 0
    if conn.dialect.name == "sqlite":
        table_sql = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'node_model_config_entry'"
        )).scalar()
        if isinstance(table_sql, str) and "'pi_agent'" not in table_sql:
            from .models import NodeModelConfigEntry

            temporary_name = "node_model_config_entry__pi_migration"
            create_sql = str(CreateTable(NodeModelConfigEntry.__table__).compile(dialect=conn.dialect))
            create_sql = create_sql.replace(
                'CREATE TABLE node_model_config_entry',
                f'CREATE TABLE {temporary_name}',
                1,
            )
            conn.execute(text(f'ALTER TABLE node_model_config_entry RENAME TO node_model_config_entry__legacy'))
            conn.execute(text(create_sql))
            conn.execute(text("""
                INSERT INTO node_model_config_entry__pi_migration
                    (id, version_id, node_key, channel, primary_model, fallback_model)
                SELECT id, version_id, node_key,
                    CASE WHEN channel = 'fastclaw' THEN 'pi_agent' ELSE channel END,
                    primary_model, fallback_model
                FROM node_model_config_entry__legacy
            """))
            result = conn.execute(text("SELECT COUNT(*) FROM node_model_config_entry__legacy WHERE node_key = 'assistant.chat' AND channel = 'fastclaw'"))
            migrated = int(result.scalar() or 0)
            conn.execute(text("DROP TABLE node_model_config_entry__legacy"))
            conn.execute(text("ALTER TABLE node_model_config_entry__pi_migration RENAME TO node_model_config_entry"))
            return migrated
    result = conn.execute(text("""
        UPDATE node_model_config_entry
        SET channel = 'pi_agent'
        WHERE node_key = 'assistant.chat' AND channel = 'fastclaw'
    """))
    return int(result.rowcount or 0)

def init_admin_user():
    """初始化管理员账号"""
    db = None
    failed = False
    try:
        from passlib.context import CryptContext

        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        db = _session.SessionLocal()
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            print("正在初始化管理员账号...")
            initial_admin_value = get_initial_admin_password()
            new_admin = User(
                username="admin",
                password_hash=pwd_context.hash(initial_admin_value),
                is_admin=1,
                role=PLATFORM_ADMIN_ROLE,
                page_permissions=json.dumps(DEFAULT_ADMIN_PAGE_PERMISSIONS),
                brand_permissions=json.dumps(DEFAULT_ADMIN_BRAND_PERMISSIONS),
                action_permissions=json.dumps(DEFAULT_PLATFORM_ADMIN_ACTION_PERMISSIONS),
            )
            db.add(new_admin)
            db.commit()
            print("管理员账号初始化完成。")
        else:
            changed = False
            from src.config import get_env
            if get_env() != "prod":
                expected_admin_value = get_initial_admin_password()
                try:
                    password_matches = pwd_context.verify(expected_admin_value, admin.password_hash)
                except (TypeError, ValueError):
                    password_matches = False
                if not password_matches:
                    admin.password_hash = pwd_context.hash(expected_admin_value)
                    changed = True
            if admin.is_admin and (admin.role or "") != PLATFORM_ADMIN_ROLE:
                admin.role = PLATFORM_ADMIN_ROLE
                changed = True
            try:
                current_pages = json.loads(admin.page_permissions or "[]")
            except json.JSONDecodeError:
                current_pages = []
            for page in DEFAULT_ADMIN_PAGE_PERMISSIONS:
                if page not in current_pages:
                    current_pages.append(page)
                    changed = True
            try:
                current_brands = json.loads(admin.brand_permissions or "[]")
            except json.JSONDecodeError:
                current_brands = []
            for brand in DEFAULT_ADMIN_BRAND_PERMISSIONS:
                if brand not in current_brands:
                    current_brands.append(brand)
                    changed = True
            try:
                current_actions = json.loads(admin.action_permissions or "[]")
            except json.JSONDecodeError:
                current_actions = []
            for action in DEFAULT_PLATFORM_ADMIN_ACTION_PERMISSIONS:
                if action not in current_actions:
                    current_actions.append(action)
                    changed = True
            if changed:
                admin.page_permissions = json.dumps(current_pages)
                admin.brand_permissions = json.dumps(current_brands)
                admin.action_permissions = json.dumps(current_actions)
                db.commit()
    except Exception:
        failed = True
    finally:
        if db is not None:
            if failed:
                try:
                    db.rollback()
                except Exception:
                    pass
            try:
                db.close()
            except Exception:
                failed = True
    if failed:
        raise RuntimeError("admin user initialization failed") from None

# --- DAO 操作封装 ---

_SCHEDULER_JOB_RUN_TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "execution_unknown",
}
