"""Compatibility dual-write adapter for the inventory recovery migration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import inspect, text

from src.migrations.inventory_recovery import (
    HISTORY_TABLE,
    LEGACY_TABLE,
    MIGRATION_STATE_TABLE,
    TARGET_TABLE,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class InventoryRecoveryTaskProjection:
    """Read-compatible projection shared by legacy and target sources."""

    id: int
    created_at: Any
    updated_at: Any
    brand: str | None
    product_id: str | None
    product_name: str | None
    original_query: str | None
    last_check_stock: int | None
    status: str | None
    last_action_request_id: str | None = None
    last_action_generation: int = 0


def _connection_for(bind):
    if hasattr(bind, "connection"):
        return bind.connection(), False
    if hasattr(bind, "connect"):
        return bind.connect(), True
    return bind, False


def _projection(row) -> InventoryRecoveryTaskProjection:
    def as_datetime(value):
        if value is None or isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace(" ", "T"))
        except ValueError:
            return value

    return InventoryRecoveryTaskProjection(
        id=int(row["id"]),
        created_at=as_datetime(row.get("created_at")),
        updated_at=as_datetime(row.get("updated_at")),
        brand=row.get("brand"),
        product_id=row.get("product_id"),
        product_name=row.get("product_name"),
        original_query=row.get("original_query"),
        last_check_stock=row.get("last_check_stock"),
        status=row.get("status"),
        last_action_request_id=row.get("last_action_request_id"),
        last_action_generation=int(row.get("last_action_generation") or 0),
    )


def read_inventory_recovery_tasks(
    bind,
    *,
    brand: str | None = None,
    query_text: str | None = None,
    status: str | Iterable[str] | None = None,
    active_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
    search: str | None = None,
    updated_after: datetime | None = None,
) -> list[InventoryRecoveryTaskProjection]:
    """Read from the configured source while preserving the old object shape."""

    connection, should_close = _connection_for(bind)
    try:
        tables = set(inspect(connection).get_table_names())
        use_target = False
        if MIGRATION_STATE_TABLE in tables:
            mode = connection.execute(
                text(f"SELECT read_mode FROM {MIGRATION_STATE_TABLE} WHERE id = 1")
            ).scalar_one_or_none()
            use_target = mode == "target"
        source = TARGET_TABLE if use_target else LEGACY_TABLE
        if source not in tables:
            raise RuntimeError(f"库存恢复读取源不存在: {source}")
        if use_target and not {TARGET_TABLE, HISTORY_TABLE}.issubset(tables):
            raise RuntimeError("库存恢复目标表结构不完整，禁止切读")

        if use_target:
            columns = (
                "legacy_id AS id, created_at, updated_at, brand, product_id, "
                "product_name, query_text AS original_query, "
                "broken_rate_percent AS last_check_stock, status, "
                "last_action_request_id, last_action_generation"
            )
        else:
            legacy_columns = {
                column["name"] for column in inspect(connection).get_columns(LEGACY_TABLE)
            }
            def legacy_column(name: str, default_sql: str) -> str:
                return name if name in legacy_columns else f"{default_sql} AS {name}"

            columns = ", ".join(
                (
                    "id",
                    "created_at",
                    "updated_at",
                    "brand",
                    "product_id",
                    "product_name",
                    "original_query",
                    "last_check_stock",
                    "status",
                    legacy_column("last_action_request_id", "NULL"),
                    legacy_column("last_action_generation", "0"),
                )
            )
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if brand:
            clauses.append("brand = :brand")
            params["brand"] = brand
        if query_text is not None:
            clauses.append("query_text = :query_text" if use_target else "original_query = :query_text")
            params["query_text"] = query_text
        if search:
            query_column = "query_text" if use_target else "original_query"
            clauses.append(
                f"(product_id LIKE :search OR product_name LIKE :search OR {query_column} LIKE :search)"
            )
            params["search"] = f"%{search}%"
        if updated_after is not None:
            clauses.append("updated_at >= :updated_after")
            params["updated_after"] = updated_after
        if active_only:
            clauses.append("status = 'monitoring'")
        elif status:
            if isinstance(status, str):
                clauses.append("status = :status")
                params["status"] = status
            else:
                values = [str(item) for item in status]
                if values:
                    placeholders = ", ".join(f":status_{index}" for index in range(len(values)))
                    clauses.append(f"status IN ({placeholders})")
                    params.update({f"status_{index}": value for index, value in enumerate(values)})
        sql = f"SELECT {columns} FROM {source}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id DESC"
        if limit is not None:
            if limit <= 0 or offset < 0:
                raise ValueError("库存恢复读取分页参数非法")
            sql += " LIMIT :_limit OFFSET :_offset"
            params.update({"_limit": limit, "_offset": offset})
        return [_projection(row) for row in connection.execute(text(sql), params).mappings().all()]
    finally:
        if should_close:
            connection.close()


def read_inventory_recovery_checks(
    bind,
    task_ids: Iterable[int],
) -> dict[int, list[dict[str, Any]]]:
    """Read ordered broken-rate snapshots for the requested recovery tasks."""

    ids = sorted({int(value) for value in task_ids if int(value) > 0})
    if not ids:
        return {}
    connection, should_close = _connection_for(bind)
    try:
        if HISTORY_TABLE not in set(inspect(connection).get_table_names()):
            return {}
        params = {f"task_id_{index}": value for index, value in enumerate(ids)}
        placeholders = ", ".join(f":task_id_{index}" for index in range(len(ids)))
        rows = connection.execute(
            text(f"""
                SELECT target_legacy_id, checked_at, broken_rate_percent, status, source
                FROM {HISTORY_TABLE}
                WHERE target_legacy_id IN ({placeholders})
                ORDER BY target_legacy_id, checked_at
            """),
            params,
        ).mappings().all()
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(int(row["target_legacy_id"]), []).append(dict(row))
        return result
    finally:
        if should_close:
            connection.close()


def count_inventory_recovery_tasks(
    bind,
    *,
    brand: str | None = None,
    query_text: str | None = None,
    status: str | Iterable[str] | None = None,
    active_only: bool = False,
    search: str | None = None,
    updated_after: datetime | None = None,
) -> int:
    """Count rows using the same source and predicates as the read adapter."""

    connection, should_close = _connection_for(bind)
    try:
        tables = set(inspect(connection).get_table_names())
        use_target = False
        if MIGRATION_STATE_TABLE in tables:
            mode = connection.execute(
                text(f"SELECT read_mode FROM {MIGRATION_STATE_TABLE} WHERE id = 1")
            ).scalar_one_or_none()
            use_target = mode == "target"
        source = TARGET_TABLE if use_target else LEGACY_TABLE
        if source not in tables:
            raise RuntimeError(f"库存恢复读取源不存在: {source}")
        clauses: list[str] = []
        params: dict[str, Any] = {}
        query_column = "query_text" if use_target else "original_query"
        if brand:
            clauses.append("brand = :brand")
            params["brand"] = brand
        if query_text is not None:
            clauses.append(f"{query_column} = :query_text")
            params["query_text"] = query_text
        if search:
            clauses.append(
                f"(product_id LIKE :search OR product_name LIKE :search OR {query_column} LIKE :search)"
            )
            params["search"] = f"%{search}%"
        if updated_after is not None:
            clauses.append("updated_at >= :updated_after")
            params["updated_after"] = updated_after
        if active_only:
            clauses.append("status = 'monitoring'")
        elif status:
            values = [status] if isinstance(status, str) else [str(item) for item in status]
            if values:
                placeholders = ", ".join(f":status_{index}" for index in range(len(values)))
                clauses.append(f"status IN ({placeholders})")
                params.update({f"status_{index}": value for index, value in enumerate(values)})
        sql = f"SELECT COUNT(*) FROM {source}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return int(connection.execute(text(sql), params).scalar_one() or 0)
    finally:
        if should_close:
            connection.close()


def sync_inventory_recovery_task(session, task) -> None:
    """Mirror one ORM task into the target projection and append its snapshot.

    The caller owns the transaction.  A target write failure therefore rolls
    back together with the legacy write instead of silently creating drift.
    """

    # Inspect the session's active connection.  Using the Engine directly can
    # borrow the same SQLite in-memory handle and accidentally roll back the
    # caller's uncommitted legacy write under StaticPool.
    connection = session.connection()
    tables = set(inspect(connection).get_table_names())
    required = {TARGET_TABLE, HISTORY_TABLE, MIGRATION_STATE_TABLE}
    if MIGRATION_STATE_TABLE not in tables:
        return
    missing = sorted(required - tables)
    if missing:
        raise RuntimeError("库存恢复双写目标结构缺失: " + ", ".join(missing))
    state = session.execute(
        text(f"SELECT phase FROM {MIGRATION_STATE_TABLE} WHERE id = 1")
    ).scalar_one_or_none()
    if state not in {"dual_write", "target_read", "backfilled"}:
        return

    values = {
        "legacy_id": int(task.id),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "brand": task.brand,
        "product_id": task.product_id,
        "product_name": task.product_name,
        "query_text": task.original_query,
        "broken_rate_percent": task.last_check_stock,
        "status": task.status or "monitoring",
        "last_action_request_id": task.last_action_request_id,
        "last_action_generation": int(task.last_action_generation or 0),
        "migrated_at": _now(),
    }
    updated = session.execute(
        text(f"""
            UPDATE {TARGET_TABLE}
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
    ).rowcount
    if not updated:
        session.execute(
            text(f"""
                INSERT INTO {TARGET_TABLE} (
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

    checked_at = task.updated_at or _now()
    exists = session.execute(
        text(f"""
            SELECT 1 FROM {HISTORY_TABLE}
            WHERE target_legacy_id = :legacy_id
              AND checked_at = :checked_at
              AND source = 'dual_write'
        """),
        {"legacy_id": int(task.id), "checked_at": checked_at},
    ).first()
    if not exists:
        session.execute(
            text(f"""
                INSERT INTO {HISTORY_TABLE} (
                    target_legacy_id, checked_at, broken_rate_percent, status, source
                ) VALUES (:legacy_id, :checked_at, :rate, :status, 'dual_write')
            """),
            {
                "legacy_id": int(task.id),
                "checked_at": checked_at,
                "rate": task.last_check_stock,
                "status": task.status or "monitoring",
            },
        )


def inventory_recovery_read_mode(bind) -> str:
    connection, should_close = _connection_for(bind)
    try:
        tables = set(inspect(connection).get_table_names())
        if MIGRATION_STATE_TABLE not in tables:
            return "legacy"
        return str(
            connection.execute(
                text(f"SELECT read_mode FROM {MIGRATION_STATE_TABLE} WHERE id = 1")
            ).scalar_one_or_none()
            or "legacy"
        )
    finally:
        if should_close:
            connection.close()


__all__ = [
    "InventoryRecoveryTaskProjection",
    "read_inventory_recovery_checks",
    "count_inventory_recovery_tasks",
    "inventory_recovery_read_mode",
    "read_inventory_recovery_tasks",
    "sync_inventory_recovery_task",
]
