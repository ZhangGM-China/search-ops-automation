import datetime

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from src.persistence import (
    Base,
    NodeModelConfigEntry,
    NodeModelConfigVersion,
    SessionLocal,
    reset_engine_for_testing,
)
from src.migrations.release_schema import (
    ensure_release_schema,
    validate_release_schema_verification,
    verify_release_schema,
)


def setup_function():
    engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=engine)


def teardown_function():
    from src.persistence import session

    Base.metadata.drop_all(bind=session.engine)
    session.engine.dispose()


def test_node_model_config_version_persists_active_release_and_node_entries():
    db = SessionLocal()
    try:
        version = NodeModelConfigVersion(
            version=1,
            status="published",
            active_slot="active",
            published_at=datetime.datetime(2026, 8, 6, 12, 0, 0),
            change_note="首次发布节点模型配置",
        )
        db.add(version)
        db.flush()
        db.add_all([
            NodeModelConfigEntry(
                version_id=version.id,
                node_key="no_result.intent",
                channel="rd_llm",
                primary_model="DeepSeek-V3",
                fallback_model="DeepSeek-R1",
            ),
            NodeModelConfigEntry(
                version_id=version.id,
                node_key="assistant.chat",
                channel="pi_agent",
                primary_model="DeepSeek-V3.1",
                fallback_model=None,
            ),
        ])
        db.commit()

        active = db.query(NodeModelConfigVersion).filter(
            NodeModelConfigVersion.active_slot == "active",
        ).one()
        assert active.version == 1
        assert [(entry.node_key, entry.channel, entry.primary_model, entry.fallback_model) for entry in active.entries] == [
            ("assistant.chat", "pi_agent", "DeepSeek-V3.1", None),
            ("no_result.intent", "rd_llm", "DeepSeek-V3", "DeepSeek-R1"),
        ]
    finally:
        db.close()


def test_node_model_config_version_allows_one_active_slot_and_unique_node_per_version():
    db = SessionLocal()
    try:
        active = NodeModelConfigVersion(version=1, status="published", active_slot="active")
        db.add(active)
        db.flush()
        db.add(NodeModelConfigEntry(
            version_id=active.id,
            node_key="no_result.intent",
            channel="rd_llm",
            primary_model="DeepSeek-V3",
        ))
        db.commit()

        db.add(NodeModelConfigVersion(version=2, status="published", active_slot="active"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.add(NodeModelConfigEntry(
            version_id=active.id,
            node_key="no_result.intent",
            channel="rd_llm",
            primary_model="DeepSeek-R1",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize(
    ("version_kwargs", "entry_kwargs"),
    [
        ({"version": 1, "status": "invalid"}, None),
        ({"version": 1, "status": "draft"}, {"node_key": " ", "channel": "rd_llm", "primary_model": "DeepSeek-V3"}),
        ({"version": 1, "status": "draft"}, {"node_key": "no_result.intent", "channel": "invalid", "primary_model": "DeepSeek-V3"}),
        ({"version": 1, "status": "draft"}, {"node_key": "no_result.intent", "channel": "rd_llm", "primary_model": " "}),
    ],
)
def test_node_model_config_constraints_reject_invalid_status_channel_and_model(
    version_kwargs,
    entry_kwargs,
):
    db = SessionLocal()
    try:
        version = NodeModelConfigVersion(**version_kwargs)
        db.add(version)
        if entry_kwargs is None:
            with pytest.raises(IntegrityError):
                db.commit()
            return

        db.flush()
        db.add(NodeModelConfigEntry(version_id=version.id, **entry_kwargs))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_release_migration_and_schema_verify_include_node_model_config_tables():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    try:
        result = ensure_release_schema(engine, log=lambda _message: None)
        verification = verify_release_schema(engine)
        inspector = inspect(engine)

        assert "ensuredIndexes" in result
        assert verify_release_schema(engine)["ok"] is True
        assert validate_release_schema_verification(verification) == verification
        assert NodeModelConfigVersion.__tablename__ in inspector.get_table_names()
        assert NodeModelConfigEntry.__tablename__ in inspector.get_table_names()
    finally:
        engine.dispose()


def test_node_model_config_ddl_keeps_constraints_for_mysql_and_mariadb():
    for dialect in (mysql_dialect(), MariaDBDialect()):
        version_ddl = str(CreateTable(NodeModelConfigVersion.__table__).compile(dialect=dialect))
        entry_ddl = str(CreateTable(NodeModelConfigEntry.__table__).compile(dialect=dialect))

        assert "CONSTRAINT uq_node_model_config_version_active_slot UNIQUE" in version_ddl
        assert "CONSTRAINT ck_node_model_config_version_status" in version_ddl
        assert "CONSTRAINT uq_node_model_config_entry_version_node UNIQUE" in entry_ddl
        assert "CONSTRAINT ck_node_model_config_entry_channel" in entry_ddl
        assert "CONSTRAINT ck_node_model_config_entry_primary_model_nonempty" in entry_ddl
