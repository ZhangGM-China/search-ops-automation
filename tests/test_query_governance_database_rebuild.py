import pytest
from sqlalchemy import create_engine, inspect

from src.persistence.models import Base, InventoryRecoveryTask, User
from src.query_governance.database import (
    QueryGovernanceDatabaseError,
    rebuild_query_governance_schema,
)
from src.query_governance.models import (
    QueryGovernanceRelevanceRecord,
)


_engines = []


@pytest.fixture(autouse=True)
def _close_test_engines():
    yield
    for engine in _engines:
        engine.dispose()
    _engines.clear()


def _engine(path):
    engine = create_engine(f"sqlite:///{path}")
    _engines.append(engine)
    return engine


def test_rebuild_creates_new_tables_and_removes_retired_query_tables(tmp_path):
    engine = _engine(tmp_path / "source.db")
    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO top_query_task (brand, data_date, query_text) VALUES ('atlas', '2026-08-28', '裙子')"
        )

    report = rebuild_query_governance_schema(engine)
    tables = set(inspect(engine).get_table_names())

    assert "query_governance_task" in tables
    assert "query_governance_selection_batch" in tables
    assert "query_governance_candidate" in tables
    assert "query_governance_step_status" in tables
    assert "query_governance_workflow_run" in tables
    assert "query_governance_async_task" in tables
    assert "query_governance_experiment" in tables
    assert "query_governance_experiment_sample" in tables
    assert "query_governance_experiment_daily_metric" in tables
    assert "query_governance_relevance_record" in tables
    assert "query_governance_image_asset" in tables
    assert "query_governance_relevance_guidance" in tables
    assert "top_query_task" not in tables
    assert "top_query_budget_account" not in tables
    assert "top_query_score" not in tables
    assert "ops_team_run" not in tables
    assert report.deleted_tables


def test_rebuild_removes_orphaned_legacy_tables(tmp_path):
    engine = _engine(tmp_path / "orphaned.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE query_governance_stage (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE agent_execution_event (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE top_query_governance_task (id INTEGER PRIMARY KEY)")

    report = rebuild_query_governance_schema(engine)
    tables = set(inspect(engine).get_table_names())

    assert "query_governance_stage" not in tables
    assert "agent_execution_event" not in tables
    assert "top_query_governance_task" not in tables
    assert {
        "query_governance_stage",
        "agent_execution_event",
        "top_query_governance_task",
    }.issubset(set(report.deleted_tables))


def test_rebuild_rehearses_users_and_inventory_migration_with_readback(tmp_path):
    source = _engine(tmp_path / "source.db")
    target = _engine(tmp_path / "target.db")
    Base.metadata.create_all(source)

    from sqlalchemy.orm import Session

    with Session(source) as db:
        db.add(
            User(
                username="alice",
                password_hash="hash",
                brand_permissions='["atlas"]',
                page_permissions='[]',
            )
        )
        db.add(
            InventoryRecoveryTask(
                brand="atlas",
                product_id="SKU-1",
                original_query="裙子",
                last_check_stock=75,
                status="monitoring",
            )
        )
        db.commit()

    report = rebuild_query_governance_schema(target, source_engine=source)
    with Session(target) as db:
        user = db.query(User).filter_by(username="alice").one()
        task = db.query(InventoryRecoveryTask).filter_by(product_id="SKU-1").one()

    assert report.migrated_rows == {"users": 1, "inventory_recovery_task": 1}
    assert user.password_hash == "hash"
    assert task.original_query == "裙子"
    assert task.last_check_stock == 75


def test_rebuild_is_idempotent_and_preserves_non_query_tables(tmp_path):
    engine = _engine(tmp_path / "db.db")
    Base.metadata.create_all(engine)
    first = rebuild_query_governance_schema(engine)
    first_tables = set(inspect(engine).get_table_names())
    second = rebuild_query_governance_schema(engine)
    second_tables = set(inspect(engine).get_table_names())

    assert first_tables == second_tables
    assert "users" in second.preserved_tables
    assert "inventory_recovery_task" in second.preserved_tables
    assert "no_result_query" in second.preserved_tables
    assert "assistant_session_meta" in second.preserved_tables
    assert first.created_tables == second.created_tables


def test_rebuild_clears_existing_new_query_history(tmp_path):
    engine = _engine(tmp_path / "history.db")
    rebuild_query_governance_schema(engine)

    from sqlalchemy.orm import Session

    with Session(engine) as db:
        db.add(
            QueryGovernanceRelevanceRecord(
                brand="atlas",
                query_text="裙子",
                query_key="裙子",
                product_id="SKU-1",
                relevance="relevant",
            )
        )
        db.commit()

    rebuild_query_governance_schema(engine)
    with Session(engine) as db:
        assert db.query(QueryGovernanceRelevanceRecord).count() == 0


def test_rebuild_stops_on_unknown_legacy_query_table_before_mutation(tmp_path):
    engine = _engine(tmp_path / "unknown.db")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE top_query_unplanned (id INTEGER PRIMARY KEY)")

    with pytest.raises(QueryGovernanceDatabaseError, match="top_query_unplanned"):
        rebuild_query_governance_schema(engine)

    assert "top_query_task" in inspect(engine).get_table_names()


def test_new_relevance_record_has_brand_query_product_uniqueness(tmp_path):
    engine = _engine(tmp_path / "constraints.db")
    rebuild_query_governance_schema(engine)

    from sqlalchemy.orm import Session

    with Session(engine) as db:
        db.add_all(
            [
                QueryGovernanceRelevanceRecord(
                    brand="atlas",
                    query_text="裙子",
                    query_key="裙子",
                    product_id="SKU-1",
                    relevance="relevant",
                ),
                QueryGovernanceRelevanceRecord(
                    brand="atlas",
                    query_text="裙子",
                    query_key="裙子",
                    product_id="SKU-1",
                    relevance="relevant",
                ),
            ]
        )
        with pytest.raises(Exception):
            db.commit()
        db.rollback()


def test_new_relevance_record_uses_normalized_query_key_for_uniqueness(tmp_path):
    engine = _engine(tmp_path / "normalized-constraints.db")
    rebuild_query_governance_schema(engine)

    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("query_governance_relevance_record")
    }
    assert columns["query_key"]["nullable"] is False

    unique_constraints = inspect(engine).get_unique_constraints("query_governance_relevance_record")
    assert any(
        set(constraint["column_names"]) == {"brand", "query_key", "product_id"}
        for constraint in unique_constraints
    )
