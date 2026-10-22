import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint, create_engine, event, inspect, text
from sqlalchemy.dialects.mysql import TEXT as MYSQL_TEXT
from sqlalchemy.dialects.mysql import MEDIUMTEXT as MYSQL_MEDIUMTEXT
from sqlalchemy.dialects.mysql import VARCHAR, dialect as mysql_dialect
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from src.migrations import release_schema as prod_migration
from src.persistence import (
    ActionAttempt,
    ActionFenceEvidence,
    ActionIntent,
    ActionReadback,
    ActionTerminalFact,
    Base,
    SchedulerJobRun,
)
from src.query_governance.models import (
    QueryGovernanceContextSnapshot,
    QueryGovernanceImageAsset,
)
from src.migrations.release_schema import (
    AIOPS127_FOREIGN_KEY_SPECS,
    AIOPS127_INDEX_SPECS,
    AIOPS127_MYSQL_COLUMN_RISK_EVIDENCE,
    AIOPS127_SCHEMA_COLUMNS,
    AIOPS127_UNIQUE_SPECS,
    RELEASE_CHECK_SPECS,
    RELEASE_VERIFICATION_TABLES,
    _build_create_index_sql,
    ensure_release_schema,
    verify_release_schema,
)


@pytest.fixture()
def engine():
    sqlite_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(sqlite_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    yield sqlite_engine
    sqlite_engine.dispose()


def _create_legacy_action_schema(engine, *, include_fence_evidence=False):
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE inventory_recovery_task (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                updated_at DATETIME,
                brand VARCHAR(50),
                product_id TEXT,
                product_name VARCHAR(255),
                original_query VARCHAR(255),
                last_check_stock INTEGER,
                status VARCHAR(20)
            )
        """))
        conn.execute(text("""
            CREATE TABLE action_intent (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                action_type VARCHAR(100) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                payload_hash VARCHAR(64) NOT NULL,
                payload_json TEXT NOT NULL,
                operator VARCHAR(120) NOT NULL,
                brand VARCHAR(50) NOT NULL,
                release VARCHAR(120) NOT NULL,
                permission VARCHAR(120) NOT NULL,
                query_text VARCHAR(255),
                target VARCHAR(255),
                status VARCHAR(40) NOT NULL,
                version INTEGER NOT NULL,
                parent_intent_id INTEGER
            )
        """))
        conn.execute(text("""
            CREATE TABLE action_attempt (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                attempt_no INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                started_at DATETIME NOT NULL,
                finished_at DATETIME,
                status VARCHAR(40) NOT NULL,
                version INTEGER NOT NULL,
                claim_token VARCHAR(64),
                lease_expires_at DATETIME,
                external_request_id VARCHAR(255),
                response_summary TEXT
            )
        """))
        if include_fence_evidence:
            conn.execute(text("""
                CREATE TABLE action_fence_evidence (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    intent_id INTEGER NOT NULL,
                    attempt_id INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    external_request_id VARCHAR(255) NOT NULL,
                    execution_generation INTEGER NOT NULL,
                    fence_claim_token VARCHAR(64) NOT NULL,
                    status VARCHAR(40) NOT NULL,
                    evidence_payload TEXT NOT NULL,
                    fact_hash VARCHAR(64) NOT NULL
                )
            """))


def _schema_snapshot(engine):
    inspector = inspect(engine)
    return {
        "tables": sorted(inspector.get_table_names()),
        "columns": {
            table_name: [column["name"] for column in inspector.get_columns(table_name)]
            for table_name in inspector.get_table_names()
        },
        "indexes": {
            table_name: sorted(index["name"] for index in inspector.get_indexes(table_name))
            for table_name in inspector.get_table_names()
        },
        "checks": {
            table_name: sorted(
                constraint["name"]
                for constraint in inspector.get_check_constraints(table_name)
                if constraint.get("name")
            )
            for table_name in inspector.get_table_names()
        },
    }


def test_release_schema_contract_covers_query_governance_image_assets():
    table_name = QueryGovernanceImageAsset.__tablename__

    assert table_name in RELEASE_VERIFICATION_TABLES
    assert table_name in prod_migration.RELEASE_SCHEMA_COLUMN_MODELS
    assert table_name in prod_migration.RELEASE_IDENTITY_TABLE_MODELS
    assert {
        column.name for column in QueryGovernanceImageAsset.__table__.columns
    } == {
        column_name
        for column_name, _definition
        in prod_migration.RELEASE_VERIFICATION_SCHEMA_COLUMNS[table_name]
    }


def test_release_schema_contract_covers_shared_context_snapshot_charset():
    table_name = QueryGovernanceContextSnapshot.__tablename__

    assert table_name in RELEASE_VERIFICATION_TABLES
    assert table_name in prod_migration.RELEASE_SCHEMA_COLUMN_MODELS
    assert table_name in prod_migration.RELEASE_IDENTITY_TABLE_MODELS
    assert prod_migration.RELEASE_TABLE_CHARSET_SPECS[table_name] == {
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci",
    }
    ddl = str(CreateTable(QueryGovernanceContextSnapshot.__table__).compile(
        dialect=mysql_dialect()
    ))
    assert "CHARSET=utf8mb4" in ddl
    assert "COLLATE utf8mb4_unicode_ci" in ddl


def test_release_schema_creates_and_checks_shared_context_snapshot(engine):
    ensure_release_schema(engine)

    verification = verify_release_schema(engine)
    assert verification["tableCharsets"]["query_governance_context_snapshot"] is True
    assert verification["ok"] is True


@pytest.mark.parametrize(
    ("table_collation", "payload_collation", "should_convert"),
    [
        ("utf8_general_ci", "utf8_general_ci", True),
        ("utf8mb4_unicode_ci", "utf8_general_ci", True),
        ("utf8mb4_unicode_ci", "utf8mb4_unicode_ci", False),
    ],
)
def test_release_charset_migration_converts_only_mismatched_mysql_tables(
    monkeypatch, table_collation, payload_collation, should_convert
):
    connection = MagicMock()

    def execute(statement, _params=None):
        sql = str(statement)
        result = MagicMock()
        if "information_schema.TABLES" in sql:
            result.scalar_one_or_none.return_value = table_collation
        elif "information_schema.COLUMNS" in sql:
            result.scalar_one_or_none.return_value = payload_collation
        return result

    connection.execute.side_effect = execute
    engine = MagicMock()
    engine.dialect = mysql_dialect()
    engine.begin.return_value.__enter__.return_value = connection
    monkeypatch.setattr(
        prod_migration,
        "inspect",
        lambda _engine: SimpleNamespace(
            get_table_names=lambda: ["query_governance_context_snapshot"]
        ),
    )

    changes = prod_migration.ensure_release_table_charsets(
        engine, prod_migration.RELEASE_TABLE_CHARSET_SPECS
    )

    alters = [
        str(call.args[0])
        for call in connection.execute.call_args_list
        if str(call.args[0]).startswith("ALTER TABLE")
    ]
    assert len(alters) == int(should_convert)
    assert len(changes) == int(should_convert)
    if should_convert:
        assert "CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" in alters[0]


@pytest.mark.parametrize(
    ("table_collation", "payload_charset", "expected"),
    [
        ("utf8_general_ci", "utf8", False),
        ("utf8mb4_unicode_ci", "utf8", False),
        ("utf8mb4_unicode_ci", "utf8mb4", True),
    ],
)
def test_release_charset_verification_checks_table_and_payload(
    table_collation, payload_charset, expected
):
    connection = MagicMock()

    def execute(statement, _params=None):
        result = MagicMock()
        if "information_schema.TABLES" in str(statement):
            result.scalar_one_or_none.return_value = table_collation
        else:
            result.one_or_none.return_value = (
                payload_charset,
                "utf8mb4_unicode_ci" if payload_charset == "utf8mb4" else "utf8_general_ci",
            )
        return result

    connection.execute.side_effect = execute
    engine = MagicMock()
    engine.dialect = mysql_dialect()
    engine.connect.return_value.__enter__.return_value = connection

    assert prod_migration._release_table_charset_matches(
        engine,
        "query_governance_context_snapshot",
        prod_migration.RELEASE_TABLE_CHARSET_SPECS["query_governance_context_snapshot"],
    ) is expected


def test_release_schema_creates_query_governance_image_asset_table(engine):
    ensure_release_schema(engine)

    inspector = inspect(engine)
    table_name = QueryGovernanceImageAsset.__tablename__
    assert table_name in inspector.get_table_names()
    assert {
        index["name"] for index in inspector.get_indexes(table_name)
    } >= {
        "ix_qg_image_asset_brand_spu",
        "ix_qg_image_asset_source_hash",
    }
    assert verify_release_schema(engine)["ok"] is True


def _table_data_snapshot(engine, table_names):
    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as conn:
        return {
            table_name: [tuple(row) for row in conn.execute(text(
                f"SELECT * FROM {quote(table_name)} ORDER BY id"
            )).all()]
            for table_name in table_names
        }


def _create_action_fact_schema_without_integrity_constraints(engine):
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE action_fence_evidence"))
        conn.execute(text("DROP TABLE action_readback"))
        conn.execute(text("DROP TABLE action_terminal_fact"))
        conn.execute(text("DROP TABLE action_attempt"))
        conn.execute(text("DROP TABLE action_intent"))
        conn.execute(text("""
            CREATE TABLE action_intent (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                action_type VARCHAR(100) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                payload_hash VARCHAR(64) NOT NULL,
                payload_json TEXT NOT NULL,
                operator VARCHAR(120) NOT NULL,
                brand VARCHAR(50) NOT NULL,
                release VARCHAR(120) NOT NULL,
                permission VARCHAR(120) NOT NULL,
                query_text VARCHAR(255),
                target VARCHAR(255),
                status VARCHAR(40) NOT NULL,
                version INTEGER NOT NULL,
                parent_intent_id INTEGER
            )
        """))
        conn.execute(text("""
            CREATE TABLE action_attempt (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                attempt_no INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                started_at DATETIME NOT NULL,
                finished_at DATETIME,
                status VARCHAR(40) NOT NULL,
                version INTEGER NOT NULL,
                execution_generation INTEGER NOT NULL DEFAULT 0,
                claim_token VARCHAR(64),
                lease_expires_at DATETIME,
                external_request_id VARCHAR(255),
                response_summary TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE action_fence_evidence (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                attempt_id INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                external_request_id VARCHAR(255) NOT NULL,
                execution_generation INTEGER NOT NULL,
                fence_claim_token VARCHAR(64) NOT NULL,
                status VARCHAR(40) NOT NULL,
                evidence_payload TEXT NOT NULL,
                fact_hash VARCHAR(64) NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE action_readback (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                attempt_id INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                status VARCHAR(40) NOT NULL,
                authoritative INTEGER NOT NULL,
                observed_payload TEXT NOT NULL,
                fact_hash VARCHAR(64) NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE action_terminal_fact (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                status VARCHAR(40) NOT NULL,
                terminal_code VARCHAR(80) NOT NULL,
                result_json TEXT NOT NULL,
                replay_result_json TEXT NOT NULL,
                fact_hash VARCHAR(64) NOT NULL
            )
        """))


def test_legacy_action_schema_is_upgraded_and_idempotent(engine):
    _create_legacy_action_schema(engine)

    first = ensure_release_schema(engine)
    second = ensure_release_schema(engine)

    inspector = inspect(engine)
    inventory_columns = {
        column["name"] for column in inspector.get_columns("inventory_recovery_task")
    }
    attempt_columns = {
        column["name"] for column in inspector.get_columns("action_attempt")
    }
    fence_columns = {
        column["name"] for column in inspector.get_columns("action_fence_evidence")
    }
    fence_indexes = {
        index["name"] for index in inspector.get_indexes("action_fence_evidence")
    }
    fence_uniques = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("action_fence_evidence")
    }

    assert {"last_action_request_id", "last_action_generation"}.issubset(inventory_columns)
    assert "execution_generation" in attempt_columns
    assert set(ActionFenceEvidence.__table__.columns.keys()).issubset(fence_columns)
    assert "ix_action_fence_intent_created" in fence_indexes
    assert "uq_action_fence_attempt_generation" in fence_uniques
    assert {
        ("inventory_recovery_task", "last_action_request_id"),
        ("inventory_recovery_task", "last_action_generation"),
        ("action_attempt", "execution_generation"),
    }.issubset({(item["table"], item["column"]) for item in first["addedColumns"]})
    assert second["addedColumns"] == []
    assert verify_release_schema(engine)["ok"] is True


def test_duplicate_fence_evidence_blocks_before_schema_changes(engine):
    _create_legacy_action_schema(engine, include_fence_evidence=True)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_fence_evidence (
                intent_id, attempt_id, created_at, external_request_id,
                execution_generation, fence_claim_token, status,
                evidence_payload, fact_hash
            ) VALUES
                (1, 7, CURRENT_TIMESTAMP, 'request-1', 3, 'claim-1', 'fenced', '{}', 'a'),
                (1, 7, CURRENT_TIMESTAMP, 'request-2', 3, 'claim-2', 'fenced', '{}', 'b')
        """))
    before = _schema_snapshot(engine)

    with pytest.raises(RuntimeError, match="duplicate unique key values detected"):
        ensure_release_schema(engine)

    assert _schema_snapshot(engine) == before


def test_missing_action_fact_integrity_assets_are_rebuilt_and_idempotent(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)

    before = verify_release_schema(engine)
    first = ensure_release_schema(engine)
    second = ensure_release_schema(engine)

    assert before["ok"] is False
    assert {
        model.__tablename__
        for model in (
            ActionIntent,
            ActionAttempt,
            ActionFenceEvidence,
            ActionReadback,
            ActionTerminalFact,
        )
    }.issubset(RELEASE_VERIFICATION_TABLES)
    assert {
        spec["name"] for spec in AIOPS127_FOREIGN_KEY_SPECS
    }.issubset(before["foreignKeys"])
    assert all(
        before["foreignKeys"][spec["name"]] is False
        for spec in AIOPS127_FOREIGN_KEY_SPECS
    )
    assert {
        spec["name"] for spec in AIOPS127_FOREIGN_KEY_SPECS
    }.issubset(first["ensuredForeignKeys"])
    assert all(
        inspect(engine).get_foreign_keys(str(spec["table"]))
        for spec in AIOPS127_FOREIGN_KEY_SPECS
    )
    assert all(
        str(spec["name"]) in {
            item["name"]
            for item in inspect(engine).get_unique_constraints(str(spec["table"]))
        }
        for spec in AIOPS127_UNIQUE_SPECS
    )
    assert {
        "ix_action_intent_status_created",
        "ix_action_attempt_intent_created",
        "ix_action_attempt_status_lease",
        "ix_action_fence_intent_created",
        "ix_action_terminal_fact_status_created",
    }.issubset({
        item["name"]
        for table_name in (
            "action_intent",
            "action_attempt",
            "action_fence_evidence",
            "action_terminal_fact",
        )
        for item in inspect(engine).get_indexes(table_name)
    })
    assert {
        model.__tablename__: {
            column.name for column in model.__table__.columns
        }
        for model in (
            ActionIntent,
            ActionAttempt,
            ActionFenceEvidence,
            ActionReadback,
            ActionTerminalFact,
        )
    } == {
        table_name: {
            column["name"]
            for column in inspect(engine).get_columns(table_name)
        }
        for table_name in (
            "action_intent",
            "action_attempt",
            "action_fence_evidence",
            "action_readback",
            "action_terminal_fact",
        )
    }
    assert second["ensuredForeignKeys"] == []
    assert verify_release_schema(engine)["ok"] is True


def test_wrong_named_action_attempt_index_blocks_before_schema_changes(engine):
    _create_legacy_action_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE INDEX ix_action_attempt_intent_created
            ON action_attempt (status, created_at, id)
        """))
    before = _schema_snapshot(engine)

    with pytest.raises(RuntimeError, match="index mismatch: ix_action_attempt_intent_created"):
        ensure_release_schema(engine)

    assert _schema_snapshot(engine) == before


def test_action_governance_mysql_ddl_is_offline_compilable():
    dialect = mysql_dialect()
    create_table = str(CreateTable(ActionFenceEvidence.__table__).compile(dialect=dialect))
    column_definitions = {
        table_name: [
            str(column.compile(dialect=dialect))
            for _column_name, column in columns
        ]
        for table_name, columns in AIOPS127_SCHEMA_COLUMNS.items()
    }
    index_statements = [
        _build_create_index_sql(SimpleNamespace(dialect=dialect), spec)
        for spec in (*AIOPS127_UNIQUE_SPECS, *AIOPS127_INDEX_SPECS)
    ]

    assert "CREATE TABLE action_fence_evidence" in create_table
    assert "CONSTRAINT uq_action_fence_attempt_generation UNIQUE" in create_table
    assert "FOREIGN KEY(intent_id) REFERENCES action_intent (id)" in create_table
    assert (
        "CONSTRAINT fk_action_fence_attempt_identity "
        "FOREIGN KEY(attempt_id, intent_id, external_request_id, execution_generation) "
        "REFERENCES action_attempt "
        "(id, intent_id, external_request_id, execution_generation)"
    ) in create_table
    assert any("last_action_request_id" in item for item in column_definitions["inventory_recovery_task"])
    assert any("last_action_generation" in item for item in column_definitions["inventory_recovery_task"])
    assert any("execution_generation" in item for item in column_definitions["action_attempt"])
    assert any("uq_action_fence_attempt_generation" in item for item in index_statements)
    assert any("ix_action_fence_intent_created" in item for item in index_statements)
    assert all(spec["mysql_online"] is True for spec in (
        *AIOPS127_UNIQUE_SPECS,
        *AIOPS127_INDEX_SPECS,
    ))
    assert all(
        statement.endswith("ALGORITHM=INPLACE LOCK=NONE")
        for statement in index_statements
    )
    assert AIOPS127_MYSQL_COLUMN_RISK_EVIDENCE == {
        "transactionalRollback": False,
        "failureRecovery": "verify_and_rerun_idempotently",
        "columns": {
            "inventory_recovery_task.last_action_request_id",
            "inventory_recovery_task.last_action_generation",
            "action_attempt.execution_generation",
        },
    }


def _action_fact_check_names():
    return {
        constraint.name
        for model in (
            ActionIntent,
            ActionAttempt,
            ActionFenceEvidence,
            ActionReadback,
            ActionTerminalFact,
        )
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_action_fact_checks_are_migrated_verified_and_idempotent(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    expected_checks = _action_fact_check_names()
    expected_release_checks = {str(spec["name"]) for spec in RELEASE_CHECK_SPECS}

    before = verify_release_schema(engine)
    first = ensure_release_schema(engine)
    second = ensure_release_schema(engine)
    after = verify_release_schema(engine)

    assert expected_checks
    assert set(before["checkConstraints"]) == expected_release_checks
    assert all(before["checkConstraints"][name] is False for name in expected_checks)
    assert all(
        before["checkConstraints"][name] is True
        for name in expected_release_checks - expected_checks
    )
    assert set(first["ensuredCheckConstraints"]) == expected_checks
    assert second["ensuredCheckConstraints"] == []
    assert all(after["checkConstraints"][name] is True for name in expected_release_checks)
    assert after["ok"] is True


def test_invalid_action_fact_rows_block_before_schema_changes(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'invalid-brand', :payload_hash, '{}', 'operator-1', '',
                'release-test', 'execute_governance_actions', 'pending', 0
            )
        """), {"payload_hash": _canonical_json_hash({})[1]})
    before = _schema_snapshot(engine)
    scanner = getattr(
        prod_migration,
        "inspect_action_fact_constraint_violations",
        None,
    )

    assert scanner is not None
    assert scanner(engine) == [
        {
            "table": "action_intent",
            "name": "ck_action_intent_brand_nonempty",
            "count": 1,
        }
    ]
    with pytest.raises(RuntimeError, match="action fact constraint violations"):
        ensure_release_schema(engine)
    assert _schema_snapshot(engine) == before


