"""Online migration helpers for the rewritten inventory-recovery table.

The helpers are deliberately additive and database-agnostic.  They create a
shadow target, copy legacy rows in bounded batches, and keep a small control
record that makes cutover and rollback explicit.  They never drop or rename a
business table.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect, text


LEGACY_TABLE = "inventory_recovery_task"
TARGET_TABLE = "query_governance_inventory_task"
HISTORY_TABLE = "query_governance_inventory_check"
MIGRATION_STATE_TABLE = "query_governance_inventory_migration"
KNOWN_STATUSES = frozenset(
    {"monitoring", "recovered", "auto_stopped", "manually_stopped"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _quote(engine, name: str) -> str:
    return engine.dialect.identifier_preparer.quote(name)


def ensure_inventory_recovery_target_schema(engine) -> dict[str, Any]:
    """Create the shadow schema and initialize its singleton state row."""

    q = lambda name: _quote(engine, name)
    existing_before = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {q(TARGET_TABLE)} (
                legacy_id INTEGER PRIMARY KEY,
                created_at DATETIME,
                updated_at DATETIME,
                brand VARCHAR(50),
                product_id TEXT,
                product_name VARCHAR(255),
                query_text VARCHAR(255),
                broken_rate_percent INTEGER,
                status VARCHAR(20) NOT NULL,
                last_action_request_id VARCHAR(255),
                last_action_generation INTEGER NOT NULL DEFAULT 0,
                migrated_at DATETIME NOT NULL
            )
        """))
        _assert_table_columns(
            conn,
            TARGET_TABLE,
            {
                "legacy_id", "created_at", "updated_at", "brand", "product_id",
                "product_name", "query_text", "broken_rate_percent", "status",
                "last_action_request_id", "last_action_generation", "migrated_at",
            },
        )
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {q(HISTORY_TABLE)} (
                target_legacy_id INTEGER NOT NULL,
                checked_at DATETIME NOT NULL,
                broken_rate_percent INTEGER,
                status VARCHAR(20) NOT NULL,
                source VARCHAR(40) NOT NULL DEFAULT 'legacy_backfill',
                UNIQUE (target_legacy_id, checked_at, source)
            )
        """))
        _assert_table_columns(
            conn,
            HISTORY_TABLE,
            {"target_legacy_id", "checked_at", "broken_rate_percent", "status", "source"},
        )
        _ensure_index(
            conn,
            engine,
            TARGET_TABLE,
            "ix_qg_inventory_task_lookup",
            ("brand", "query_text", "status", "updated_at", "legacy_id"),
        )
        _ensure_index(
            conn,
            engine,
            HISTORY_TABLE,
            "ix_qg_inventory_check_task_time",
            ("target_legacy_id", "checked_at"),
        )
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {q(MIGRATION_STATE_TABLE)} (
                id INTEGER PRIMARY KEY,
                phase VARCHAR(30) NOT NULL,
                read_mode VARCHAR(20) NOT NULL,
                last_legacy_id INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL
            )
        """))
        _assert_table_columns(
            conn,
            MIGRATION_STATE_TABLE,
            {"id", "phase", "read_mode", "last_legacy_id", "updated_at"},
        )
        insert_ignore = "INSERT IGNORE" if engine.dialect.name in {"mysql", "mariadb"} else "INSERT OR IGNORE"
        conn.execute(
            text(
                f"{insert_ignore} INTO {q(MIGRATION_STATE_TABLE)} "
                "(id, phase, read_mode, last_legacy_id, updated_at) "
                "VALUES (1, 'prepared', 'legacy', 0, :updated)"
            ),
            {"updated": _utc_now()},
        )
    return {
        "createdTables": [
            name
            for name in (TARGET_TABLE, HISTORY_TABLE, MIGRATION_STATE_TABLE)
            if name not in existing_before
        ],
        "phase": get_inventory_recovery_migration_state(engine)["phase"],
    }


def _assert_table_columns(conn, table: str, required: set[str]) -> None:
    actual = {column["name"] for column in inspect(conn).get_columns(table)}
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(f"库存恢复目标表结构不完整: {table} 缺少字段 {', '.join(missing)}")


def _ensure_index(conn, engine, table: str, name: str, columns: tuple[str, ...]) -> None:
    existing = {item.get("name") for item in inspect(conn).get_indexes(table)}
    if name in existing:
        return
    columns_sql = ", ".join(_quote(engine, column) for column in columns)
    conn.execute(
        text(
            f"CREATE INDEX {_quote(engine, name)} ON "
            f"{_quote(engine, table)} ({columns_sql})"
        )
    )


