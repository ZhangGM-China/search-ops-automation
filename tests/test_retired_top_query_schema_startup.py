from sqlalchemy import create_engine, inspect, text

import src.persistence.schema as persistence_schema
from src.persistence import session as persistence_session


def test_schema_upgrade_ignores_retired_top_query_indexes(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stale-legacy.db'}")
    monkeypatch.setattr(persistence_session, "engine", engine)

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE top_query_task ("
                "id INTEGER PRIMARY KEY, brand VARCHAR(50), "
                "data_date DATE, status VARCHAR(30)"
                ")"
            )
        )

    persistence_schema._ensure_schema_upgrades()

    indexes = {
        row[1]
        for row in inspect(engine).get_indexes("top_query_task")
    }
    assert not any(name.startswith("ix_top_query_") for name in indexes)


def test_schema_upgrade_adds_inventory_action_fence_columns(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-inventory.db'}")
    monkeypatch.setattr(persistence_session, "engine", engine)

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE inventory_recovery_task ("
                "id INTEGER PRIMARY KEY, created_at DATETIME, updated_at DATETIME, "
                "brand VARCHAR(50), product_id TEXT, product_name VARCHAR(255), "
                "original_query VARCHAR(255), last_check_stock INTEGER, status VARCHAR(20)"
                ")"
            )
        )

    persistence_schema._ensure_schema_upgrades()

    with engine.connect() as conn:
        columns = {
            column[1]
            for column in conn.execute(text("PRAGMA table_info(inventory_recovery_task)"))
        }
    assert {"last_action_request_id", "last_action_generation"}.issubset(columns)