def test_action_fact_mysql_check_ddl_is_offline_compilable():
    specs = getattr(prod_migration, "AIOPS131_CHECK_SPECS", None)
    builder = getattr(prod_migration, "_build_add_check_constraint_sql", None)

    assert specs is not None
    assert builder is not None
    engine = SimpleNamespace(dialect=mysql_dialect())
    statements = [builder(engine, spec) for spec in specs]

    assert len(statements) == len(_action_fact_check_names())
    assert all(statement.startswith("ALTER TABLE ") for statement in statements)
    assert all(" ADD CONSTRAINT " in statement for statement in statements)
    assert all(" CHECK (" in statement for statement in statements)


def test_action_intent_release_check_quotes_mysql_family_reserved_identifier():
    spec = next(
        item
        for item in prod_migration.AIOPS131_CHECK_SPECS
        if item["name"] == "ck_action_intent_release_nonempty"
    )

    for dialect in (mysql_dialect(), MariaDBDialect()):
        create_table = str(
            CreateTable(ActionIntent.__table__).compile(dialect=dialect)
        )
        alter_table = prod_migration._build_add_check_constraint_sql(
            SimpleNamespace(dialect=dialect),
            spec,
        )

        assert "trim(`release`)" in create_table
        assert "trim(`release`)" in alter_table


