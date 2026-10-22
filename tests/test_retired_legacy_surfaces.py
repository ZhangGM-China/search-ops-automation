import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class RetiredLegacySurfacesTest(unittest.TestCase):
    def test_operational_service_has_no_legacy_global_circuit_helpers(self):
        services_source = (ROOT / "src" / "integrations" / "operational_service.py").read_text()
        tree = ast.parse(services_source)
        operational_service = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "OperationalService"
        )
        method_names = {
            node.name
            for node in operational_service.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertFalse(
            {"_check_circuit", "_record_success", "_record_failure"} & method_names
        )
        for call in (
            "self._check_circuit(",
            "self._record_success(",
            "self._record_failure(",
        ):
            self.assertNotIn(call, services_source)
        for legacy_state in (
            "_consecutive_failures",
            "_is_circuit_broken",
            "_last_circuit_reject_log_at",
        ):
            self.assertNotIn(legacy_state, services_source)

    def test_legacy_workflow_module_is_removed(self):
        self.assertFalse((ROOT / "src" / "workflow.py").exists())

    def test_unreferenced_stub_modules_are_removed(self):
        retired_paths = [
            ROOT / "src" / "stubs.py",
            ROOT / "src" / "business_rules.py",
        ]

        self.assertEqual([], [str(path) for path in retired_paths if path.exists()])

    def test_root_legacy_release_entries_are_removed(self):
        retired_paths = [
            ROOT / "check_remote_db.exp",
            ROOT / "deploy.exp",
            ROOT / "deploy.sh",
            ROOT / "init_remote_db.exp",
            ROOT / "migrate_prod.exp",
            ROOT / "migrate_prod.sh",
            ROOT / "run_remote_cmd.exp",
        ]

        self.assertEqual([], [str(path) for path in retired_paths if path.exists()])

    def test_one_off_real_environment_manual_scripts_are_removed(self):
        retired_paths = [
            ROOT / "tests" / "manual_scripts" / "summit_specific" / "summit_prod_search_test.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "execute_blacklist_task.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "force_refresh_blacklist.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "re_execute_tasks.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "run_overnight_real_test.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "test_demotion_task.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_api_real_v2.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_blacklist_interface.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_blacklist_search_effect.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_components_real.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_demotion_multi.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_demotion_search_effect.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_expert_ranking_demo_readiness.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_new_components.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_single_query.py",
            ROOT / "tests" / "manual_scripts" / "atlas_specific" / "fetch_atlas_abnormal.py",
            ROOT / "tests" / "manual_scripts" / "atlas_specific" / "verify_atlas_demotion_final.py",
            ROOT / "tests" / "manual_scripts" / "atlas_specific" / "verify_atlas_logic_v2.py",
        ]

        self.assertEqual([], [str(path) for path in retired_paths if path.exists()])

    def test_second_stage_one_off_scripts_are_removed(self):
        retired_paths = [
            ROOT / "scripts" / "ops" / "collect_prod_rerun_evidence.exp",
            ROOT / "scripts" / "ops" / "resume_prod_top_query_rerun.exp",
            ROOT / "scripts" / "re_execute_failed_blacklist.py",
            ROOT / "scripts" / "ci" / "final_check.py",
            ROOT / "scripts" / "db" / "check_db_data.py",
            ROOT / "scripts" / "db" / "init_db_local.py",
            ROOT / "scripts" / "db" / "migrate_anomaly_fields.py",
            ROOT / "scripts" / "db" / "migrate_operation_log_search_lookup_index.py",
            ROOT / "scripts" / "db" / "seed_extended.py",
            ROOT / "scripts" / "db" / "seed_today.py",
            ROOT / "scripts" / "db" / "init_prod_tables.py",
            ROOT / "scripts" / "db" / "migrate_test_202607_zha26_storage.py",
            ROOT / "scripts" / "mcp" / "open_meteo_weather_mcp.py",
            ROOT / "scripts" / "ops" / "import_atlas_top_query_demo.py",
            ROOT / "scripts" / "ops" / "run_atlas_ops_team_top3.py",
            ROOT / "scripts" / "ops" / "verify_operation_log_search_lookup.py",
            ROOT / "tests" / "manual_scripts" / "api_probes" / "probe_local_llm.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_no_result_kg_stg_small_scope.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_query_demotion_roundtrip.py",
            ROOT / "tests" / "manual_scripts" / "verification_scripts" / "verify_top_query_world_knowledge_batch_cache.py",
        ]

        self.assertEqual([], [str(path) for path in retired_paths if path.exists()])

    def test_legacy_test_modules_are_removed(self):
        retired_paths = [
            ROOT / "tests" / "test_auto_ops.py",
            ROOT / "tests" / "test_core_logic.py",
            ROOT / "tests" / "test_summit_e2e.py",
            ROOT / "tests" / "test_operation_log_index_migration.py",
            ROOT / "tests" / "test_probe_local_llm.py",
            ROOT / "tests" / "test_query_demotion_roundtrip_script.py",
            ROOT / "tests" / "test_stubs.py",
            ROOT / "tests" / "test_top_query_world_knowledge_batch_cache_verifier.py",
        ]

        self.assertEqual([], [str(path) for path in retired_paths if path.exists()])

    def test_manual_script_inventory_is_limited_to_governed_verifiers(self):
        manual_root = ROOT / "tests" / "manual_scripts"
        expected_paths = {
            "benchmark_relevance.py",
            "benchmark_preference_generation.py",
            "verification_scripts/verify_business_http_governor.py",
            "verification_scripts/verify_no_result_production_readiness.py",
            "verification_scripts/verify_runtime_watchdog.py",
            "verification_scripts/verify_runtime_security.py",
        }
        actual_paths = {
            path.relative_to(manual_root).as_posix()
            for path in manual_root.rglob("*.py")
        }

        self.assertEqual(expected_paths, actual_paths)
        scripts_manual_root = ROOT / "scripts" / "manual_scripts"
        self.assertEqual([], list(scripts_manual_root.rglob("*.py")))

    def test_scheduler_has_no_legacy_daily_ops_exports(self):
        tree = ast.parse((ROOT / "src" / "scheduler" / "runner.py").read_text())
        function_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        class_names = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        }
        self.assertFalse(
            {
                "_process_single_query",
                "batch_verify_actions",
                "apply_batch_actions",
                "daily_ops_job",
                "top_query_governance_job",
                "daily_tracking_job",
                "daily_report_job",
                "_raise_legacy_daily_ops_retired",
            }
            & function_names
        )
        self.assertNotIn("LegacyDailyOpsRetiredError", class_names)

    def test_api_has_no_retired_task_board_or_split_experiment_routes(self):
        api_source = (ROOT / "src" / "api" / "app.py").read_text()
        for route in (
            '/api/dashboard/task-board',
            '/api/dashboard/task-board/tasks/{task_id}',
            '/api/dashboard/top-query/experiments/{experiment_id}/overview',
            '/api/dashboard/top-query/experiments/{experiment_id}/trend',
            '/api/dashboard/top-query/experiments/{experiment_id}/queries',
        ):
            self.assertNotIn(route, api_source)

    def test_experiment_analytics_has_no_split_read_projections(self):
        analytics_source = (
            ROOT / "src" / "top_query" / "experiment_analytics.py"
        ).read_text()
        tree = ast.parse(analytics_source)
        service = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "ExperimentAnalyticsService"
        )
        method_names = {
            node.name
            for node in service.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertFalse(
            {"get_overview", "get_trend", "get_queries", "_paginate_queries"}
            & method_names
        )

    def test_api_has_no_legacy_expert_ranking_routes(self):
        api_source = (ROOT / "src" / "api" / "app.py").read_text()
        for route in (
            '/api/dashboard/top-query/queries/{task_id}/expert-ranking',
            '/api/dashboard/top-query/queries/{task_id}/expert-ranking/generate',
            '/api/dashboard/top-query/queries/{task_id}/expert-ranking/retry',
            '/api/dashboard/top-query/queries/{task_id}/expert-ranking/plans',
        ):
            self.assertNotIn(route, api_source)

    def test_legacy_expert_ranking_modules_and_tests_are_removed(self):
        retired_paths = [
            ROOT / "src" / "top_query" / "expert_ranking.py",
            ROOT / "src" / "top_query" / "expert_ranking_service.py",
            ROOT / "tests" / "test_top_query_expert_ranking.py",
            ROOT / "tests" / "test_top_query_expert_ranking_api.py",
            ROOT / "tests" / "test_top_query_expert_ranking_scheduler.py",
            ROOT / "tests" / "test_expert_ranking_model_config_runtime.py",
            FRONTEND / "pages" / "Config" / "expertConfig.ts",
        ]

        self.assertEqual([], [str(path) for path in retired_paths if path.exists()])

    def test_scheduler_has_no_legacy_expert_ranking_exports(self):
        scheduler_source = (ROOT / "src" / "scheduler" / "runner.py").read_text()
        tree = ast.parse(scheduler_source)
        function_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertFalse(
            {
                "_generate_expert_ranking_if_enabled",
                "_generate_expert_ranking_after_governance",
                "atlas_ops_team_hot_word_job",
            }
            & function_names
        )
        self.assertNotIn("ExpertRankingService", scheduler_source)
        self.assertNotIn('"expertRanking"', scheduler_source)

    def test_legacy_expert_ranking_config_surface_is_removed(self):
        retired_config_tokens = (
            "top_query_expert_enabled_experts",
            "top_query_expert_default_weights",
            "top_query_expert_ranking_top_n",
            "top_query_expert_token_reserve_per_expert",
            "top_query.expert_ranking",
        )
        checked_paths = (
            ROOT / "src" / "config.py",
            ROOT / "src" / "api" / "app.py",
            ROOT / "src" / "llm" / "node_config.py",
            ROOT / "src" / "top_query" / "constants.py",
            ROOT / "src" / "top_query" / "persistence.py",
            ROOT / "config.example.yaml",
            FRONTEND / "features" / "config" / "pages" / "Config" / "index.tsx",
            FRONTEND / "features" / "config" / "pages" / "Config" / "components" / "BrandConfigSection.tsx",
            FRONTEND / "features" / "config" / "pages" / "Config" / "components" / "NodeModelConfigSection.tsx",
            FRONTEND / "shared" / "types" / "index.ts",
        )

        for path in checked_paths:
            source = path.read_text()
            for token in retired_config_tokens:
                self.assertNotIn(token, source, f"{token} remains in {path}")

        constants_source = (ROOT / "src" / "top_query" / "constants.py").read_text()
        self.assertNotIn('"expert_ranking"', constants_source)
        persistence_source = (ROOT / "src" / "top_query" / "persistence.py").read_text()
        self.assertNotIn("expert-ranking:", persistence_source)
        self.assertNotIn("_recover_expired_expert_operation", persistence_source)
        self.assertNotIn("claim_exclusive_exact", persistence_source)

    def test_ops_team_expert_ranking_contract_is_retired(self):
        persistence_models_source = (ROOT / "src" / "persistence" / "models.py").read_text()
        node_config_source = (ROOT / "src" / "llm" / "node_config.py").read_text()
        frontend_types = (FRONTEND / "shared" / "types" / "index.ts").read_text()

        self.assertNotIn("class OpsExpertRanking", persistence_models_source)
        self.assertFalse((ROOT / "src" / "top_query" / "ops_team.py").exists())
        self.assertNotIn('"ops_team.market"', node_config_source)
        self.assertNotIn('"ops_team.leader"', node_config_source)
        self.assertNotIn("export interface OpsExpertRanking", frontend_types)

if __name__ == "__main__":
    unittest.main()
