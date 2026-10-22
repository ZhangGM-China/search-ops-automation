import unittest
from unittest.mock import patch, MagicMock, ANY, call
import datetime
import json
import src.scheduler
from importlib import import_module

scheduler_module = import_module("src.scheduler.runner")
from src.query_governance.retirement import LegacyAgentLoopRetiredError
from src.scheduler import (
    inventory_monitor_job, sync_inventory_monitor_jobs,
    service_health_check_job, no_result_governance_extract_job, no_result_governance_process_job,
    start_scheduler,
    operation_log_retention_job,
    agent_loop_governance_job,
)


def _registered_scheduler_function(call):
    return getattr(call.args[0], "__scheduler_original_func__", call.args[0])


def _scheduler_job_call(mock_scheduler, job_id):
    return next(
        call
        for call in mock_scheduler.add_job.call_args_list
        if call.kwargs.get("id") == job_id
    )


def _complete_experiment_quality():
    return {
        "status": "complete",
        "expected": 40,
        "observed": 40,
        "expectedSamples": 40,
        "observedSamples": 40,
        "missingKeys": [],
        "treatmentTaskCount": 20,
        "controlTaskCount": 0,
        "controlActionCount": 0,
        "contaminationCount": 0,
        "actionFailed": 0,
        "actionInconclusive": 0,
        "unresolved": 0,
        "retryCount": 0,
        "dataDate": "2026-07-08",
        "calculationVersion": "top-query-ab-test-v2",
    }