def test_scheduler_job_run_release_check_quotes_mysql_family_reserved_identifier():
    spec = next(
        item
        for item in prod_migration.AIOPS130B_CHECK_SPECS
        if item["name"] == "ck_scheduler_job_run_release_nonempty"
    )

    for dialect in (mysql_dialect(), MariaDBDialect()):
        create_table = str(
            CreateTable(SchedulerJobRun.__table__).compile(dialect=dialect)
        )
        alter_table = prod_migration._build_add_check_constraint_sql(
            SimpleNamespace(dialect=dialect),
            spec,
        )

        assert "trim(`release`)" in create_table
        assert "trim(`release`)" in alter_table

    assert prod_migration.AIOPS131_LEGACY_CHECK_SQL[
        (ActionIntent.__tablename__, "ck_action_intent_release_nonempty")
    ] == ("length(trim(`release`)) > 0",)


def test_mysql_action_attempt_reference_index_precedes_foreign_keys(monkeypatch):
    calls = []
    identity_index = next(
        spec
        for spec in AIOPS127_UNIQUE_SPECS
        if spec["name"] == "uq_action_attempt_identity_facts"
    )
    engine = SimpleNamespace(dialect=mysql_dialect())

    monkeypatch.setattr(
        prod_migration,
        "_raise_on_release_schema_preflight_blockers",
        lambda _engine: None,
    )
    monkeypatch.setattr(Base.metadata, "create_all", lambda **_kwargs: None)
    monkeypatch.setattr(
        prod_migration,
        "ensure_release_primary_keys",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        prod_migration,
        "ensure_release_schema_upgrades",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        prod_migration,
        "ensure_action_attempt_external_request_ids",
        lambda _engine: 0,
    )
    monkeypatch.setattr(
        prod_migration,
        "ensure_release_column_type_upgrades",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        prod_migration,
        "_raise_on_column_contract_mismatches",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(prod_migration, "inspect", lambda _engine: object())
    monkeypatch.setattr(
        prod_migration,
        "_schema_foreign_key_matches",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        prod_migration,
        "ensure_action_fact_check_constraints",
        lambda _engine: [],
    )

    def record_foreign_keys(_engine, _specs):
        calls.append(("foreign_keys", ()))
        return []

    def record_indexes(_engine, specs):
        names = tuple(str(spec["name"]) for spec in specs)
        calls.append(("indexes", names))
        return []

    monkeypatch.setattr(
        prod_migration,
        "ensure_release_foreign_keys",
        record_foreign_keys,
    )
    monkeypatch.setattr(
        prod_migration,
        "ensure_action_attempt_external_request_id_not_null",
        lambda _engine: calls.append(("not_null", ())),
    )
    monkeypatch.setattr(
        prod_migration,
        "ensure_release_indexes",
        record_indexes,
    )
    monkeypatch.setattr(
        prod_migration,
        "verify_release_schema",
        lambda _engine: {"ok": True},
    )

    prod_migration.ensure_release_schema(engine, log=lambda _message: None)

    assert identity_index["columns"] == (
        "id",
        "intent_id",
        "external_request_id",
        "execution_generation",
    )
    identity_index_position = next(
        index
        for index, call in enumerate(calls)
        if call[0] == "indexes" and identity_index["name"] in call[1]
    )
    foreign_key_position = next(
        index for index, call in enumerate(calls) if call[0] == "foreign_keys"
    )
    assert identity_index_position < foreign_key_position
    not_null_position = next(
        index for index, call in enumerate(calls) if call[0] == "not_null"
    )
    assert not_null_position < foreign_key_position


def test_mysql_nullable_request_id_with_existing_reference_fk_fails_closed(
    monkeypatch,
):
    class PartialMigrationInspector:
        def get_table_names(self):
            return ["action_attempt", "action_fence_evidence"]

        def get_columns(self, table_name):
            assert table_name == "action_attempt"
            return [{"name": "external_request_id", "nullable": True}]

        def get_foreign_keys(self, table_name):
            if table_name != "action_fence_evidence":
                return []
            return [{
                "name": "fk_action_fence_attempt_identity",
                "referred_table": "action_attempt",
                "referred_columns": [
                    "id",
                    "intent_id",
                    "external_request_id",
                    "execution_generation",
                ],
            }]

    mutation_attempted = {"value": False}

    def begin():
        mutation_attempted["value"] = True
        raise AssertionError("partial migration must fail before ALTER TABLE")

    monkeypatch.setattr(
        prod_migration,
        "inspect",
        lambda _engine: PartialMigrationInspector(),
    )
    engine = SimpleNamespace(dialect=mysql_dialect(), begin=begin)

    with pytest.raises(
        RuntimeError,
        match=(
            "manual recovery required.*action_attempt.external_request_id.*"
            "fk_action_fence_attempt_identity"
        ),
    ):
        prod_migration.ensure_action_attempt_external_request_id_not_null(engine)

    assert mutation_attempted["value"] is False


def test_wrong_named_action_fact_check_blocks_before_schema_changes(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE action_intent"))
        conn.execute(text("""
            CREATE TABLE action_intent (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                action_type VARCHAR(100) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                payload_hash VARCHAR(64) NOT NULL,
                payload_json TEXT NOT NULL,
                operator VARCHAR(120) NOT NULL,
                brand VARCHAR(50) NOT NULL,
                release VARCHAR(120) NOT NULL,
                permission VARCHAR(120) NOT NULL,
                query_text VARCHAR(255),
                target VARCHAR(255),
                status VARCHAR(40) NOT NULL,
                version INTEGER NOT NULL,
                parent_intent_id INTEGER,
                CONSTRAINT ck_action_intent_brand_nonempty
                    CHECK (length(trim(brand)) >= 0)
            )
        """))
    before = _schema_snapshot(engine)

    with pytest.raises(RuntimeError, match="check constraint mismatch"):
        ensure_release_schema(engine)

    assert _schema_snapshot(engine) == before


def test_sqlite_action_fact_check_migration_recovers_after_injected_failure(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    before = _schema_snapshot(engine)
    failed_once = {"value": False}

    def fail_mid_rebuild(_conn, _cursor, statement, _parameters, _context, _many):
        if (
            not failed_once["value"]
            and "CREATE TABLE __aiops131_action_readback_new" in statement
        ):
            failed_once["value"] = True
            raise RuntimeError("injected action fact migration failure")

    event.listen(engine, "before_cursor_execute", fail_mid_rebuild)
    try:
        with pytest.raises(RuntimeError, match="injected action fact migration failure"):
            ensure_release_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", fail_mid_rebuild)

    assert failed_once["value"] is True
    assert _schema_snapshot(engine) == before
    assert ensure_release_schema(engine)["ensuredCheckConstraints"]
    assert verify_release_schema(engine)["ok"] is True


def test_sqlite_action_fact_rebuild_blocks_unmodeled_columns_without_data_loss(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE action_intent ADD COLUMN legacy_note TEXT"))
        conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version, legacy_note
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'legacy-note', :payload_hash, '{}', 'operator-1', 'atlas',
                'release-test', 'execute_governance_actions', 'pending', 0,
                'must-survive'
            )
        """), {"payload_hash": _canonical_json_hash({})[1]})
    before_schema = _schema_snapshot(engine)
    before_data = _table_data_snapshot(engine, ["action_intent"])

    with pytest.raises(RuntimeError, match="unexpected column: action_intent.legacy_note"):
        ensure_release_schema(engine)

    assert _schema_snapshot(engine) == before_schema
    assert _table_data_snapshot(engine, ["action_intent"]) == before_data


@pytest.mark.parametrize("foreign_keys_enabled", [False, True])
@pytest.mark.parametrize("orphan_kind", ["intent", "attempt"])
def test_sqlite_action_fact_rebuild_blocks_orphans_before_commit_and_rerun(
    engine,
    foreign_keys_enabled,
    orphan_kind,
):
    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        if orphan_kind == "intent":
            conn.execute(text("""
                INSERT INTO action_attempt (
                    intent_id, attempt_no, created_at, started_at, status,
                    version, execution_generation, external_request_id
                ) VALUES (
                    999, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'persisted',
                    0, 0, 'orphan-intent'
                )
            """))
        else:
            intent_id = conn.execute(text("""
                INSERT INTO action_intent (
                    created_at, updated_at, action_type, idempotency_key,
                    payload_hash, payload_json, operator, brand, release,
                    permission, status, version
                ) VALUES (
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                    'orphan-attempt', :payload_hash, '{}', 'operator-1', 'atlas',
                    'release-test', 'execute_governance_actions', 'pending', 0
                )
            """), {"payload_hash": _canonical_json_hash({})[1]}).lastrowid
            conn.execute(text("""
                INSERT INTO action_readback (
                    intent_id, attempt_id, created_at, status, authoritative,
                    observed_payload, fact_hash
                ) VALUES (
                    :intent_id, 999, CURRENT_TIMESTAMP, 'success', 1, '{}',
                    :fact_hash
                )
            """), {
                "intent_id": intent_id,
                "fact_hash": _canonical_json_hash({})[1],
            })

    with engine.connect() as conn:
        conn.exec_driver_sql(
            "PRAGMA foreign_keys=" + ("ON" if foreign_keys_enabled else "OFF")
        )
        conn.commit()
    before_schema = _schema_snapshot(engine)
    before_data = _table_data_snapshot(
        engine,
        ["action_intent", "action_attempt", "action_readback"],
    )

    for _attempt in range(2):
        with pytest.raises(
            RuntimeError,
            match="action fact sqlite rebuild foreign key violations",
        ):
            ensure_release_schema(engine)
        assert _schema_snapshot(engine) == before_schema
        assert _table_data_snapshot(
            engine,
            ["action_intent", "action_attempt", "action_readback"],
        ) == before_data


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        (
            "status IN ('pending', 'success')",
            "(`status` in (_utf8mb4'pending',_utf8mb4'success'))",
        ),
        (
            "status = 'fenced'",
            "(`status` = _txsql_utf8mb4'fenced')",
        ),
        (
            "length(replace(trim(`release`), char(9), '')) > 0",
            "(length(replace(trim(`release`),char(9),_utf8mb4'')) > 0)",
        ),
        (
            "length(replace(trim(`release`), char(9), '')) > 0",
            "octet_length(replace(trim(`release`),convert(char(9) using utf8mb4),'')) > 0",
        ),
        (
            "(status = 'success' AND code IN ('a', 'b')) OR (status = 'failed' AND code = 'c')",
            "status = 'success' and code in ('a','b') or status = 'failed' and code = 'c'",
        ),
    ],
)
def test_action_fact_check_normalization_matches_mysql_family_forms(
    expected,
    actual,
):
    assert prod_migration._normalize_check_sql(expected) == (
        prod_migration._normalize_check_sql(actual)
    )


def test_mysql_check_match_uses_information_schema_when_reflection_omits_clause():
    spec = next(
        item
        for item in RELEASE_CHECK_SPECS
        if item["name"] == "ck_action_intent_status"
    )

    class Inspector:
        def get_table_names(self):
            return [str(spec["table"])]

        def get_check_constraints(self, table_name):
            assert table_name == spec["table"]
            return [{"name": spec["name"], "sqltext": ""}]

    def build_engine(check_clause):
        calls = []

        class Result:
            def mappings(self):
                return self

            def all(self):
                return [{"name": spec["name"], "sqltext": check_clause}]

        class ShowCreateResult:
            def mappings(self):
                return self

            def one(self):
                return {
                    "Table": str(spec["table"]),
                    "Create Table": (
                        "CREATE TABLE `action_intent` ("
                        f"CONSTRAINT `{spec['name']}` CHECK ({check_clause})"
                        ")"
                    ),
                }

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, statement, parameters):
                calls.append((str(statement), parameters))
                return Result()

            def exec_driver_sql(self, _statement):
                return ShowCreateResult()

        return SimpleNamespace(
            dialect=mysql_dialect(),
            connect=lambda: Connection(),
        ), calls

    engine, calls = build_engine(str(spec["sqltext"]))
    assert prod_migration._schema_check_matches(
        Inspector(),
        spec,
        engine=engine,
    ) is True
    assert len(calls) == 1
    assert "information_schema.TABLE_CONSTRAINTS" in calls[0][0]
    assert calls[0][1] == {"table_name": str(spec["table"])}

    mismatched_engine, _ = build_engine("status IN ('pending')")
    assert prod_migration._schema_check_matches(
        Inspector(),
        spec,
        engine=mismatched_engine,
    ) is False


def test_txsql_legacy_check_replacement_is_exactly_fingerprinted(monkeypatch):
    spec = next(
        item
        for item in RELEASE_CHECK_SPECS
        if item["name"] == "ck_action_attempt_status"
    )
    engine = SimpleNamespace(dialect=mysql_dialect())
    known_digest = (
        "e01d2d728d450cf2dbde8a33edb141895833f7bc675b311d67fc542a781291bb"
    )

    monkeypatch.setattr(
        prod_migration,
        "inspect_database_check_enforcement",
        lambda _engine: {
            "family": "txsql",
            "version": "8.0.22-txsql",
            "checkConstraintsEnforced": True,
        },
    )

    assert prod_migration._is_exact_txsql_legacy_check_replacement(
        engine,
        spec,
        known_digest,
    ) is True
    assert prod_migration._is_exact_txsql_legacy_check_replacement(
        engine,
        spec,
        "0" * 64,
    ) is False

    monkeypatch.setattr(
        prod_migration,
        "inspect_database_check_enforcement",
        lambda _engine: {
            "family": "mysql",
            "version": "8.0.36",
            "checkConstraintsEnforced": True,
        },
    )
    assert prod_migration._is_exact_txsql_legacy_check_replacement(
        engine,
        spec,
        known_digest,
    ) is False


def test_txsql_check_replacement_uses_one_alter_per_table():
    spec = next(
        item
        for item in RELEASE_CHECK_SPECS
        if item["name"] == "ck_action_attempt_status"
    )
    engine = SimpleNamespace(dialect=mysql_dialect())

    statement = prod_migration._build_replace_check_constraints_sql(engine, [spec])
    quote = engine.dialect.identifier_preparer.quote

    assert statement.startswith(f"ALTER TABLE {quote('action_attempt')} ")
    assert f"DROP CHECK {quote('ck_action_attempt_status')}" in statement
    assert (
        f"ADD CONSTRAINT {quote('ck_action_attempt_status')} CHECK (" in statement
    )


def test_txsql_exact_legacy_check_is_replaced_after_data_preflight(monkeypatch):
    target = next(
        item
        for item in RELEASE_CHECK_SPECS
        if item["name"] == "ck_action_attempt_status"
    )
    action_attempt_specs = [
        item
        for item in RELEASE_CHECK_SPECS
        if item["table"] == "action_attempt"
    ]
    replaced = {"value": False}
    statements = []
    scanned = []

    class Inspector:
        def get_table_names(self):
            return ["action_attempt"]

        def get_check_constraints(self, table_name):
            assert table_name == "action_attempt"
            return [{"name": item["name"], "sqltext": "legacy"} for item in action_attempt_specs]

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            statements.append(str(statement))
            replaced["value"] = True

    engine = SimpleNamespace(dialect=mysql_dialect(), begin=lambda: Transaction())

    monkeypatch.setattr(prod_migration, "inspect", lambda _engine: Inspector())
    monkeypatch.setattr(
        prod_migration,
        "_schema_check_matches",
        lambda _inspector, spec, **_kwargs: (
            spec["name"] != target["name"] or replaced["value"]
        ),
    )
    monkeypatch.setattr(
        prod_migration,
        "_mysql_show_create_check_sqltexts",
        lambda *_args, **_kwargs: {target["name"]: "legacy"},
    )
    monkeypatch.setattr(
        prod_migration,
        "_is_exact_txsql_legacy_check_replacement",
        lambda _engine, spec, _digest: spec["name"] == target["name"],
    )
    monkeypatch.setattr(
        prod_migration,
        "inspect_action_fact_constraint_violations",
        lambda _engine, specs: scanned.extend(specs) or [],
    )

    assert prod_migration.ensure_action_fact_check_constraints(engine) == [
        target["name"]
    ]
    assert scanned == [target]
    assert len(statements) == 1
    assert "DROP CHECK" in statements[0]
    assert "ADD CONSTRAINT" in statements[0]


def test_mysql_check_match_uses_show_create_when_metadata_omits_clause():
    spec = next(
        item
        for item in RELEASE_CHECK_SPECS
        if item["name"] == "ck_action_intent_status"
    )

    class Inspector:
        def get_table_names(self):
            return [str(spec["table"])]

        def get_check_constraints(self, table_name):
            assert table_name == spec["table"]
            return [{"name": spec["name"], "sqltext": ""}]

    def build_engine(check_clause):
        calls = []
        create_sql = (
            "CREATE TABLE `action_intent` (\n"
            "  `id` bigint NOT NULL,\n"
            f"  CONSTRAINT `{spec['name']}` CHECK ({check_clause})\n"
            ") ENGINE=InnoDB"
        )

        class InformationResult:
            def mappings(self):
                return self

            def all(self):
                return []

        class ShowCreateResult:
            def mappings(self):
                return self

            def one(self):
                return {"Table": str(spec["table"]), "Create Table": create_sql}

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, statement, parameters):
                calls.append(("information_schema", str(statement), parameters))
                return InformationResult()

            def exec_driver_sql(self, statement):
                calls.append(("show_create", statement))
                return ShowCreateResult()

        return SimpleNamespace(
            dialect=mysql_dialect(),
            connect=lambda: Connection(),
        ), calls

    engine, calls = build_engine(str(spec["sqltext"]))
    assert prod_migration._schema_check_matches(
        Inspector(),
        spec,
        engine=engine,
    ) is True
    assert calls[-1] == (
        "show_create",
        "SHOW CREATE TABLE "
        + engine.dialect.identifier_preparer.quote(str(spec["table"])),
    )

    mismatched_engine, _ = build_engine("status IN ('pending')")
    assert prod_migration._schema_check_matches(
        Inspector(),
        spec,
        engine=mismatched_engine,
    ) is False


def test_txsql_request_id_type_qualifier_compatibility_is_narrow(monkeypatch):
    expected = dict(AIOPS127_SCHEMA_COLUMNS["inventory_recovery_task"])[
        "last_action_request_id"
    ]
    actual = {
        "type": VARCHAR(255, charset="utf8mb4"),
        "nullable": True,
        "default": None,
    }
    engine = SimpleNamespace(dialect=mysql_dialect())
    monkeypatch.setattr(
        prod_migration,
        "inspect_database_check_enforcement",
        lambda _engine: {
            "family": "txsql",
            "version": "8.0.22-txsql",
            "checkConstraintsEnforced": True,
        },
    )

    assert prod_migration._column_contract_check(
        actual,
        expected,
        engine.dialect,
    )["type"] is False
    assert prod_migration._release_column_contract_check(
        engine,
        "inventory_recovery_task",
        "last_action_request_id",
        actual,
        expected,
    )["ok"] is True
    assert prod_migration._release_column_contract_check(
        engine,
        "other_table",
        "last_action_request_id",
        actual,
        expected,
    )["type"] is False


def test_txsql_action_log_image_url_type_qualifier_is_compatible(monkeypatch):
    expected = dict(prod_migration.RELEASE_VERIFICATION_SCHEMA_COLUMNS[
        "automation_action_log"
    ])["image_url"]
    actual = {
        "type": MYSQL_TEXT(collation="utf8mb4_general_ci"),
        "nullable": True,
        "default": None,
    }
    engine = SimpleNamespace(dialect=mysql_dialect())
    monkeypatch.setattr(
        prod_migration,
        "inspect_database_check_enforcement",
        lambda _engine: {
            "family": "txsql",
            "version": "8.0.22-txsql",
            "checkConstraintsEnforced": True,
        },
    )

    assert prod_migration._column_contract_check(
        actual,
        expected,
        engine.dialect,
    )["type"] is False
    assert prod_migration._release_column_contract_check(
        engine,
        "automation_action_log",
        "image_url",
        actual,
        expected,
    )["ok"] is True


def test_context_snapshot_mysql_text_collation_and_mediumtext_are_compatible():
    expected = dict(prod_migration.RELEASE_VERIFICATION_SCHEMA_COLUMNS[
        "query_governance_context_snapshot"
    ])
    engine = SimpleNamespace(dialect=mysql_dialect())
    for column_name, actual_type in (
        ("batch_id", VARCHAR(120, collation="utf8mb4_unicode_ci")),
        ("payload", MYSQL_MEDIUMTEXT(collation="utf8mb4_unicode_ci")),
    ):
        actual = {"type": actual_type, "nullable": False, "default": None}
        assert prod_migration._release_column_contract_check(
            engine,
            "query_governance_context_snapshot",
            column_name,
            actual,
            expected[column_name],
        )["ok"] is True

    wrong_collation = {
        "type": MYSQL_MEDIUMTEXT(collation="utf8_general_ci"),
        "nullable": False,
        "default": None,
    }
    assert prod_migration._release_column_contract_check(
        engine,
        "query_governance_context_snapshot",
        "payload",
        wrong_collation,
        expected["payload"],
    )["type"] is False


def test_action_fact_check_comparison_preserves_string_literal_case(engine):
    assert prod_migration._normalize_check_sql(
        "status IN ('pending')"
    ) != prod_migration._normalize_check_sql(
        "STATUS IN ('PENDING')"
    )
    assert prod_migration._normalize_check_sql(
        "(status = 'success' OR code = 'failed') AND enabled = 1"
    ) != prod_migration._normalize_check_sql(
        "status = 'success' OR code = 'failed' AND enabled = 1"
    )

    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE action_intent"))
        conn.execute(text("""
            CREATE TABLE action_intent (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                action_type VARCHAR(100) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                payload_hash VARCHAR(64) NOT NULL,
                payload_json TEXT NOT NULL,
                operator VARCHAR(120) NOT NULL,
                brand VARCHAR(50) NOT NULL,
                release VARCHAR(120) NOT NULL,
                permission VARCHAR(120) NOT NULL,
                query_text VARCHAR(255),
                target VARCHAR(255),
                status VARCHAR(40) NOT NULL,
                version INTEGER NOT NULL,
                parent_intent_id INTEGER,
                CONSTRAINT ck_action_intent_status CHECK (
                    status IN (
                        'PENDING', 'SUCCESS', 'FAILED',
                        'EXECUTION_UNKNOWN', 'INCONCLUSIVE'
                    )
                )
            )
        """))
    before = _schema_snapshot(engine)

    with pytest.raises(RuntimeError, match="check constraint mismatch"):
        ensure_release_schema(engine)

    assert _schema_snapshot(engine) == before


def test_sqlite_action_fact_rebuild_blocks_unmodeled_autoindex(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE action_intent"))
        conn.execute(text("""
            CREATE TABLE action_intent (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                action_type VARCHAR(100) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                payload_hash VARCHAR(64) NOT NULL,
                payload_json TEXT NOT NULL,
                operator VARCHAR(120) NOT NULL,
                brand VARCHAR(50) NOT NULL,
                release VARCHAR(120) NOT NULL,
                permission VARCHAR(120) NOT NULL,
                query_text VARCHAR(255),
                target VARCHAR(255),
                status VARCHAR(40) NOT NULL,
                version INTEGER NOT NULL,
                parent_intent_id INTEGER,
                UNIQUE(action_type, operator)
            )
        """))
    with engine.connect() as conn:
        autoindexes = [
            row["name"]
            for row in conn.execute(text("PRAGMA index_list(action_intent)"))
            .mappings()
            .all()
            if row["origin"] == "u"
        ]
    before = _schema_snapshot(engine)

    assert autoindexes
    with pytest.raises(
        RuntimeError,
        match="action fact sqlite rebuild cannot replay autoindex",
    ):
        ensure_release_schema(engine)
    assert _schema_snapshot(engine) == before


def test_verify_release_schema_reports_sqlite_foreign_key_enforcement(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    ensure_release_schema(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()

    verification = verify_release_schema(engine)

    assert verification["foreignKeyEnforcement"] is False
    assert verification["ok"] is False


def test_verify_release_schema_reports_sqlite_foreign_key_data_violations(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    ensure_release_schema(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_attempt (
                intent_id, attempt_no, created_at, started_at, status,
                version, execution_generation, external_request_id
            ) VALUES (
                999, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'persisted',
                0, 0, 'verify-orphan'
            )
        """))
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.commit()

    verification = verify_release_schema(engine)

    assert verification["foreignKeyEnforcement"] is True
    assert verification["foreignKeyViolations"]
    assert verification["ok"] is False


def test_known_stage1_hash_check_is_upgraded_without_losing_data(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE action_intent"))
        conn.execute(text("""
            CREATE TABLE action_intent (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                action_type VARCHAR(100) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                payload_hash VARCHAR(64) NOT NULL,
                payload_json TEXT NOT NULL,
                operator VARCHAR(120) NOT NULL,
                brand VARCHAR(50) NOT NULL,
                release VARCHAR(120) NOT NULL,
                permission VARCHAR(120) NOT NULL,
                query_text VARCHAR(255),
                target VARCHAR(255),
                status VARCHAR(40) NOT NULL,
                version INTEGER NOT NULL,
                parent_intent_id INTEGER,
                CONSTRAINT ck_action_intent_payload_hash_sha256
                    CHECK (length(payload_hash) = 64)
            )
        """))
        conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'stage1-hash', :payload_hash, '{}', 'operator-1', 'atlas',
                'release-test', 'execute_governance_actions', 'pending', 0
            )
        """), {"payload_hash": _canonical_json_hash({})[1]})
    before_data = _table_data_snapshot(engine, ["action_intent"])

    result = ensure_release_schema(engine)

    assert "ck_action_intent_payload_hash_sha256" in result[
        "ensuredCheckConstraints"
    ]
    assert _table_data_snapshot(engine, ["action_intent"]) == before_data
    assert verify_release_schema(engine)["ok"] is True


def test_nullable_external_request_id_is_backfilled_and_enforced(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        intent_id = conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'request-backfill', :payload_hash, '{}', 'operator-1', 'atlas',
                'release-test', 'execute_governance_actions', 'pending', 0
            )
        """), {"payload_hash": _canonical_json_hash({})[1]}).lastrowid
        conn.execute(text("""
            INSERT INTO action_attempt (
                intent_id, attempt_no, created_at, started_at, status,
                version, execution_generation, external_request_id
            ) VALUES (
                :intent_id, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                'persisted', 0, 0, NULL
            )
        """), {"intent_id": intent_id})

    first = ensure_release_schema(engine)
    second = ensure_release_schema(engine)

    external_request_column = next(
        column
        for column in inspect(engine).get_columns("action_attempt")
        if column["name"] == "external_request_id"
    )
    with engine.connect() as conn:
        external_request_id = conn.execute(text(
            "SELECT external_request_id FROM action_attempt"
        )).scalar_one()
    assert first["backfilledActionAttemptRequestIds"] == 1
    assert second["backfilledActionAttemptRequestIds"] == 0
    assert external_request_id == "request-backfill"
    assert external_request_column["nullable"] is False
    assert ActionAttempt.__table__.c.external_request_id.nullable is False
    assert verify_release_schema(engine)["ok"] is True


