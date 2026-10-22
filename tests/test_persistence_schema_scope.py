from sqlalchemy import create_engine, inspect

import src.persistence.schema as persistence_schema
from src.persistence import session as persistence_session


def test_init_db_creates_only_active_schema(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'active.db'}")
    monkeypatch.setattr(persistence_session, "engine", engine)
    monkeypatch.setattr(persistence_schema, "init_admin_user", lambda: None)

    persistence_schema.init_db()
    tables = set(inspect(engine).get_table_names())

    assert "users" in tables
    assert "inventory_recovery_task" in tables
    assert "query_governance_task" in tables
    assert "query_governance_relevance_record" in tables
    assert "top_query_task" not in tables
    assert "top_query_workflow_run" not in tables
    assert "ops_team_run" not in tables