def get_inventory_recovery_migration_state(engine) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"SELECT phase, read_mode, last_legacy_id, updated_at "
                f"FROM {_quote(engine, MIGRATION_STATE_TABLE)} WHERE id = 1"
            )
        ).mappings().one()
    return {
        "phase": row["phase"],
        "readMode": row["read_mode"],
        "lastLegacyId": int(row["last_legacy_id"] or 0),
        "updatedAt": row["updated_at"],
    }


def set_inventory_recovery_phase(engine, phase: str) -> dict[str, Any]:
    read_mode_by_phase = {
        "prepared": "legacy",
        "backfilled": "legacy",
        "dual_write": "legacy",
        "target_read": "target",
        "observe": "target",
        "retire": "target",
        "rollback": "legacy",
    }
    if phase not in read_mode_by_phase:
        raise ValueError(f"不支持的库存迁移阶段: {phase}")
    current = get_inventory_recovery_migration_state(engine)
    if phase == "target_read":
        if current["phase"] not in {"backfilled", "dual_write", "target_read", "observe"}:
            raise RuntimeError("回填校验未通过：尚未完成 backfill")
        validation = validate_inventory_recovery_migration(engine)
        if not validation["ok"]:
            raise RuntimeError("回填校验未通过：源表与目标表存在差异")
    if phase == "dual_write":
        if current["phase"] not in {"backfilled", "dual_write"}:
            raise RuntimeError("双写启动前必须完成 backfill")
        validation = validate_inventory_recovery_migration(engine)
        if not validation["ok"]:
            raise RuntimeError("双写启动前校验未通过：源表与目标表存在差异")
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {_quote(engine, MIGRATION_STATE_TABLE)} "
                "SET phase = :phase, read_mode = :read_mode, updated_at = :updated "
                "WHERE id = 1"
            ),
            {
                "phase": phase,
                "read_mode": read_mode_by_phase[phase],
                "updated": _utc_now(),
            },
        )
    return get_inventory_recovery_migration_state(engine)