def _canonical_json_hash(payload):
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_action_intent_idempotency_hash_is_not_treated_as_audit_fact_hash(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    payload_json, _audit_hash = _canonical_json_hash({
        "type": "product_blacklist",
        "brand": "atlas",
        "query": "dress",
        "product_ids": ["sku-1"],
        "runtime_context": {"request_id": "request-1"},
    })
    _identity_json, idempotency_hash = _canonical_json_hash({
        "type": "product_blacklist",
        "brand": "atlas",
        "query": "dress",
        "product_ids": ["sku-1"],
        "batch_id": "batch-1",
    })
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'product_blacklist',
                'idempotency-hash', :payload_hash, :payload_json, 'operator-1',
                'atlas', 'release-test', 'execute_governance_actions',
                'success', 1
            )
        """), {
            "payload_hash": idempotency_hash,
            "payload_json": payload_json,
        })

    assert prod_migration.inspect_action_fact_hash_violations(engine) == []


@pytest.mark.parametrize("fact_kind", ["fence", "readback", "terminal"])
def test_historical_fact_hash_mismatch_blocks_before_schema_changes(
    engine,
    fact_kind,
):
    _create_action_fact_schema_without_integrity_constraints(engine)
    payload_json, payload_hash = _canonical_json_hash({"value": 1})
    attempt_status = "executing" if fact_kind == "fence" else "persisted"
    generation = 1 if fact_kind == "fence" else 0
    claim_token = "claim-1" if fact_kind == "fence" else None
    lease_expires_at = "2099-01-01 00:00:00" if fact_kind == "fence" else None

    with engine.begin() as conn:
        intent_id = conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'hash-history', :payload_hash, :payload_json, 'operator-1',
                'atlas', 'release-test', 'execute_governance_actions',
                'pending', 0
            )
        """), {
            "payload_hash": payload_hash,
            "payload_json": payload_json,
        }).lastrowid
        attempt_id = conn.execute(text("""
            INSERT INTO action_attempt (
                intent_id, attempt_no, created_at, started_at, status,
                version, execution_generation, claim_token, lease_expires_at,
                external_request_id
            ) VALUES (
                :intent_id, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :status,
                0, :generation, :claim_token, :lease_expires_at, 'hash-history'
            )
        """), {
            "intent_id": intent_id,
            "status": attempt_status,
            "generation": generation,
            "claim_token": claim_token,
            "lease_expires_at": lease_expires_at,
        }).lastrowid
        if fact_kind == "fence":
            conn.execute(text("""
                INSERT INTO action_fence_evidence (
                    intent_id, attempt_id, created_at, external_request_id,
                    execution_generation, fence_claim_token, status,
                    evidence_payload, fact_hash
                ) VALUES (
                    :intent_id, :attempt_id, CURRENT_TIMESTAMP, 'hash-history',
                    1, 'fence-claim', 'fenced', :payload, :fact_hash
                )
            """), {
                "intent_id": intent_id,
                "attempt_id": attempt_id,
                "payload": _canonical_json_hash({"status": "fenced"})[0],
                "fact_hash": "0" * 64,
            })
        elif fact_kind == "readback":
            conn.execute(text("""
                INSERT INTO action_readback (
                    intent_id, attempt_id, created_at, status, authoritative,
                    observed_payload, fact_hash
                ) VALUES (
                    :intent_id, :attempt_id, CURRENT_TIMESTAMP, 'success', 1,
                    :payload, :fact_hash
                )
            """), {
                "intent_id": intent_id,
                "attempt_id": attempt_id,
                "payload": _canonical_json_hash({"status": "success"})[0],
                "fact_hash": "0" * 64,
            })
        elif fact_kind == "terminal":
            result_json = _canonical_json_hash({"status": "success"})[0]
            conn.execute(text("""
                INSERT INTO action_terminal_fact (
                    intent_id, created_at, status, terminal_code, result_json,
                    replay_result_json, fact_hash
                ) VALUES (
                    :intent_id, CURRENT_TIMESTAMP, 'success',
                    'executor_succeeded', :result_json, :result_json, :fact_hash
                )
            """), {
                "intent_id": intent_id,
                "result_json": result_json,
                "fact_hash": "0" * 64,
            })

    before = _schema_snapshot(engine)
    scanner = getattr(prod_migration, "inspect_action_fact_hash_violations", None)

    assert scanner is not None
    expected_table = {
        "fence": "action_fence_evidence",
        "readback": "action_readback",
        "terminal": "action_terminal_fact",
    }[fact_kind]
    assert scanner(engine) == [{"table": expected_table, "count": 1}]
    with pytest.raises(RuntimeError, match="action fact hash violations"):
        ensure_release_schema(engine)
    assert _schema_snapshot(engine) == before


