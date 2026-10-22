import json
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects.mysql import TEXT as MYSQL_TEXT
from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.persistence import (
    ActionLog,
    AssistantAgentBinding,
    AssistantBrandSpace,
    AssistantUserBinding,
    TopQueryExperiment,
    TopQueryExperimentSample,
    TopQueryActionPlan,
    TopQueryTask,
    User,
)
from src.migrations.release_schema import (
    RELEASE_IDENTITY_TABLE_MODELS,
    RELEASE_INDEX_SPECS,
    RELEASE_SCHEMA_COLUMNS,
    RELEASE_UNIQUE_SPECS,
    RELEASE_VERIFICATION_INDEX_SPECS,
    RELEASE_VERIFICATION_SCHEMA_COLUMNS,
    CHECKPOINT_COLUMN_TYPE_UPGRADES,
    apply_assistant_bootstrap,
    _build_create_index_sql,
    _is_allowed_release_column_type_upgrade,
    ensure_release_indexes,
    ensure_release_schema_upgrades,
    normalize_query_governance_action_plan_statuses,
    retire_legacy_assistant_binding_columns,
    parse_assistant_bootstrap,
)
from src.query_governance.models import (
    QueryGovernanceActionPlan,
    QueryGovernanceBatchSummary,
    QueryGovernanceCandidate,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
)


