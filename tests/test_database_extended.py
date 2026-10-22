import gc
import unittest
import datetime
import json
from unittest.mock import MagicMock, patch
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from src.persistence import database as database_module
from src.persistence import (
    Base, reset_engine_for_testing, SessionLocal,
    ActionLog, DailyAbnormalQuery, InventoryRecoveryTask, OperationLog, TaskRunSession, TopQueryWorkflowRun,
    get_active_demoted_products, get_active_demoted_items,
    save_inventory_task, get_active_inventory_tasks, 
    mark_inventory_task_recovered, mark_inventory_task_recovered_by_details,
    get_daily_stats, save_abnormal_queries,
    get_dashboard_action_logs,
    get_dashboard_inventory_tasks, get_dashboard_manual_attention,
    save_operation_log, create_task_session, update_task_session,
    get_daily_action_count, mark_query_processed, get_queries_to_track,
    check_database_connection, get_latest_task_session, get_latest_abnormal_query_for_brand,
    get_recent_table_activity, get_recent_operation_metrics, mark_stale_running_sessions_failed,
    TopQueryTask,
)


def test_reset_engine_for_testing_disposes_existing_engine(monkeypatch):
    class FakeEngine:
        disposed = False

        def dispose(self):
            self.disposed = True

    fake_engine = FakeEngine()
    monkeypatch.setattr("src.persistence.session.engine", fake_engine)

    next_engine = database_module.reset_engine_for_testing()

    assert fake_engine.disposed is True
    next_engine.dispose()


def test_reset_engine_for_testing_closes_checked_out_sessions():
    initial_engine = reset_engine_for_testing()
    db = SessionLocal()
    db.execute(text("SELECT 1"))

    replacement_engine = reset_engine_for_testing()
    del initial_engine
    gc.collect()

    db.close()
    replacement_engine.dispose()


def test_persistence_facade_engine_tracks_the_latest_test_reset():
    import src.persistence as persistence

    latest_engine = persistence.reset_engine_for_testing()

    assert persistence.engine is latest_engine