def test_truncated_fact_summary_uses_embedded_full_hash(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    full_payload = {"value": "x" * 5000}
    _full_json, full_hash = _canonical_json_hash(full_payload)
    summary_json = json.dumps(
        {"_truncated": True, "preview": "{\"value\":\"xxx", "sha256": full_hash},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'truncated-hash', :payload_hash, :payload_json, 'operator-1',
                'atlas', 'release-test', 'execute_governance_actions',
                'pending', 0
            )
        """), {"payload_hash": full_hash, "payload_json": summary_json})

    scanner = getattr(prod_migration, "inspect_action_fact_hash_violations", None)

    assert scanner is not None
    assert scanner(engine) == []


@pytest.mark.parametrize("drift", ["terminal_code", "intent", "attempt"])
def test_terminal_chain_state_drift_blocks_before_schema_changes(engine, drift):
    _create_action_fact_schema_without_integrity_constraints(engine)
    result_json, fact_hash = _canonical_json_hash({"status": "success"})
    intent_status = "failed" if drift == "intent" else "success"
    attempt_status = "failed" if drift == "attempt" else "success"
    terminal_code = "executor_failed" if drift == "terminal_code" else "executor_succeeded"
    with engine.begin() as conn:
        intent_id = conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'terminal-drift', :payload_hash, '{}', 'operator-1', 'atlas',
                'release-test', 'execute_governance_actions', :status, 1
            )
        """), {
            "payload_hash": _canonical_json_hash({})[1],
            "status": intent_status,
        }).lastrowid
        conn.execute(text("""
            INSERT INTO action_attempt (
                intent_id, attempt_no, created_at, started_at, finished_at,
                status, version, execution_generation, external_request_id
            ) VALUES (
                :intent_id, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP, :status, 1, 1, 'terminal-drift'
            )
        """), {"intent_id": intent_id, "status": attempt_status})
        conn.execute(text("""
            INSERT INTO action_terminal_fact (
                intent_id, created_at, status, terminal_code, result_json,
                replay_result_json, fact_hash
            ) VALUES (
                :intent_id, CURRENT_TIMESTAMP, 'success', :terminal_code,
                :result_json, :result_json, :fact_hash
            )
        """), {
            "intent_id": intent_id,
            "terminal_code": terminal_code,
            "result_json": result_json,
            "fact_hash": fact_hash,
        })

    before = _schema_snapshot(engine)
    scanner = getattr(prod_migration, "inspect_action_terminal_state_violations", None)

    assert scanner is not None
    assert scanner(engine) == [{"intent_id": intent_id, "reason": drift}]
    with pytest.raises(RuntimeError, match="action terminal state violations"):
        ensure_release_schema(engine)
    assert _schema_snapshot(engine) == before