class TestProdMigration(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

    def tearDown(self):
        self.engine.dispose()

    def test_query_governance_checkpoint_is_released_as_mysql_mediumtext(self):
        mysql = mysql_dialect()
        for model, column_name in (
            (QueryGovernanceWorkflowRun, "checkpoint_payload"),
            (QueryGovernanceStepStatus, "result_json"),
        ):
            with self.subTest(table=model.__tablename__, column=column_name):
                spec = next(
                    item
                    for item in CHECKPOINT_COLUMN_TYPE_UPGRADES
                    if item["table"] == model.__tablename__
                    and item["column"] == column_name
                )
                expected = spec["definition"]
                actual = {"type": MYSQL_TEXT(), "nullable": True, "default": None}

                self.assertEqual(expected.type.compile(dialect=mysql), "MEDIUMTEXT")
                self.assertTrue(
                    _is_allowed_release_column_type_upgrade(
                        model.__tablename__,
                        column_name,
                        actual,
                        expected,
                        mysql,
                    )
                )

    def test_query_governance_action_plan_executor_columns_are_release_managed(self):
        columns = dict(RELEASE_SCHEMA_COLUMNS[QueryGovernanceActionPlan.__tablename__])

        self.assertEqual(
            set(columns),
            {"executor_type", "executor_user_id", "executed_at", "failure_stage"},
        )

    def test_release_schema_adds_action_log_image_url_for_operations_pages(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE automation_action_log (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT,
                    product_name VARCHAR(255),
                    query_text VARCHAR(255),
                    action_type INTEGER
                )
            """))

        columns = dict(RELEASE_SCHEMA_COLUMNS[ActionLog.__tablename__])
        self.assertEqual(columns["image_url"], "TEXT")

        ensure_release_schema_upgrades(self.engine, RELEASE_SCHEMA_COLUMNS)

        action_log_columns = {
            column["name"]
            for column in inspect(self.engine).get_columns(ActionLog.__tablename__)
        }
        self.assertIn("image_url", action_log_columns)

    def test_release_schema_manages_query_governance_batch_summary(self):
        table_name = QueryGovernanceBatchSummary.__tablename__

        self.assertIs(RELEASE_IDENTITY_TABLE_MODELS[table_name], QueryGovernanceBatchSummary)
        self.assertEqual(
            {name for name, _column in RELEASE_VERIFICATION_SCHEMA_COLUMNS[table_name]},
            {column.name for column in QueryGovernanceBatchSummary.__table__.columns},
        )
        self.assertIn(
            "uq_qg_batch_summary_batch_brand",
            {str(spec["name"]) for spec in RELEASE_UNIQUE_SPECS},
        )
        self.assertIn(
            "ix_qg_batch_summary_brand_date",
            {str(spec["name"]) for spec in RELEASE_VERIFICATION_INDEX_SPECS},
        )

    def test_release_schema_manages_query_governance_candidate_lookup_index(self):
        index_names = {str(spec["name"]) for spec in RELEASE_VERIFICATION_INDEX_SPECS}
        self.assertIn("ix_qg_candidate_batch_brand_query", index_names)
        index = next(
            spec
            for spec in RELEASE_VERIFICATION_INDEX_SPECS
            if spec["name"] == "ix_qg_candidate_batch_brand_query"
        )
        self.assertEqual(index["table"], QueryGovernanceCandidate.__tablename__)
        self.assertEqual(index["columns"], ("batch_id", "brand", "query_text"))

    def test_release_schema_manages_query_governance_task_batch_lookup_index(self):
        index_names = {str(spec["name"]) for spec in RELEASE_VERIFICATION_INDEX_SPECS}
        self.assertIn("ix_qg_task_batch_brand_date", index_names)
        index = next(
            spec
            for spec in RELEASE_VERIFICATION_INDEX_SPECS
            if spec["name"] == "ix_qg_task_batch_brand_date"
        )
        self.assertEqual(index["table"], QueryGovernanceTask.__tablename__)
        self.assertEqual(index["columns"], ("batch_id", "brand", "data_date", "id"))

    def test_release_schema_normalizes_retired_query_governance_plan_statuses(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE query_governance_action_plan (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    action_type VARCHAR(100) NOT NULL,
                    status VARCHAR(30) NOT NULL,
                    response_json TEXT,
                    error_code VARCHAR(80),
                    updated_at DATETIME
                )
            """))
            conn.execute(text("""
                INSERT INTO query_governance_action_plan
                    (action_type, status, response_json, error_code, updated_at)
                VALUES
                    ('query_reorder', 'executed', NULL, NULL, '2026-09-17 10:00:00'),
                    ('query_reorder', 'error', '{"verification":{"rollback":{"status":"success"}}}', 'ACTION_READBACK_FAILED', '2026-09-17 10:01:00'),
                    ('query_reorder', 'error', '{"verification":{"rollback":{"status":"error"}}}', 'ACTION_READBACK_FAILED', '2026-09-17 10:02:00'),
                    ('query_reorder', 'error', NULL, 'ACTION_READBACK_FAILED', '2026-09-17 10:03:00'),
                    ('query_reorder', 'error', NULL, 'ACTION_EXECUTION_FAILED', '2026-09-17 10:04:00')
            """))

        ensure_release_schema_upgrades(self.engine, RELEASE_SCHEMA_COLUMNS)
        normalized = normalize_query_governance_action_plan_statuses(self.engine)

        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT status, executor_type, executed_at, failure_stage
                FROM query_governance_action_plan
                ORDER BY id
            """)).mappings().all()

        self.assertEqual(normalized, {
            "verified": 1,
            "rolledBack": 1,
            "rollbackFailed": 1,
            "executionUnknown": 1,
            "failed": 1,
        })
        self.assertEqual(dict(rows[0]), {
            "status": "verified",
            "executor_type": "ai_auto",
            "executed_at": None,
            "failure_stage": None,
        })
        self.assertEqual(dict(rows[1]), {
            "status": "rolled_back",
            "executor_type": "ai_auto",
            "executed_at": None,
            "failure_stage": "verification",
        })
        self.assertEqual(dict(rows[2]), {
            "status": "rollback_failed",
            "executor_type": "ai_auto",
            "executed_at": None,
            "failure_stage": "rollback",
        })
        self.assertEqual(dict(rows[3]), {
            "status": "execution_unknown",
            "executor_type": "ai_auto",
            "executed_at": None,
            "failure_stage": "verification",
        })
        self.assertEqual(dict(rows[4]), {
            "status": "failed",
            "executor_type": "ai_auto",
            "executed_at": None,
            "failure_stage": "execution",
        })
        self.assertEqual(
            normalize_query_governance_action_plan_statuses(self.engine),
            {
                "verified": 0,
                "rolledBack": 0,
                "rollbackFailed": 0,
                "executionUnknown": 0,
                "failed": 0,
            },
        )

    def _create_legacy_seed_schema(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE users (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    username VARCHAR(50),
                    password_hash VARCHAR(255),
                    page_permissions TEXT,
                    brand_permissions TEXT,
                    is_admin INTEGER,
                    is_active INTEGER,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.execute(text("""
                CREATE TABLE assistant_agent_binding (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    brand_space_id INTEGER,
                    display_name VARCHAR(120),
                    model_display_name VARCHAR(120),
                    binding_type VARCHAR(20),
                    enabled INTEGER,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.execute(text("""
                CREATE TABLE operation_log (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME,
                    brand VARCHAR(50),
                    query_text VARCHAR(255),
                    module VARCHAR(50),
                    action VARCHAR(100),
                    request_payload TEXT,
                    response_payload TEXT,
                    status VARCHAR(20),
                    duration FLOAT,
                    error_message TEXT
                )
            """))

    def test_release_schema_upgrades_create_missing_columns_and_indexes(self):
        self._create_legacy_seed_schema()

        ensure_release_schema_upgrades(self.engine, RELEASE_SCHEMA_COLUMNS)
        retire_legacy_assistant_binding_columns(self.engine)
        ensure_release_indexes(self.engine, RELEASE_INDEX_SPECS)

        inspector = inspect(self.engine)
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        binding_columns = {column["name"] for column in inspector.get_columns("assistant_agent_binding")}
        binding_indexes = {index["name"] for index in inspector.get_indexes("assistant_agent_binding")}
        operation_log_indexes = {index["name"] for index in inspector.get_indexes("operation_log")}

        self.assertIn("role", user_columns)
        self.assertIn("action_permissions", user_columns)
        self.assertIn("model_name", binding_columns)
        self.assertIn("available_models_json", binding_columns)
        self.assertIn("supports_model_switch", binding_columns)
        self.assertIn("ix_assistant_agent_binding_brand_space_id", binding_indexes)
        self.assertIn("ix_operation_log_search_lookup", operation_log_indexes)
        self.assertIn("ix_operation_log_retention_created_id", operation_log_indexes)

        ensure_release_schema_upgrades(self.engine, RELEASE_SCHEMA_COLUMNS)
        ensure_release_indexes(self.engine, RELEASE_INDEX_SPECS)

        inspector = inspect(self.engine)
        binding_columns = [column["name"] for column in inspector.get_columns("assistant_agent_binding")]
        self.assertEqual(binding_columns.count("model_name"), 1)

    def test_release_schema_upgrades_branch_parent_column(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE assistant_session_meta (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id VARCHAR(120),
                    user_id INTEGER
                )
            """))
        ensure_release_schema_upgrades(self.engine, RELEASE_SCHEMA_COLUMNS)
        columns = {column["name"] for column in inspect(self.engine).get_columns("assistant_session_meta")}
        assert "parent_session_id" in columns

    def test_release_schema_retires_legacy_assistant_binding_columns(self):
        self._create_legacy_seed_schema()
        with self.engine.begin() as conn:
            conn.execute(text("ALTER TABLE assistant_agent_binding ADD COLUMN legacy_agent_id VARCHAR(120)"))
            conn.execute(text("ALTER TABLE assistant_agent_binding ADD COLUMN legacy_team_id VARCHAR(120)"))

        ensure_release_schema_upgrades(self.engine, RELEASE_SCHEMA_COLUMNS)
        retire_legacy_assistant_binding_columns(self.engine)

        binding_columns = {
            column["name"] for column in inspect(self.engine).get_columns("assistant_agent_binding")
        }
        assert "legacy_agent_id" not in binding_columns
        assert "legacy_team_id" not in binding_columns

    def test_operation_log_retention_index_requires_online_mysql_ddl(self):
        spec = next(
            item
            for item in RELEASE_INDEX_SPECS
            if item["name"] == "ix_operation_log_retention_created_id"
        )

        self.assertIs(spec["mysql_online"], True)
        statement = _build_create_index_sql(
            SimpleNamespace(dialect=mysql_dialect()),
            spec,
        )
        self.assertIn("ALGORITHM=INPLACE", statement)
        self.assertIn("LOCK=NONE", statement)

    def test_release_schema_declares_zha36_ab_test_v2_columns_without_defaults(
        self,
    ):
        experiment_specs = dict(RELEASE_SCHEMA_COLUMNS[TopQueryExperiment.__tablename__])
        sample_specs = dict(RELEASE_SCHEMA_COLUMNS[TopQueryExperimentSample.__tablename__])

        self.assertEqual(experiment_specs, {
            "baseline_start_date": "DATE",
            "baseline_end_date": "DATE",
            "baseline_locked_at": "DATETIME",
            "calculation_version": "VARCHAR(64)",
        })
        self.assertEqual(sample_specs, {
            "baseline_observed_days": "INTEGER",
            "baseline_search_uv": "INTEGER",
            "baseline_search_pv": "INTEGER",
            "baseline_click_uv": "FLOAT",
            "baseline_purchase_uv": "FLOAT",
            "baseline_ctr": "FLOAT",
            "baseline_cvr": "FLOAT",
        })

    def test_release_verification_preserves_full_action_plan_contract(self):
        table_name = TopQueryActionPlan.__tablename__
        model_columns = [column.name for column in TopQueryActionPlan.__table__.columns]
        release_columns = [
            column_name
            for column_name, _definition in RELEASE_SCHEMA_COLUMNS[table_name]
        ]
        expected_columns = list(dict.fromkeys((*model_columns, *release_columns)))
        verification_columns = [
            column_name
            for column_name, _definition
            in RELEASE_VERIFICATION_SCHEMA_COLUMNS[table_name]
        ]

        self.assertEqual(verification_columns, expected_columns)
        self.assertEqual(len(verification_columns), len(set(verification_columns)))
        self.assertTrue({
            "task_id",
            "status",
            "execution_mode",
            "workflow_run_id",
            "external_request_id",
            "reason",
            "before_snapshot",
            "after_snapshot",
            "expert_key",
            "baseline_hash",
        }.issubset(verification_columns))

    def test_orm_server_defaults_match_release_migration_contract(self):
        expected_defaults = {
            "top_query_task.query_source": "top_query",
            "top_query_action_plan.risk_level": "low",
        }
        columns = {
            "top_query_task.query_source": TopQueryTask.__table__.c.query_source,
            "top_query_action_plan.risk_level": TopQueryActionPlan.__table__.c.risk_level,
        }
        actual_defaults = {
            name: (
                str(column.server_default.arg).strip("'\"")
                if column.server_default is not None
                else None
            )
            for name, column in columns.items()
        }

        self.assertEqual(actual_defaults, expected_defaults)

    def test_bootstrap_creates_brand_spaces_default_bindings_and_admin_grants(self):
        from src.persistence import Base

        Base.metadata.create_all(bind=self.engine)
        session = self.SessionLocal()
        try:
            session.add(User(
                username="admin",
                password_hash="hashed",
                page_permissions="[]",
                brand_permissions="[]",
                is_admin=1,
                is_active=1,
            ))
            session.commit()
        finally:
            session.close()

        bootstrap = parse_assistant_bootstrap(json.dumps({
            "brandSpaces": [
                {
                    "brand": "atlas",
                    "displayName": "ATLAS",
                    "defaultBinding": {
                        "bindingType": "team",
                        "displayName": "ATLAS Agent",
                        "modelDisplayName": "ATLAS Assistant",
                        "modelName": "gpt-4.1",
                        "availableModels": ["gpt-4.1", "gpt-4o"],
                        "supportsModelSwitch": True,
                    },
                }
            ]
        }))

        summary = apply_assistant_bootstrap(self.SessionLocal, bootstrap)

        session = self.SessionLocal()
        try:
            brand_space = session.query(AssistantBrandSpace).filter_by(brand="atlas").one()
            binding = session.query(AssistantAgentBinding).filter_by(brand_space_id=brand_space.id).one()
            user_binding = session.query(AssistantUserBinding).filter_by(
                user_id=session.query(User).filter_by(username="admin").one().id,
                brand_space_id=brand_space.id,
                agent_binding_id=binding.id,
            ).one()
        finally:
            session.close()

        self.assertEqual(summary["brand_spaces_created"], 1)
        self.assertEqual(summary["bindings_created"], 1)
        self.assertEqual(summary["admin_bindings_created"], 1)
        self.assertEqual(brand_space.default_agent_binding_id, binding.id)
        self.assertEqual(binding.model_name, "gpt-4.1")
        self.assertEqual(json.loads(binding.available_models_json), ["gpt-4.1", "gpt-4o"])
        self.assertEqual(user_binding.agent_binding_id, binding.id)

    def test_parse_assistant_bootstrap_rejects_invalid_payload(self):
        with self.assertRaises(ValueError):
            parse_assistant_bootstrap('{"brandSpaces":[{"brand":""}]}')

    def test_parse_assistant_bootstrap_does_not_fabricate_model_from_display_name(self):
        bootstrap = parse_assistant_bootstrap(json.dumps({
            "brandSpaces": [{
                "brand": "atlas",
                "displayName": "ATLAS",
                "defaultBinding": {
                    "modelDisplayName": "ATLAS Assistant",
                },
            }],
        }))

        binding = bootstrap["brandSpaces"][0]["defaultBinding"]
        self.assertEqual(binding["modelDisplayName"], "ATLAS Assistant")
        self.assertEqual(binding["modelName"], "")


if __name__ == "__main__":
    unittest.main()