class TestDatabaseExtended(unittest.TestCase):
    def setUp(self):
        self.engine = reset_engine_for_testing()
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_get_active_demoted_products(self):
        d = ActionLog(brand="atlas", query_text="q1", product_id="S1", action_type=2)
        self.db.add(d)
        r = ActionLog(brand="atlas", query_text="q2", product_id="S2", action_type=3)
        self.db.add(r)
        self.db.commit()
        active = get_active_demoted_products()
        self.assertEqual(len(active), 1)

    def test_get_active_demoted_items(self):
        d = ActionLog(brand="atlas", query_text="q1", product_id="S1", action_type=2, created_at=datetime.datetime.now() - datetime.timedelta(hours=1))
        self.db.add(d)
        self.db.commit()
        items = get_active_demoted_items("atlas")
        self.assertEqual(items["q1"], ["S1"])
        r = ActionLog(brand="atlas", query_text="q1", product_id="S1", action_type=3, created_at=datetime.datetime.now())
        self.db.add(r)
        self.db.commit()
        items = get_active_demoted_items("atlas")
        self.assertEqual(len(items.get("q1", [])), 0)

    def test_inventory_tasks_flow(self):
        task_data = {"brand": "atlas", "spu": "S1", "name": "N1", "query": "Q1", "stock": 5}
        save_inventory_task(task_data)
        tasks = get_active_inventory_tasks()
        self.assertEqual(len(tasks), 1)
        mark_inventory_task_recovered(tasks[0].id, 15)
        self.assertEqual(len(get_active_inventory_tasks()), 0)

    def test_inventory_tasks_are_tracked_per_query(self):
        save_inventory_task({"brand": "atlas", "spu": "S1", "name": "N1", "query": "Q1", "stock": 49})
        save_inventory_task({"brand": "atlas", "spu": "S1", "name": "N1", "query": "Q2", "stock": 49})

        tasks = get_active_inventory_tasks()

        self.assertEqual(len(tasks), 2)
        self.assertEqual({task.original_query for task in tasks}, {"Q1", "Q2"})

    def test_get_daily_stats(self):
        q = DailyAbnormalQuery(brand="atlas", is_processed=1, action_taken="blacklisted", ops_effect="success")
        self.db.add(q)
        self.db.commit()
        stats = get_daily_stats()
        self.assertIn("ATLAS", stats["brands"])

    def test_dashboard_action_logs_filters(self):
        l = ActionLog(brand="atlas", query_text="test", product_id="S1", product_name="N1", action_type=1)
        self.db.add(l)
        self.db.commit()
        self.assertEqual(len(get_dashboard_action_logs(1, {"search": "N1"})["items"]), 1)

    def test_dashboard_inventory_tasks_filters(self):
        t = InventoryRecoveryTask(brand="atlas", product_id="S1", product_name="N1", original_query="Q1", status="monitoring")
        self.db.add(t)
        self.db.commit()
        self.assertEqual(len(get_dashboard_inventory_tasks({"search": "Q1"})["items"]), 1)

    def test_dashboard_manual_attention_logic(self):
        q = DailyAbnormalQuery(id=10, brand="atlas", query_text="failed_q", ops_effect="failed")
        self.db.add(q)
        # Add excessive blacklist for SAME id to test merging
        for i in range(10):
            l = ActionLog(brand="atlas", query_text="failed_q", product_id=f"S{i}", abnormal_query_id=10, action_type=1)
            self.db.add(l)
        self.db.commit()
        res = get_dashboard_manual_attention({})
        self.assertEqual(len(res["items"]), 1)
        self.assertIn("依然存在问题", res["items"][0]["problem"])
        self.assertIn("拉黑商品数量过多", res["items"][0]["problem"])

    def test_operation_logs(self):
        log_data = {"brand": "atlas", "query": "q", "module": "m", "action": "a", "status": "success"}
        save_operation_log(log_data)
        logs = self.db.query(OperationLog).all()
        self.assertEqual(len(logs), 1)

    def test_task_sessions(self):
        sid = create_task_session("test_task")
        self.assertIsNotNone(sid)
        update_task_session(sid, phase="Phase 2", status="success", summary={"ok": 1})
        session = self.db.get(TaskRunSession, sid)
        self.assertEqual(session.current_phase, "Phase 2")
        self.assertEqual(session.status, "success")
        self.assertTrue(check_database_connection())
        self.assertEqual(get_latest_task_session("test_task").id, sid)

    def test_database_connection_failure_logs_only_safe_diagnostics(self):
        db = MagicMock()
        db.execute.side_effect = OperationalError(
            "SELECT 1",
            {},
            Exception(2003, "private database host and credential must not leak"),
        )

        with patch("src.persistence.repositories.SessionLocal", return_value=db):
            with self.assertLogs("src.persistence.repositories", level="WARNING") as captured:
                connected = check_database_connection()

        self.assertFalse(connected)
        log_text = "\n".join(captured.output)
        self.assertIn("error_type=OperationalError", log_text)
        self.assertIn("driver_error_code=2003", log_text)
        self.assertNotIn("private database host", log_text)
        self.assertNotIn("credential must not leak", log_text)
        db.close.assert_called_once_with()

    def test_daily_action_count(self):
        l = ActionLog(brand="atlas", action_type=1)
        self.db.add(l)
        self.db.commit()
        self.assertEqual(get_daily_action_count(), 1)

    def test_mark_query_processed(self):
        q = DailyAbnormalQuery(id=100, brand="atlas", is_processed=0)
        self.db.add(q)
        self.db.commit()
        mark_query_processed(100, "blacklisted", remark="Done")
        q_updated = self.db.get(DailyAbnormalQuery, 100)
        self.assertEqual(q_updated.is_processed, 1)

    def test_get_queries_to_track(self):
        q = DailyAbnormalQuery(brand="atlas", is_processed=1, action_taken="demoted", created_at=datetime.datetime.now())
        self.db.add(q)
        self.db.commit()
        queries = get_queries_to_track()
        self.assertEqual(len(queries), 1)

    def test_save_abnormal_queries_updates(self):
        q1 = {"query": "q1", "brand": "atlas", "type": "low_ctr", "ctr": 0.01}
        created_ids = save_abnormal_queries([q1])

        self.assertEqual(len(created_ids), 1)
        created = self.db.get(DailyAbnormalQuery, created_ids[0])
        self.assertIsNotNone(created)
        self.assertEqual(created.query_text, "q1")
        self.assertEqual(created.anomaly_type, "low_ctr")
        self.assertEqual(created.ctr, 0.01)

        updated_ids = save_abnormal_queries([
            {"query": "q1", "brand": "atlas", "type": "both", "ctr": 0.02}
        ])

        self.assertEqual(updated_ids, created_ids)
        self.db.expire_all()
        updated = self.db.get(DailyAbnormalQuery, created_ids[0])
        self.assertEqual(updated.ctr, 0.02)
        self.assertEqual(updated.anomaly_type, "both")
        self.assertEqual(
            self.db.query(DailyAbnormalQuery)
            .filter(DailyAbnormalQuery.brand == "atlas")
            .count(),
            1,
        )

    def test_recent_metrics_helpers(self):
        self.db.add(DailyAbnormalQuery(brand="atlas", query_text="q_latest"))
        self.db.add(OperationLog(status="success", duration=1.5))
        self.db.add(OperationLog(status="failed", duration=6.5))
        self.db.commit()

        latest_query = get_latest_abnormal_query_for_brand("atlas")
        self.assertEqual(latest_query.query_text, "q_latest")

        activity = get_recent_table_activity(window_minutes=60)
        self.assertGreaterEqual(activity["daily_abnormal_query"], 1)

        metrics = get_recent_operation_metrics(window_minutes=60, slow_threshold=5)
        self.assertEqual(metrics["total_count"], 2)
        self.assertEqual(metrics["failed_count"], 1)
        self.assertEqual(metrics["slow_count"], 1)

    def test_recent_operation_metrics_avoids_loading_large_payload_columns(self):
        self.db.add(OperationLog(
            module="OperationalService",
            status="success",
            duration=1.5,
            request_payload="x" * 1000,
            response_payload="y" * 1000,
            error_message="z" * 1000,
        ))
        self.db.commit()
        statements = []

        def capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(self.engine, "before_cursor_execute", capture_sql)
        try:
            metrics = get_recent_operation_metrics(
                window_minutes=60,
                slow_threshold=5,
                modules=["OperationalService"],
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_sql)

        self.assertEqual(metrics["total_count"], 1)
        selected_sql = "\n".join(statements)
        self.assertNotIn("request_payload", selected_sql)
        self.assertNotIn("response_payload", selected_sql)
        self.assertNotIn("error_message", selected_sql)

    def test_mark_stale_running_sessions_failed(self):
        stale = TaskRunSession(
            task_name="sample_governance_job",
            status="running",
            current_phase="AI_Diagnosis",
            start_time=datetime.datetime.now() - datetime.timedelta(hours=3),
        )
        self.db.add(stale)
        self.db.commit()

        updated = mark_stale_running_sessions_failed("sample_governance_job", stale_minutes=90)

        self.assertEqual(updated, 1)
        stale = self.db.get(TaskRunSession, stale.id)
        self.assertEqual(stale.status, "failed")

    def test_mark_stale_running_sessions_failed_skips_agent_loop_with_recent_top_query_activity(self):
        stale = TaskRunSession(
            task_name="agent_loop_governance_job",
            status="running",
            current_phase="Running_Agent_Loop:atlas",
            start_time=datetime.datetime.now() - datetime.timedelta(hours=3),
        )
        active_task = TopQueryTask(
            brand="atlas",
            data_date=datetime.date.today(),
            query_text="连衣裙",
            query_rank=1,
            status="collecting_context",
            updated_at=datetime.datetime.now(),
        )
        self.db.add(stale)
        self.db.add(active_task)
        self.db.commit()

        updated = mark_stale_running_sessions_failed("agent_loop_governance_job", stale_minutes=90)

        self.assertEqual(updated, 0)
        stale = self.db.get(TaskRunSession, stale.id)
        self.assertEqual(stale.status, "running")

    def test_mark_stale_running_session_keeps_current_leased_workflow_session_running(self):
        now = datetime.datetime.now()
        stale_session = TaskRunSession(
            task_name="agent_loop_governance_job",
            status="running",
            current_phase="Running_Agent_Loop:atlas",
            start_time=now - datetime.timedelta(hours=3),
        )
        task = TopQueryTask(
            brand="atlas",
            data_date=now.date(),
            query_text="连衣裙",
            status="running",
            started_at=now - datetime.timedelta(hours=3),
            updated_at=now - datetime.timedelta(hours=3),
        )
        self.db.add_all([stale_session, task])
        self.db.flush()
        self.db.add(TopQueryWorkflowRun(
            task_id=task.id,
            run_key=f"task:{task.id}:current",
            active_slot="active",
            status="running",
            current_node="planner",
            started_at=now - datetime.timedelta(hours=3),
            heartbeat_at=now,
            lease_owner="current-worker",
            lease_expires_at=now + datetime.timedelta(minutes=5),
        ))
        self.db.commit()

        updated = mark_stale_running_sessions_failed("agent_loop_governance_job", stale_minutes=90)

        self.assertEqual(updated, 0)
        self.assertEqual(self.db.get(TaskRunSession, stale_session.id).status, "running")

    def test_active_query_tables_have_performance_indexes_after_schema_upgrade(self):
        database_module._ensure_schema_upgrades()
        expected = {
            "brand_daily_metric": {
                "ix_brand_daily_metric_brand_data_date_id",
            },
        }

        with self.engine.begin() as conn:
            index_names_by_table = {}
            for table_name in expected:
                rows = conn.execute(text(f"PRAGMA index_list({table_name})")).fetchall()
                index_names_by_table[table_name] = {row[1] for row in rows}

        for table_name, index_names in expected.items():
            self.assertTrue(
                index_names.issubset(index_names_by_table[table_name]),
                f"missing indexes for {table_name}: expected {index_names}, got {index_names_by_table[table_name]}",
            )

    def test_schema_upgrade_adds_assistant_session_parent_column(self):
        from src.persistence import schema as persistence_schema
        from sqlalchemy import create_engine

        legacy_engine = create_engine("sqlite://")
        try:
            with legacy_engine.begin() as conn:
                conn.execute(text("CREATE TABLE assistant_session_meta (id INTEGER PRIMARY KEY, session_id VARCHAR(120))"))
            original_engine = persistence_schema._session.engine
            persistence_schema._session.engine = legacy_engine
            try:
                persistence_schema._ensure_schema_upgrades()
            finally:
                persistence_schema._session.engine = original_engine

            columns = {
                row[1]
                for row in legacy_engine.connect().execute(text("PRAGMA table_info(assistant_session_meta)"))
            }
            self.assertIn("parent_session_id", columns)
        finally:
            legacy_engine.dispose()

if __name__ == '__main__':
    unittest.main()