def _row_digest(row: dict[str, Any]) -> str:
    payload = {
        "legacy_id": row.get("id", row.get("legacy_id")),
        "brand": row.get("brand"),
        "product_id": row.get("product_id"),
        "product_name": row.get("product_name"),
        "query_text": row.get("original_query", row.get("query_text")),
        "broken_rate_percent": row.get("last_check_stock", row.get("broken_rate_percent")),
        "status": row.get("status"),
        "last_action_request_id": row.get("last_action_request_id"),
        "last_action_generation": int(row.get("last_action_generation") or 0),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def backfill_inventory_recovery(
    engine,
    *,
    batch_size: int = 500,
    reconcile: bool = False,
) -> dict[str, Any]:
    """Copy legacy rows idempotently and record one history snapshot per row."""

    if batch_size <= 0:
        raise ValueError("batch_size 必须为正整数")
    ensure_inventory_recovery_target_schema(engine)
    q = lambda name: _quote(engine, name)
    state = get_inventory_recovery_migration_state(engine)
    cursor = state["lastLegacyId"]
    if reconcile and cursor:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {q(MIGRATION_STATE_TABLE)} "
                    "SET last_legacy_id = 0, updated_at = :updated WHERE id = 1"
                ),
                {"updated": _utc_now()},
            )
        cursor = 0
    migrated = 0
    updated = 0
    history_rows = 0

    while True:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT * FROM {q(LEGACY_TABLE)} "
                    "WHERE id > :cursor ORDER BY id LIMIT :limit"
                ),
                {"cursor": cursor, "limit": batch_size},
            ).mappings().all()
        if not rows:
            break
        with engine.begin() as conn:
            for row in rows:
                status = str(row.get("status") or "monitoring")
                if status not in KNOWN_STATUSES:
                    raise ValueError(f"未知库存任务状态: {status}")
                missing_identity = [
                    name
                    for name, value in (
                        ("brand", row.get("brand")),
                        ("product_id", row.get("product_id")),
                        ("original_query", row.get("original_query")),
                    )
                    if value is None or not str(value).strip()
                ]
                if missing_identity:
                    raise ValueError(
                        "库存任务缺少迁移必需字段: "
                        + ", ".join(missing_identity)
                        + f" (legacy_id={row['id']})"
                    )
                values = {
                    "legacy_id": row["id"],
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "brand": row.get("brand"),
                    "product_id": row.get("product_id"),
                    "product_name": row.get("product_name"),
                    "query_text": row.get("original_query"),
                    "broken_rate_percent": row.get("last_check_stock"),
                    "status": status,
                    "last_action_request_id": row.get("last_action_request_id"),
                    "last_action_generation": int(row.get("last_action_generation") or 0),
                    "migrated_at": _utc_now(),
                }
                insert_ignore = "INSERT IGNORE" if engine.dialect.name in {"mysql", "mariadb"} else "INSERT OR IGNORE"
                inserted = conn.execute(
                    text(f"""
                        {insert_ignore} INTO {q(TARGET_TABLE)} (
                            legacy_id, created_at, updated_at, brand, product_id,
                            product_name, query_text, broken_rate_percent, status,
                            last_action_request_id, last_action_generation, migrated_at
                        ) VALUES (
                            :legacy_id, :created_at, :updated_at, :brand, :product_id,
                            :product_name, :query_text, :broken_rate_percent, :status,
                            :last_action_request_id, :last_action_generation, :migrated_at
                        )
                    """),
                    values,
                )
                if inserted.rowcount:
                    migrated += 1
                    conn.execute(
                        text(f"""
                            {insert_ignore} INTO {q(HISTORY_TABLE)} (
                                target_legacy_id, checked_at, broken_rate_percent,
                                status, source
                            ) VALUES (:legacy_id, :checked_at, :rate, :status, 'legacy_backfill')
                        """),
                        {
                            "legacy_id": row["id"],
                            "checked_at": row.get("updated_at") or _utc_now(),
                            "rate": row.get("last_check_stock"),
                            "status": status,
                        },
                    )
                    history_rows += 1
                else:
                    target_row = conn.execute(
                        text(
                            f"SELECT * FROM {q(TARGET_TABLE)} WHERE legacy_id = :legacy_id"
                        ),
                        {"legacy_id": row["id"]},
                    ).mappings().one()
                    if _row_digest(dict(row)) != _row_digest(dict(target_row)):
                        conn.execute(
                            text(f"""
                                UPDATE {q(TARGET_TABLE)}
                                SET created_at = :created_at,
                                    updated_at = :updated_at,
                                    brand = :brand,
                                    product_id = :product_id,
                                    product_name = :product_name,
                                    query_text = :query_text,
                                    broken_rate_percent = :broken_rate_percent,
                                    status = :status,
                                    last_action_request_id = :last_action_request_id,
                                    last_action_generation = :last_action_generation,
                                    migrated_at = :migrated_at
                                WHERE legacy_id = :legacy_id
                            """),
                            values,
                        )
                        updated += 1
                        conn.execute(
                            text(f"""
                                {insert_ignore} INTO {q(HISTORY_TABLE)} (
                                    target_legacy_id, checked_at, broken_rate_percent,
                                    status, source
                                ) VALUES (:legacy_id, :checked_at, :rate, :status, 'legacy_backfill')
                            """),
                            {
                                "legacy_id": row["id"],
                                "checked_at": row.get("updated_at") or _utc_now(),
                                "rate": row.get("last_check_stock"),
                                "status": status,
                            },
                        )
                        history_rows += 1
                cursor = int(row["id"])
            conn.execute(
                text(
                    f"UPDATE {q(MIGRATION_STATE_TABLE)} "
                    "SET last_legacy_id = :cursor, phase = 'backfilled', updated_at = :updated "
                    "WHERE id = 1"
                ),
                {"cursor": cursor, "updated": _utc_now()},
            )
    return {
        "migratedRows": migrated,
        "updatedRows": updated,
        "historyRows": history_rows,
        "lastLegacyId": cursor,
        "phase": get_inventory_recovery_migration_state(engine)["phase"],
    }


def validate_inventory_recovery_migration(engine) -> dict[str, Any]:
    """Compare the source and target business projections, excluding timestamps."""

    q = lambda name: _quote(engine, name)
    with engine.connect() as conn:
        legacy = conn.execute(text(f"SELECT * FROM {q(LEGACY_TABLE)} ORDER BY id")).mappings().all()
        target = conn.execute(text(f"SELECT * FROM {q(TARGET_TABLE)} ORDER BY legacy_id")).mappings().all()
    legacy_checksum = hashlib.sha256(
        "".join(_row_digest(dict(row)) for row in legacy).encode()
    ).hexdigest()
    target_checksum = hashlib.sha256(
        "".join(_row_digest(dict(row)) for row in target).encode()
    ).hexdigest()
    report = {
        "ok": len(legacy) == len(target) and legacy_checksum == target_checksum,
        "legacyRows": len(legacy),
        "targetRows": len(target),
        "checksumMatch": legacy_checksum == target_checksum,
        "legacyChecksum": legacy_checksum,
        "targetChecksum": target_checksum,
    }
    return report


def rollback_inventory_recovery(engine) -> dict[str, Any]:
    """Switch reads back to the legacy table without deleting target data."""

    return set_inventory_recovery_phase(engine, "rollback")


__all__ = [
    "HISTORY_TABLE",
    "LEGACY_TABLE",
    "MIGRATION_STATE_TABLE",
    "TARGET_TABLE",
    "backfill_inventory_recovery",
    "ensure_inventory_recovery_target_schema",
    "get_inventory_recovery_migration_state",
    "rollback_inventory_recovery",
    "set_inventory_recovery_phase",
    "validate_inventory_recovery_migration",
]