class TestScheduler(unittest.TestCase):
    @patch('builtins.print')
    @patch('src.scheduler.runner.time.monotonic', side_effect=[100.0, 100.125])
    @patch('src.scheduler.runner.purge_expired_operation_logs', return_value=42)
    def test_operation_log_retention_emits_success_audit_with_count_and_duration(
        self,
        mock_purge,
        _mock_monotonic,
        mock_print,
    ):
        deleted = operation_log_retention_job()

        self.assertEqual(deleted, 42)
        mock_purge.assert_called_once_with()
        audit = json.loads(mock_print.call_args.args[0])
        self.assertEqual(mock_print.call_args.kwargs, {"flush": True})
        self.assertEqual(audit, {
            "deletedRows": 42,
            "durationMs": 125,
            "event": "operation_log_retention",
            "status": "success",
        })

    @patch('src.scheduler.runner.BlockingScheduler')
    def test_start_scheduler_registers_bounded_operation_log_retention_job(self, mock_scheduler_class):
        mock_scheduler = mock_scheduler_class.return_value

        with patch('src.config.BRAND_CONFIGS', {}):
            start_scheduler()

        call = _scheduler_job_call(mock_scheduler, "operation_log_retention_job")
        self.assertIs(_registered_scheduler_function(call), operation_log_retention_job)
        self.assertEqual(call.args[1], "interval")
        self.assertEqual(call.kwargs["minutes"], 10)
        self.assertEqual(call.kwargs["misfire_grace_time"], 600)
        self.assertTrue(call.kwargs["replace_existing"])
    @patch('src.scheduler.runner.OperationalService')
    @patch('src.scheduler.runner.get_latest_abnormal_query_for_brand')
    def test_probe_brand_readonly_reports_structured_search_failure(
        self,
        mock_latest_query,
        mock_service_class,
    ):
        mock_latest_query.return_value = MagicMock(query_text="连衣裙 女士")
        mock_service_class.return_value.search_products_with_total.return_value = {
            "success": False,
            "products": [],
            "total_count": None,
            "result_complete": False,
            "error_reason": "rate_limited",
        }

        result = scheduler_module._probe_brand_readonly("atlas")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_reason"], "rate_limited")
        self.assertIsNone(result["result_count"])

    @patch('src.scheduler.runner.OperationalService')
    @patch('src.scheduler.runner.get_latest_abnormal_query_for_brand')
    def test_probe_brand_readonly_accepts_successful_empty_result(
        self,
        mock_latest_query,
        mock_service_class,
    ):
        mock_latest_query.return_value = MagicMock(query_text="不存在商品")
        mock_service_class.return_value.search_products_with_total.return_value = {
            "success": True,
            "products": [],
            "total_count": 0,
            "result_complete": True,
            "error_reason": None,
        }

        result = scheduler_module._probe_brand_readonly("atlas")

        self.assertTrue(result["ok"])
        self.assertEqual(result["result_count"], 0)

    @patch('src.scheduler.runner.OperationalService')
    @patch('src.scheduler.runner.get_latest_abnormal_query_for_brand')
    def test_probe_brand_readonly_accepts_summit_products_with_unknown_total(
        self,
        mock_latest_query,
        mock_service_class,
    ):
        mock_latest_query.return_value = MagicMock(query_text="露营天幕")
        mock_service_class.return_value.search_products_with_total.return_value = {
            "success": True,
            "products": [{"productId": "SUMMIT-1"}],
            "total_count": None,
            "result_complete": False,
            "error_reason": None,
        }

        result = scheduler_module._probe_brand_readonly("summit")

        self.assertTrue(result["ok"])
        self.assertIsNone(result["result_count"])
        self.assertEqual(result["observed_result_count"], 1)
        self.assertFalse(result["result_count_known"])
        self.assertIsNone(result["error_reason"])

    @patch('src.scheduler.runner.OperationalService')
    @patch('src.scheduler.runner.get_latest_abnormal_query_for_brand')
    def test_probe_brand_readonly_accepts_summit_empty_products_with_unknown_total(
        self,
        mock_latest_query,
        mock_service_class,
    ):
        mock_latest_query.return_value = MagicMock(query_text="不存在商品")
        mock_service_class.return_value.search_products_with_total.return_value = {
            "success": True,
            "products": [],
            "total_count": None,
            "result_complete": False,
            "error_reason": None,
        }

        result = scheduler_module._probe_brand_readonly("summit")

        self.assertTrue(result["ok"])
        self.assertIsNone(result["result_count"])
        self.assertEqual(result["observed_result_count"], 0)
        self.assertFalse(result["result_count_known"])
        self.assertIsNone(result["error_reason"])

    @patch('src.scheduler.runner.OperationalService')
    @patch('src.scheduler.runner.get_latest_abnormal_query_for_brand')
    def test_probe_brand_readonly_rejects_summit_response_missing_total_count(
        self,
        mock_latest_query,
        mock_service_class,
    ):
        mock_latest_query.return_value = MagicMock(query_text="露营天幕")
        mock_service_class.return_value.search_products_with_total.return_value = {
            "success": True,
            "products": [{"productId": "SUMMIT-1"}],
            "result_complete": False,
            "error_reason": None,
        }

        result = scheduler_module._probe_brand_readonly("summit")

        self.assertFalse(result["ok"])
        self.assertIsNone(result["result_count"])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.scheduler.runner.OperationalService')
    @patch('src.scheduler.runner.get_latest_abnormal_query_for_brand')
    def test_probe_brand_readonly_rejects_summit_success_with_error_reason(
        self,
        mock_latest_query,
        mock_service_class,
    ):
        mock_latest_query.return_value = MagicMock(query_text="露营天幕")
        mock_service_class.return_value.search_products_with_total.return_value = {
            "success": True,
            "products": [{"productId": "SUMMIT-1"}],
            "total_count": None,
            "result_complete": False,
            "error_reason": "unexpected_upstream_error",
        }

        result = scheduler_module._probe_brand_readonly("summit")

        self.assertFalse(result["ok"])
        self.assertIsNone(result["result_count"])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.scheduler.runner.OperationalService')
    @patch('src.scheduler.runner.get_latest_abnormal_query_for_brand')
    def test_probe_brand_readonly_rejects_inconsistent_total(
        self,
        mock_latest_query,
        mock_service_class,
    ):
        mock_latest_query.return_value = MagicMock(query_text="连衣裙 女士")
        mock_service_class.return_value.search_products_with_total.return_value = {
            "success": True,
            "products": [{"productId": "SKU-1"}],
            "total_count": 0,
            "result_complete": True,
            "error_reason": None,
        }

        result = scheduler_module._probe_brand_readonly("atlas")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.scheduler.runner.requests.get')
    def test_probe_api_health_fails_when_runtime_circuit_is_open(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "healthy": False,
                "database": {"connected": True},
                "runtime": {
                    "available": True,
                    "is_circuit_broken": True,
                    "limiter": {"rateLimitedResponses": 0},
                    "circuit": {"openCircuitCount": 1},
                },
            },
        )

        result = scheduler_module._probe_api_health()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "health endpoint runtime circuit is open")

    @patch('src.scheduler.runner.requests.get')
    def test_probe_api_health_fails_when_runtime_state_is_unavailable(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "healthy": False,
                "database": {"connected": True},
                "runtime": {
                    "available": False,
                    "is_circuit_broken": False,
                    "limiter": {},
                    "circuit": {"openCircuitCount": None},
                },
            },
        )

        result = scheduler_module._probe_api_health()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "health endpoint runtime unavailable")

    @patch('src.scheduler.runner.requests.get')
    def test_probe_api_health_ignores_historical_rate_limit_count(self, mock_get):
        runtime = {
            "available": True,
            "is_circuit_broken": False,
            "limiter": {"rateLimitedResponses": 9, "inFlight": 0},
            "circuit": {"openCircuitCount": 0, "lastRecoveryAt": 123.0},
        }
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "healthy": False,
                "database": {"connected": True},
                "runtime": runtime,
            },
        )

        result = scheduler_module._probe_api_health()

        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"]["runtime"], runtime)
        self.assertTrue(result["detail"]["aggregateHealthyIgnored"])
    def setUp(self):
        from src.persistence import Base, reset_engine_for_testing, SessionLocal
        self.engine = reset_engine_for_testing()
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        # Reset global state
        import src.scheduler
        scheduler_module.GLOBAL_LIMIT_REACHED = False

    def tearDown(self):
        self.db.close()
        from src.persistence import Base
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_get_no_result_extractor_type_uses_shared_data_source_for_harbor_and_rl(self):
        self.assertEqual(scheduler_module._get_no_result_extractor_type("summit"), "summit")
        self.assertEqual(scheduler_module._get_no_result_extractor_type("harbor"), "rl")
        self.assertEqual(scheduler_module._get_no_result_extractor_type("riviera"), "rl")

    def test_agent_loop_governance_job_is_retired(self):
        with self.assertRaisesRegex(LegacyAgentLoopRetiredError, "top_query_agent_loop_retired"):
            agent_loop_governance_job(target_date=datetime.date(2026, 7, 8), dry_run=False)


















    @patch('src.scheduler.runner.reload_config')
    @patch('src.scheduler.runner.create_task_session')
    @patch('src.scheduler.runner.update_task_session')
    @patch('src.config.is_brand_enabled')
    @patch('src.scheduler.runner.QueryExtractor')
    def test_no_result_governance_extract_job_creates_supported_brand_batches(
        self,
        mock_extractor_class,
        mock_enabled,
        mock_update,
        mock_create,
        mock_reload,
    ):
        from src.persistence import NoResultQuery

        mock_create.return_value = 99
        mock_enabled.return_value = True
        extractor = mock_extractor_class.return_value
        extractor.get_no_result_queries.side_effect = lambda brand, target_date, limit=100, excluded_keywords=None, **_: {
            "harbor": [{"search_text": "粉色大衣", "search_pv": 12}],
            "riviera": [{"search_text": "绞花毛衣", "search_pv": 8}],
            "summit": [{"keyword": "露营天幕", "no_result_count": 6}],
        }.get(brand, [])

        with patch('src.config.BRAND_CONFIGS', {
            "harbor": {"no_result_governance_enabled": True},
            "riviera": {"no_result_governance_enabled": True},
            "summit": {"no_result_governance_enabled": True},
            "atlas": {},
        }):
            no_result_governance_extract_job(target_date=datetime.date(2026, 6, 22))

        rows = self.db.query(NoResultQuery).order_by(NoResultQuery.brand.asc()).all()
        self.assertEqual({row.brand for row in rows}, {"summit", "harbor", "riviera"})
        self.assertEqual({row.query_text for row in rows}, {"露营天幕", "粉色大衣", "绞花毛衣"})
        self.assertTrue(all(row.status == "pending" for row in rows))
        mock_reload.assert_called_once()
        self.assertTrue(mock_update.called)

    @patch('src.scheduler.runner.reload_config')
    @patch('src.scheduler.runner.create_task_session')
    @patch('src.scheduler.runner.update_task_session')
    @patch('src.config.is_brand_enabled')
    @patch('src.scheduler.runner.QueryExtractor')
    def test_no_result_governance_extract_job_reports_brand_extraction_failure(
        self,
        mock_extractor_class,
        mock_enabled,
        mock_update,
        mock_create,
        mock_reload,
    ):
        mock_create.return_value = 99
        mock_enabled.return_value = True
        extractor = mock_extractor_class.return_value
        extractor.get_no_result_queries.side_effect = RuntimeError("missing source table")

        with patch('src.config.BRAND_CONFIGS', {"harbor": {"no_result_governance_enabled": True}}):
            result = no_result_governance_extract_job(target_date=datetime.date(2026, 6, 22))

        self.assertEqual(result["brands"]["harbor"]["status"], "failed")
        self.assertIn("missing source table", result["brands"]["harbor"]["error"])
        self.assertTrue(mock_update.called)

    @patch('src.scheduler.runner.reload_config')
    @patch('src.scheduler.runner.create_task_session')
    @patch('src.scheduler.runner.update_task_session')
    @patch('src.config.is_brand_enabled')
    @patch('src.scheduler.runner.QueryExtractor')
    def test_no_result_governance_extract_job_requires_explicit_brand_enable(
        self,
        mock_extractor_class,
        mock_enabled,
        mock_update,
        mock_create,
        mock_reload,
    ):
        mock_create.return_value = 99
        mock_enabled.return_value = True

        with patch('src.config.BRAND_CONFIGS', {"harbor": {}}):
            result = no_result_governance_extract_job(target_date=datetime.date(2026, 6, 22))

        self.assertEqual(result["brands"]["harbor"]["status"], "skipped")
        self.assertEqual(result["brands"]["harbor"]["reason"], "no_result_disabled")
        mock_extractor_class.assert_not_called()

    @patch('src.scheduler.runner.reload_config')
    @patch('src.scheduler.runner.create_task_session')
    @patch('src.scheduler.runner.update_task_session')
    @patch('src.scheduler.runner.NoResultGovernanceService')
    def test_no_result_governance_process_job_runs_main_cycle(
        self,
        mock_service_class,
        mock_update,
        mock_create,
        mock_reload,
    ):
        mock_create.return_value = 100
        service = mock_service_class.return_value
        service.run_governance_cycle.return_value = {
            "processed": 2,
            "ignored": 1,
            "manualRequired": 0,
            "executedActions": 1,
            "queuedRetagBatches": 1,
            "retagSuccess": 1,
            "retagFailed": 0,
            "reviewed": 1,
            "failed": 0,
        }

        result = no_result_governance_process_job()

        mock_reload.assert_called_once()
        service.run_governance_cycle.assert_called_once_with(limit=5)
        self.assertEqual(result["processed"], 2)
        mock_update.assert_any_call(100, phase="Processing_No_Result_Governance")
        mock_update.assert_any_call(100, phase="Finished", status="success", summary=service.run_governance_cycle.return_value)

    @patch('src.scheduler.runner.reload_config')
    @patch('src.scheduler.runner.create_task_session')
    @patch('src.scheduler.runner.update_task_session')
    @patch('src.scheduler.runner.NoResultGovernanceService')
    def test_no_result_governance_process_job_uses_configured_cycle_limit(
        self,
        mock_service_class,
        mock_update,
        mock_create,
        mock_reload,
    ):
        mock_create.return_value = 101
        service = mock_service_class.return_value
        service.run_governance_cycle.return_value = {"processed": 0}

        with patch('src.scheduler.runner.GLOBAL_BUSINESS_CONFIG', {"no_result_governance_cycle_limit": 2}):
            no_result_governance_process_job()

        service.run_governance_cycle.assert_called_once_with(limit=2)

    @patch('src.scheduler.runner.BlockingScheduler')
    def test_start_scheduler_registers_no_result_jobs_only_when_independent_switch_is_explicit(self, mock_scheduler_class):
        mock_scheduler = mock_scheduler_class.return_value

        with patch.dict('src.scheduler.runner.GLOBAL_BUSINESS_CONFIG', {"no_result_agent_enabled": True}, clear=False), \
             patch('src.config.BRAND_CONFIGS', {}):
            start_scheduler()

        scheduled_functions = [_registered_scheduler_function(call) for call in mock_scheduler.add_job.call_args_list if call.args]
        self.assertIn(no_result_governance_extract_job, scheduled_functions)
        self.assertIn(no_result_governance_process_job, scheduled_functions)


    @patch('src.scheduler.runner.BlockingScheduler')
    @patch('src.scheduler.runner.acquire_process_lock')
    def test_start_scheduler_skips_duplicate_process_when_singleton_lock_is_held(
        self,
        mock_acquire_process_lock,
        mock_scheduler_class,
    ):
        from src.runtime.process_guard import ProcessLockError

        mock_acquire_process_lock.side_effect = ProcessLockError("already running")

        started = start_scheduler(singleton_name="main_scheduler")

        self.assertFalse(started)
        mock_scheduler_class.assert_not_called()

    @patch('src.scheduler.runner.BlockingScheduler')
    def test_start_scheduler_registers_per_brand_inventory_monitor(self, mock_scheduler_class):
        mock_scheduler = mock_scheduler_class.return_value

        with patch('src.scheduler.runner.get_brand_business_config', return_value={"top_query_inventory_check_interval_minutes": 30, "enable_stock_demotion": True}), \
             patch('src.config.BRAND_CONFIGS', {"atlas": {"enabled": True}}), \
             patch('src.config.is_brand_enabled', return_value=True):
            start_scheduler()

        scheduled_functions = [_registered_scheduler_function(call) for call in mock_scheduler.add_job.call_args_list if call.args]
        self.assertIn(inventory_monitor_job, scheduled_functions)
        call = _scheduler_job_call(mock_scheduler, "inventory_monitor_job:atlas")
        self.assertIs(_registered_scheduler_function(call), inventory_monitor_job)
        self.assertEqual(call.args[1], "interval")
        self.assertEqual(call.kwargs["minutes"], 30)
        self.assertEqual(call.kwargs["kwargs"], {"brand_filter": "atlas"})
        self.assertIsNotNone(call.kwargs["start_date"])


    @patch('src.scheduler.runner.BlockingScheduler')
    def test_inventory_monitor_jobs_have_stable_distinct_start_offsets(self, mock_scheduler_class):
        mock_scheduler = mock_scheduler_class.return_value

        with patch(
            'src.scheduler.runner.get_brand_business_config',
            return_value={"top_query_inventory_check_interval_minutes": 30},
        ), patch(
            'src.config.BRAND_CONFIGS',
            {"atlas": {"enabled": True}, "harbor": {"enabled": True}},
        ), patch('src.config.is_brand_enabled', return_value=True):
            start_scheduler()

        inventory_calls = {
            call.kwargs["kwargs"]["brand_filter"]: call.kwargs["start_date"]
            for call in mock_scheduler.add_job.call_args_list
            if str(call.kwargs.get("id") or "").startswith("inventory_monitor_job:")
        }
        self.assertEqual(set(inventory_calls), {"harbor", "atlas"})
        self.assertGreaterEqual(
            abs((inventory_calls["atlas"] - inventory_calls["harbor"]).total_seconds()),
            59,
        )

    @patch('src.scheduler.runner.reload_config')
    def test_inventory_monitor_config_sync_reloads_and_replaces_brand_intervals(self, mock_reload):
        scheduler = MagicMock()
        stale_job = MagicMock()
        stale_job.id = "inventory_monitor_job:harbor"
        unrelated_job = MagicMock()
        unrelated_job.id = "service_health_check_job"
        scheduler.get_jobs.return_value = [stale_job, unrelated_job]

        with patch('src.scheduler.runner.get_brand_business_config', return_value={"top_query_inventory_check_interval_minutes": 17}), \
             patch('src.config.BRAND_CONFIGS', {"atlas": {"enabled": True}, "harbor": {"enabled": False}}), \
             patch('src.config.is_brand_enabled', side_effect=lambda brand: brand == "atlas"):
            sync_inventory_monitor_jobs(scheduler)

        mock_reload.assert_called_once()
        scheduler.add_job.assert_any_call(
            inventory_monitor_job,
            'interval',
            minutes=17,
            misfire_grace_time=600,
            coalesce=True,
            max_instances=1,
            kwargs={"brand_filter": "atlas"},
            id='inventory_monitor_job:atlas',
            replace_existing=True,
            start_date=ANY,
        )
        scheduler.remove_job.assert_called_once_with("inventory_monitor_job:harbor")

    @patch('src.scheduler.runner.reload_config')
    def test_inventory_monitor_config_sync_replaces_legacy_recovery_job_with_scan_job(self, mock_reload):
        scheduler = MagicMock()
        legacy_job = MagicMock()
        legacy_job.id = "agent_loop_inventory_monitor:atlas"
        scheduler.get_jobs.return_value = [legacy_job]

        with patch('src.scheduler.runner.get_brand_business_config', return_value={"top_query_inventory_check_interval_minutes": 17}), \
             patch('src.config.BRAND_CONFIGS', {"atlas": {"enabled": True}}), \
             patch('src.config.is_brand_enabled', return_value=True):
            sync_inventory_monitor_jobs(scheduler)

        mock_reload.assert_called_once()
        scheduler.add_job.assert_called_once_with(
            inventory_monitor_job,
            'interval',
            minutes=17,
            misfire_grace_time=600,
            coalesce=True,
            max_instances=1,
            kwargs={"brand_filter": "atlas"},
            id='inventory_monitor_job:atlas',
            replace_existing=True,
            start_date=ANY,
        )
        scheduler.remove_job.assert_called_once_with("agent_loop_inventory_monitor:atlas")

    @patch('src.scheduler.runner.reload_config')
    def test_inventory_monitor_config_sync_removes_legacy_and_scan_jobs_when_brand_is_disabled(self, mock_reload):
        scheduler = MagicMock()
        legacy_job = MagicMock()
        legacy_job.id = "agent_loop_inventory_monitor:atlas"
        scan_job = MagicMock()
        scan_job.id = "inventory_monitor_job:atlas"
        scheduler.get_jobs.return_value = [legacy_job, scan_job]

        with patch('src.config.BRAND_CONFIGS', {"atlas": {"enabled": False}}), \
             patch('src.config.is_brand_enabled', return_value=False):
            sync_inventory_monitor_jobs(scheduler)

        mock_reload.assert_called_once()
        scheduler.add_job.assert_not_called()
        scheduler.remove_job.assert_has_calls([
            call("agent_loop_inventory_monitor:atlas"),
            call("inventory_monitor_job:atlas"),
        ])
        self.assertEqual(scheduler.remove_job.call_count, 2)

    @patch('src.scheduler.runner.reload_config')
    def test_inventory_monitor_config_sync_does_not_rebuild_unchanged_intervals(self, mock_reload):
        scheduler = MagicMock()
        existing_job = MagicMock()
        existing_job.id = "inventory_monitor_job:atlas"
        existing_trigger = MagicMock()
        existing_trigger.interval.total_seconds.return_value = 17 * 60
        existing_job.trigger = existing_trigger
        scheduler.get_jobs.return_value = [existing_job]

        with patch('src.scheduler.runner.get_brand_business_config', return_value={"top_query_inventory_check_interval_minutes": 17}), \
             patch('src.config.BRAND_CONFIGS', {"atlas": {"enabled": True}}), \
             patch('src.config.is_brand_enabled', return_value=True):
            sync_inventory_monitor_jobs(scheduler)

        mock_reload.assert_called_once()
        scheduler.add_job.assert_not_called()
        scheduler.remove_job.assert_not_called()

    @patch('src.scheduler.runner.BlockingScheduler')
    def test_start_scheduler_skips_broken_rate_job_when_brand_is_disabled(self, mock_scheduler_class):
        mock_scheduler = mock_scheduler_class.return_value

        with patch('src.config.BRAND_CONFIGS', {"atlas": {"enabled": False}}), \
             patch('src.config.is_brand_enabled', return_value=False):
            start_scheduler()

        job_calls = mock_scheduler.add_job.call_args_list
        size_job_calls = [call for call in job_calls if call.kwargs.get("id") == "inventory_monitor_job:atlas"]
        self.assertEqual(size_job_calls, [])

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.scheduler.runner._probe_performance')
    @patch('src.scheduler.runner._probe_external_readonly')
    @patch('src.scheduler.runner._probe_db_freshness')
    @patch('src.scheduler.runner._run_probe_with_retry')
    @patch('src.scheduler.runner.update_task_session')
    @patch('src.scheduler.runner.create_task_session')
    @patch('src.scheduler.runner.reload_config')
    def test_service_health_check_job_success(
        self,
        mock_reload,
        mock_create,
        mock_update,
        mock_run_retry,
        mock_db_freshness,
        mock_external,
        mock_performance,
        mock_notify,
    ):
        mock_create.return_value = 99
        mock_run_retry.return_value = {"ok": True, "detail": {"healthy": True}}
        mock_db_freshness.return_value = {"ok": True, "detail": {"activity": {}}}
        mock_external.return_value = {"ok": True, "detail": {"brands": {}}}
        mock_performance.return_value = {"ok": True, "detail": {"total_count": 0}}

        service_health_check_job()

        mock_notify.assert_not_called()
        mock_update.assert_any_call(99, phase="Finished", status="success", summary=unittest.mock.ANY)

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.scheduler.runner._probe_performance')
    @patch('src.scheduler.runner._probe_external_readonly')
    @patch('src.scheduler.runner._probe_db_freshness')
    @patch('src.scheduler.runner._run_probe_with_retry')
    @patch('src.scheduler.runner.update_task_session')
    @patch('src.scheduler.runner.create_task_session')
    @patch('src.scheduler.runner.reload_config')
    def test_service_health_check_summary_records_business_http_telemetry(
        self,
        mock_reload,
        mock_create,
        mock_update,
        mock_run_retry,
        mock_db_freshness,
        mock_external,
        mock_performance,
        mock_notify,
    ):
        mock_create.return_value = 101
        mock_run_retry.return_value = {
            "ok": True,
            "detail": {
                "runtime": {
                    "limiter": {
                        "activeLeases": 2,
                        "rateLimitedResponses": 7,
                        "waitSeconds": 3.5,
                        "rateLimitRetryCount": 4,
                        "structuredFailureCount": 3,
                    },
                    "circuit": {
                        "openCircuitCount": 1,
                        "openCircuitIds": ["sha256:abc123"],
                        "lastRecoveryAt": 123.0,
                    },
                },
            },
        }
        mock_db_freshness.return_value = {"ok": True, "detail": {}}
        mock_external.return_value = {"ok": True, "detail": {}}
        mock_performance.return_value = {"ok": True, "detail": {}}

        service_health_check_job()

        finished = next(
            call
            for call in mock_update.call_args_list
            if call.kwargs.get("phase") == "Finished"
        )
        assert finished.kwargs["summary"]["businessHttp"] == {
            "activeLeases": 2,
            "rateLimitedResponses": 7,
            "waitSeconds": 3.5,
            "rateLimitRetryCount": 4,
            "structuredFailureCount": 3,
            "openCircuitCount": 1,
            "openCircuitIds": ["sha256:abc123"],
            "lastRecoveryAt": 123.0,
        }
        mock_notify.assert_not_called()

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.scheduler.runner._probe_performance')
    @patch('src.scheduler.runner._probe_external_readonly')
    @patch('src.scheduler.runner._probe_db_freshness')
    @patch('src.scheduler.runner._run_probe_with_retry')
    @patch('src.scheduler.runner.update_task_session')
    @patch('src.scheduler.runner.create_task_session')
    @patch('src.scheduler.runner.reload_config')
    def test_service_health_check_job_failure_sends_alert(
        self,
        mock_reload,
        mock_create,
        mock_update,
        mock_run_retry,
        mock_db_freshness,
        mock_external,
        mock_performance,
        mock_notify,
    ):
        mock_create.return_value = 100
        mock_run_retry.return_value = {"ok": False, "error": "api down"}
        mock_db_freshness.return_value = {"ok": True, "detail": {}}
        mock_external.return_value = {"ok": True, "detail": {}}
        mock_performance.return_value = {"ok": True, "detail": {}}

        service_health_check_job()

        mock_notify.assert_called_once()
        mock_update.assert_any_call(100, phase="Finished", status="failed", summary=unittest.mock.ANY)

    @patch('src.scheduler.runner.requests.get')
    def test_probe_api_health_ignores_aggregate_health_to_avoid_self_lock(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "healthy": False,
                "database": {"connected": True},
                "tasks": {
                    "serviceHealthCheck": {"status": "failed"},
                },
                "runtime": {
                    "available": True,
                    "is_circuit_broken": False,
                    "limiter": {"rateLimitedResponses": 0},
                    "circuit": {"openCircuitCount": 0},
                },
            },
        )

        result = scheduler_module._probe_api_health()

        self.assertTrue(result["ok"])
        self.assertFalse(result["detail"]["aggregateHealthy"])
        self.assertTrue(result["detail"]["aggregateHealthyIgnored"])

    @patch('src.scheduler.runner.requests.get')
    def test_probe_api_health_fails_when_database_unavailable(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "healthy": False,
                "database": {"connected": False},
            },
        )

        result = scheduler_module._probe_api_health()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "health endpoint database.connected=false")










    @patch.dict('src.scheduler.runner.os.environ', {}, clear=True)
    def test_health_check_api_url_defaults_to_compose_service(self):
        self.assertEqual(
            scheduler_module._get_health_check_api_url(),
            "http://api:8000/api/health",
        )

    @patch.dict('src.scheduler.runner.os.environ', {"HEALTH_CHECK_API_URL": "http://127.0.0.1:9000/api/health"}, clear=True)
    def test_health_check_api_url_allows_override(self):
        self.assertEqual(
            scheduler_module._get_health_check_api_url(),
            "http://127.0.0.1:9000/api/health",
        )

    @patch('src.scheduler.runner.get_recent_operation_metrics')
    @patch.dict('src.scheduler.runner.GLOBAL_SAFETY_CONFIG', {
        "api_delay_threshold": 5,
        "health_check_api_p95_threshold_seconds": 5,
        "health_check_error_rate_threshold_percent": 20,
    }, clear=False)
    @patch.dict('src.scheduler.runner.GLOBAL_BUSINESS_CONFIG', {"health_check_interval_minutes": 60}, clear=False)
    def test_probe_performance_detects_threshold_breach(self, mock_metrics):
        mock_metrics.return_value = {
            "window_minutes": 60,
            "total_count": 10,
            "failed_count": 3,
            "failure_rate_percent": 30.0,
            "slow_count": 2,
            "p95_duration_seconds": 6.0,
        }

        result = scheduler_module._probe_performance()

        self.assertFalse(result["ok"])
        self.assertIn("failure_rate_percent", result["error"])