def test_terminal_chain_valid_state_is_accepted_by_scanner(engine):
    Base.metadata.create_all(bind=engine)
    result_json, fact_hash = _canonical_json_hash({"status": "success"})
    with engine.begin() as conn:
        intent_id = conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'terminal-valid', :payload_hash, '{}', 'operator-1', 'atlas',
                'release-test', 'execute_governance_actions', 'success', 1
            )
        """), {"payload_hash": _canonical_json_hash({})[1]}).lastrowid
        conn.execute(text("""
            INSERT INTO action_attempt (
                intent_id, attempt_no, created_at, started_at, finished_at,
                status, version, execution_generation, external_request_id
            ) VALUES (
                :intent_id, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP, 'success', 1, 1, 'terminal-valid'
            )
        """), {"intent_id": intent_id})
        conn.execute(text("""
            INSERT INTO action_terminal_fact (
                intent_id, created_at, status, terminal_code, result_json,
                replay_result_json, fact_hash
            ) VALUES (
                :intent_id, CURRENT_TIMESTAMP, 'success', 'executor_succeeded',
                :result_json, :result_json, :fact_hash
            )
        """), {
            "intent_id": intent_id,
            "result_json": result_json,
            "fact_hash": fact_hash,
        })

    scanner = getattr(prod_migration, "inspect_action_terminal_state_violations", None)

    assert scanner is not None
    assert scanner(engine) == []


@pytest.mark.parametrize(
    ("version", "comment", "expected_family", "expected_supported"),
    [
        ("8.0.15", "MySQL Community Server - GPL", "mysql", False),
        ("8.0.16", "MySQL Community Server - GPL", "mysql", True),
        ("8.0.15-txsql", "20220831", "txsql", False),
        ("8.0.22-txsql", "20220831", "txsql", True),
        ("8.0.22-TXSQL", "20220831", "txsql", True),
        ("5.5.5-10.1.48-MariaDB", "MariaDB Server", "mariadb", False),
        ("5.5.5-10.2.1-MariaDB", "MariaDB Server", "mariadb", True),
        ("10.6.16-MariaDB", "MariaDB Server", "mariadb", True),
    ],
)
def test_mysql_check_enforcement_version_contract(
    version,
    comment,
    expected_family,
    expected_supported,
):
    contract = getattr(prod_migration, "mysql_check_enforcement_contract", None)

    assert contract is not None
    assert contract(version, comment) == {
        "family": expected_family,
        "version": version,
        "checkConstraintsEnforced": expected_supported,
    }


@pytest.mark.parametrize(
    ("version", "comment"),
    [
        ("", ""),
        ("custom-build", "compatible server"),
        ("8.0", "unknown mysql proxy"),
        ("8.0.22-txsql-proxy", "20220831"),
        ("8.0.22", "txsql"),
    ],
)
def test_mysql_check_enforcement_unknown_version_fails_closed(version, comment):
    contract = getattr(prod_migration, "mysql_check_enforcement_contract", None)

    assert contract is not None
    with pytest.raises(RuntimeError, match="database CHECK enforcement unknown"):
        contract(version, comment)


def test_action_fact_schema_snapshot_includes_data_trigger_fk_and_autoindex(engine):
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TRIGGER trg_action_intent_snapshot
            AFTER INSERT ON action_intent BEGIN SELECT 1; END
        """))
        conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'snapshot-fact', :payload_hash, '{}', 'operator-1', 'atlas',
                'release-test', 'execute_governance_actions', 'pending', 0
            )
        """), {"payload_hash": _canonical_json_hash({})[1]})

    snapshotter = getattr(prod_migration, "snapshot_action_fact_schema", None)

    assert snapshotter is not None
    snapshot = snapshotter(engine)
    assert snapshot["dialect"] == "sqlite"
    assert snapshot["tables"]["action_intent"]["rowCount"] == 1
    assert len(snapshot["tables"]["action_intent"]["dataHash"]) == 64
    assert snapshot["tables"]["action_intent"]["triggers"][0]["name"] == (
        "trg_action_intent_snapshot"
    )
    assert any(
        item["origin"] == "u"
        for item in snapshot["tables"]["action_intent"]["indexes"]
    )
    assert snapshot["tables"]["action_attempt"]["foreignKeys"]


@pytest.mark.parametrize(
    "failure_point",
    ["create", "copy", "drop", "rename", "index", "trigger", "foreign_key_check"],
)
def test_sqlite_action_fact_rebuild_recovers_every_ddl_failure_point(
    engine,
    failure_point,
):
    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE INDEX ix_action_intent_snapshot_custom
            ON action_intent (operator)
        """))
        conn.execute(text("""
            CREATE TRIGGER trg_action_intent_snapshot_custom
            AFTER INSERT ON action_intent BEGIN SELECT 1; END
        """))
        conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'snapshot-recovery', :payload_hash, '{}', 'operator-1', 'atlas',
                'release-test', 'execute_governance_actions', 'pending', 0
            )
        """), {"payload_hash": _canonical_json_hash({})[1]})

    snapshotter = getattr(prod_migration, "snapshot_action_fact_schema", None)
    assert snapshotter is not None
    before = snapshotter(engine)
    failed_once = {"value": False}

    def fail_at_point(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.split())
        matches = {
            "create": "CREATE TABLE __aiops131_action_intent_new" in normalized,
            "copy": "INSERT INTO __aiops131_action_intent_new" in normalized,
            "drop": "DROP TABLE action_intent" in normalized,
            "rename": "ALTER TABLE __aiops131_action_intent_new RENAME TO action_intent" in normalized,
            "index": "CREATE INDEX ix_action_intent_snapshot_custom" in normalized,
            "trigger": "CREATE TRIGGER trg_action_intent_snapshot_custom" in normalized,
            "foreign_key_check": normalized == "PRAGMA foreign_key_check",
        }
        if not failed_once["value"] and matches[failure_point]:
            failed_once["value"] = True
            raise RuntimeError(f"injected {failure_point} failure")

    event.listen(engine, "before_cursor_execute", fail_at_point)
    try:
        with pytest.raises(RuntimeError, match=f"injected {failure_point} failure"):
            ensure_release_schema(engine)
    finally:
        event.remove(engine, "before_cursor_execute", fail_at_point)

    assert failed_once["value"] is True
    assert snapshotter(engine) == before
    assert all(
        not table_name.startswith("__aiops131_")
        for table_name in inspect(engine).get_table_names()
    )
    ensure_release_schema(engine)
    assert verify_release_schema(engine)["ok"] is True


def test_reserved_action_fact_temp_table_requires_manual_recovery(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE __aiops131_action_intent_new (
                marker TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO __aiops131_action_intent_new (marker)
            VALUES ('preserve-for-audit')
        """))
    before_schema = _schema_snapshot(engine)
    with engine.connect() as conn:
        before_rows = conn.execute(text(
            "SELECT marker FROM __aiops131_action_intent_new"
        )).scalars().all()

    with pytest.raises(
        RuntimeError,
        match="manual recovery required for AIOPS-131 temp table",
    ):
        ensure_release_schema(engine)

    assert _schema_snapshot(engine) == before_schema
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT marker FROM __aiops131_action_intent_new"
        )).scalars().all() == before_rows


