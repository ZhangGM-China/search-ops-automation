import datetime as dt

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.migrations.inventory_recovery import (
    HISTORY_TABLE,
    LEGACY_TABLE,
    MIGRATION_STATE_TABLE,
    TARGET_TABLE,
    backfill_inventory_recovery,
    ensure_inventory_recovery_target_schema,
    get_inventory_recovery_migration_state,
    rollback_inventory_recovery,
    set_inventory_recovery_phase,
    validate_inventory_recovery_migration,
)
from src.persistence.inventory_recovery_dual_write import (
    sync_inventory_recovery_task,
)
from src.persistence.inventory_recovery_dual_write import (
    read_inventory_recovery_tasks,
)
from src.persistence.models import InventoryRecoveryTask
from src.persistence import repositories as persistence_repositories


@pytest.fixture()
def engine():
    db = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with db.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE {LEGACY_TABLE} (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                brand VARCHAR(50),
                product_id TEXT,
                product_name VARCHAR(255),
                original_query VARCHAR(255),
                last_check_stock INTEGER,
                status VARCHAR(20),
                last_action_request_id VARCHAR(255),
                last_action_generation INTEGER NOT NULL DEFAULT 0
            )
        """))
        conn.execute(text(f"""
            INSERT INTO {LEGACY_TABLE} (
                created_at, updated_at, brand, product_id, product_name,
                original_query, last_check_stock, status,
                last_action_request_id, last_action_generation
            ) VALUES
                ('2026-09-01 10:00:00', '2026-09-02 10:00:00', 'atlas', 'SKU-1', 'One', 'dress', 75, 'monitoring', 'req-1', 2),
                ('2026-09-01 11:00:00', '2026-09-03 11:00:00', 'harbor', 'SKU-2', 'Two', 'shirt', 10, 'recovered', NULL, 0)
        """))
    yield db
    db.dispose()


def test_prepare_creates_target_history_and_state_tables(engine):
    report = ensure_inventory_recovery_target_schema(engine)

    assert set(report["createdTables"]) == {
        TARGET_TABLE,
        HISTORY_TABLE,
        MIGRATION_STATE_TABLE,
    }
    assert {TARGET_TABLE, HISTORY_TABLE, MIGRATION_STATE_TABLE}.issubset(
        set(inspect(engine).get_table_names())
    )
    state = get_inventory_recovery_migration_state(engine)
    assert state["phase"] == "prepared"
    assert state["readMode"] == "legacy"


def test_prepare_blocks_partial_target_schema_instead_of_claiming_ready(engine):
    ensure_inventory_recovery_target_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_qg_inventory_task_lookup"))
        conn.execute(text(f"ALTER TABLE {TARGET_TABLE} DROP COLUMN query_text"))

    with pytest.raises(RuntimeError, match="目标表结构不完整"):
        ensure_inventory_recovery_target_schema(engine)


def test_backfill_is_idempotent_and_preserves_semantics(engine):
    ensure_inventory_recovery_target_schema(engine)

    first = backfill_inventory_recovery(engine, batch_size=1)
    second = backfill_inventory_recovery(engine, batch_size=1)

    assert first["migratedRows"] == 2
    assert second["migratedRows"] == 0
    assert first["historyRows"] == 2
    assert second["historyRows"] == 0
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT * FROM {TARGET_TABLE} WHERE legacy_id = 1")
        ).mappings().one()
        assert row["query_text"] == "dress"
        assert row["broken_rate_percent"] == 75
        assert row["last_action_request_id"] == "req-1"
        assert row["last_action_generation"] == 2
        assert conn.execute(text(f"SELECT COUNT(*) FROM {HISTORY_TABLE}")).scalar_one() == 2
    assert get_inventory_recovery_migration_state(engine)["phase"] == "backfilled"


def test_backfill_reconciles_source_changes_after_initial_copy(engine):
    ensure_inventory_recovery_target_schema(engine)
    backfill_inventory_recovery(engine)
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE {LEGACY_TABLE} SET last_check_stock = 88 WHERE id = 1"))

    report = backfill_inventory_recovery(engine, reconcile=True)
    assert report["migratedRows"] == 0
    assert report["updatedRows"] == 1
    with engine.connect() as conn:
        assert conn.execute(
            text(f"SELECT broken_rate_percent FROM {TARGET_TABLE} WHERE legacy_id = 1")
        ).scalar_one() == 88


def test_validation_reports_missing_rows_and_checksum_drift(engine):
    ensure_inventory_recovery_target_schema(engine)
    backfill_inventory_recovery(engine)
    assert validate_inventory_recovery_migration(engine)["ok"] is True

    with engine.begin() as conn:
        conn.execute(text(f"UPDATE {TARGET_TABLE} SET broken_rate_percent = 99 WHERE legacy_id = 1"))
    report = validate_inventory_recovery_migration(engine)
    assert report["ok"] is False
    assert report["checksumMatch"] is False


def test_cutover_and_rollback_are_explicit_and_non_destructive(engine):
    ensure_inventory_recovery_target_schema(engine)
    backfill_inventory_recovery(engine)

    state = set_inventory_recovery_phase(engine, "target_read")
    assert state["phase"] == "target_read"
    assert state["readMode"] == "target"

    rolled_back = rollback_inventory_recovery(engine)
    assert rolled_back["phase"] == "rollback"
    assert rolled_back["readMode"] == "legacy"
    with engine.connect() as conn:
        assert conn.execute(text(f"SELECT COUNT(*) FROM {LEGACY_TABLE}")).scalar_one() == 2
        assert conn.execute(text(f"SELECT COUNT(*) FROM {TARGET_TABLE}")).scalar_one() == 2


def test_target_read_returns_projection_and_rollback_returns_legacy_rows(engine):
    ensure_inventory_recovery_target_schema(engine)
    backfill_inventory_recovery(engine)

    set_inventory_recovery_phase(engine, "target_read")
    target_rows = read_inventory_recovery_tasks(engine, brand="atlas")
    assert len(target_rows) == 1
    assert target_rows[0].id == 1
    assert target_rows[0].original_query == "dress"
    assert target_rows[0].last_check_stock == 75

    set_inventory_recovery_phase(engine, "rollback")
    legacy_rows = read_inventory_recovery_tasks(engine, brand="atlas")
    assert len(legacy_rows) == 1
    assert legacy_rows[0].id == 1
    assert legacy_rows[0].original_query == "dress"


def test_cutover_is_blocked_until_backfill_validation_passes(engine):
    ensure_inventory_recovery_target_schema(engine)
    with pytest.raises(RuntimeError, match="回填校验未通过"):
        set_inventory_recovery_phase(engine, "target_read")


def test_dashboard_repository_uses_target_projection_after_cutover(engine, monkeypatch):
    ensure_inventory_recovery_target_schema(engine)
    backfill_inventory_recovery(engine)
    set_inventory_recovery_phase(engine, "target_read")
    monkeypatch.setattr(
        persistence_repositories,
        "SessionLocal",
        sessionmaker(bind=engine),
    )

    result = persistence_repositories.get_dashboard_inventory_tasks(
        {"brand": "atlas", "status": "monitoring"}, page=1, limit=20
    )
    assert result["total"] == 1
    assert result["items"][0].id == 1
    assert result["items"][0].original_query == "dress"


def test_unknown_legacy_status_blocks_backfill(engine):
    ensure_inventory_recovery_target_schema(engine)
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE {LEGACY_TABLE} SET status = 'mystery' WHERE id = 1"))

    with pytest.raises(ValueError, match="未知库存任务状态"):
        backfill_inventory_recovery(engine)


def test_dual_write_keeps_target_and_history_in_same_transaction(engine):
    ensure_inventory_recovery_target_schema(engine)
    InventoryRecoveryTask.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {LEGACY_TABLE} (id, brand, product_id, original_query, status) VALUES (10, 'atlas', 'SKU-10', 'coat', 'monitoring')"))
    backfill_inventory_recovery(engine)
    set_inventory_recovery_phase(engine, "dual_write")
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        task = session.get(InventoryRecoveryTask, 10)
        assert task is not None
        task.product_name = "Ten"
        task.last_check_stock = 61
        task.last_action_request_id = "req-10"
        task.last_action_generation = 3
        sync_inventory_recovery_task(session, task)
        session.commit()

        task.last_check_stock = 32
        task.status = "recovered"
        task.updated_at = dt.datetime(2026, 9, 4, 12, 0, 0)
        sync_inventory_recovery_task(session, task)
        session.commit()
        with engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT broken_rate_percent, status FROM {TARGET_TABLE} WHERE legacy_id = 10")
            ).mappings().one()
            assert row["broken_rate_percent"] == 32
            assert row["status"] == "recovered"
            assert conn.execute(
                text(f"SELECT COUNT(*) FROM {HISTORY_TABLE} WHERE target_legacy_id = 10")
            ).scalar_one() == 3
    finally:
        session.close()


def test_dual_write_failure_rolls_back_legacy_write(engine):
    ensure_inventory_recovery_target_schema(engine)
    InventoryRecoveryTask.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {LEGACY_TABLE} (id, brand, product_id, original_query, status) VALUES (20, 'atlas', 'SKU-20', 'jeans', 'monitoring')"))
    backfill_inventory_recovery(engine)
    set_inventory_recovery_phase(engine, "dual_write")
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE {HISTORY_TABLE}"))

    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        task = InventoryRecoveryTask(
            id=21,
            brand="atlas",
            product_id="SKU-21",
            original_query="jeans",
            status="monitoring",
        )
        session.add(task)
        session.flush()
        with pytest.raises(RuntimeError, match="目标结构缺失"):
            sync_inventory_recovery_task(session, task)
        session.rollback()
    finally:
        session.close()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM inventory_recovery_task WHERE id = 21")).scalar_one() == 0