class SkippedBrandsSummarySuffixTest(unittest.TestCase):
    def test_appends_skipped_brands_to_summary(self):
        result = {"status": "success", "skippedBrands": ["harbor:batch_busy", "atlas:no_candidates"]}
        self.assertEqual(
            scheduler_module._skipped_brands_summary_suffix(result),
            " skipped=harbor:batch_busy,atlas:no_candidates",
        )

    def test_no_suffix_without_anomalous_skips(self):
        # brand_disabled 等长期配置性禁用由 mainline 过滤，不进入 skippedBrands。
        self.assertEqual(scheduler_module._skipped_brands_summary_suffix({"skippedBrands": []}), "")
        self.assertEqual(scheduler_module._skipped_brands_summary_suffix({}), "")
        self.assertEqual(scheduler_module._skipped_brands_summary_suffix("not-a-mapping"), "")
        self.assertEqual(scheduler_module._skipped_brands_summary_suffix(None), "")

    def test_suffix_is_truncated(self):
        result = {"skippedBrands": [f"brand{i}:batch_busy" for i in range(40)]}
        suffix = scheduler_module._skipped_brands_summary_suffix(result)
        self.assertLessEqual(len(suffix), len(" skipped=") + 200)
        self.assertTrue(suffix.startswith(" skipped="))

if __name__ == '__main__':
    unittest.main()