def test_legacy_trim_only_nonempty_check_is_upgraded(engine):
    _create_action_fact_schema_without_integrity_constraints(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE action_intent"))
        conn.execute(text("""
            CREATE TABLE action_intent (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                action_type VARCHAR(100) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                payload_hash VARCHAR(64) NOT NULL,
                payload_json TEXT NOT NULL,
                operator VARCHAR(120) NOT NULL,
                brand VARCHAR(50) NOT NULL,
                release VARCHAR(120) NOT NULL,
                permission VARCHAR(120) NOT NULL,
                query_text VARCHAR(255),
                target VARCHAR(255),
                status VARCHAR(40) NOT NULL,
                version INTEGER NOT NULL,
                parent_intent_id INTEGER,
                CONSTRAINT ck_action_intent_brand_nonempty
                    CHECK (length(trim(brand)) > 0)
            )
        """))
        conn.execute(text("""
            INSERT INTO action_intent (
                created_at, updated_at, action_type, idempotency_key,
                payload_hash, payload_json, operator, brand, release,
                permission, status, version
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'inventory.switch',
                'legacy-trim', :payload_hash, '{}', 'operator-1', 'atlas',
                'release-test', 'execute_governance_actions', 'pending', 0
            )
        """), {"payload_hash": _canonical_json_hash({})[1]})

    result = ensure_release_schema(engine)

    assert "ck_action_intent_brand_nonempty" in result["ensuredCheckConstraints"]
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE action_intent SET brand = '\t\n\r'"))


@pytest.mark.parametrize(
    "stage",
    ["create_tables", "columns", "checks", "foreign_keys", "indexes"],
)
def test_release_schema_stage_failure_is_rerunnable(engine, stage):
    _create_action_fact_schema_without_integrity_constraints(engine)
    failed_once = {"value": False}

    def fail_once(current_stage):
        if current_stage == stage and not failed_once["value"]:
            failed_once["value"] = True
            raise RuntimeError(f"injected migration stage failure: {stage}")

    with pytest.raises(
        RuntimeError,
        match=f"injected migration stage failure: {stage}",
    ):
        ensure_release_schema(engine, fault_injector=fail_once)

    assert failed_once["value"] is True
    assert all(
        not table_name.startswith("__aiops131_")
        for table_name in inspect(engine).get_table_names()
    )
    ensure_release_schema(engine)
    assert verify_release_schema(engine)["ok"] is True


def test_release_schema_wraps_create_table_failure_with_safe_stage(engine, monkeypatch):
    def _fail_create_all(*_args, **_kwargs):
        raise RuntimeError("simulated database driver failure")

    monkeypatch.setattr(prod_migration.Base.metadata, "create_all", _fail_create_all)

    with pytest.raises(prod_migration.ReleaseSchemaMigrationError) as raised:
        prod_migration.ensure_release_schema(engine)

    assert raised.value.stage == "create_tables"
    assert "simulated database driver failure" in str(raised.value)
