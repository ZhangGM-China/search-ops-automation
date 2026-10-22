import unittest
import os
import datetime
import json
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

# 导入必要的组件
from src.persistence import (
    SessionLocal,
    Base,
    ActionLog,
    OperationLog,
    DailyAbnormalQuery,
    InventoryRecoveryTask,
    TaskRunSession,
    User,
    NoResultAgentEvent,
    NoResultGovernanceAction,
    NoResultGovernanceBatch,
    NoResultQuery,
    ActionAttempt,
    ActionIntent as ActionIntentFact,
    ActionReadback,
    ActionTerminalFact,
)
from src.api import app, get_current_user
from importlib import import_module
api_module = import_module("src.api.app")
dashboard_module = import_module("src.api.application.dashboard")

class TestManagementAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 强制重置 engine 为内存数据库 (带 StaticPool)
        cls.engine = create_engine(
            "sqlite://", 
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        SessionLocal.configure(bind=cls.engine)
        
        def override_get_user():
            return User(
                id=1, 
                username="test_admin", 
                is_admin=1, 
                page_permissions='["/", "/task-board", "/ops-dashboard", "/smart-assistant", "/blacklist", "/demotion", "/stock-tracking", "/no-result-governance", "/account-management"]',
                brand_permissions='["atlas", "summit"]'
            )
        app.dependency_overrides[get_current_user] = override_get_user
        
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.engine.dispose()
        app.dependency_overrides.clear()

    def setUp(self):
        # 每次测试前创建表
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()

    def tearDown(self):
        # 每次测试后清空数据并删表
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_abnormal_query_log_endpoint_is_removed(self):
        response = self.client.get("/api/dashboard/abnormal-queries")

        self.assertEqual(response.status_code, 404)

    def test_list_blacklist_logs_pagination(self):
        for i in range(15):
            log = ActionLog(
                brand="atlas",
                product_id=f"sku_{i}",
                query_text="q",
                action_type=1, # 拉黑
                created_at=datetime.datetime.now()
            )
            self.db.add(log)
        self.db.commit()
        
        response = self.client.get("/api/dashboard/blacklist-logs", params={"limit": 10})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 15)
        self.assertEqual(len(data["items"]), 10)

    def test_blacklist_logs_backfill_image_from_latest_search_snapshot(self):
        self.db.add_all([
            ActionLog(
                brand="atlas",
                product_id="C12321810105",
                product_name="猫跟凉鞋",
                query_text="凉鞋 女士",
                action_type=1,
                image_url=None,
                created_at=datetime.datetime(2026, 9, 14, 9, 0, 0),
            ),
            OperationLog(
                brand="atlas",
                query_text="凉鞋 女士",
                module="OperationalService",
                action="POST mixed",
                status="success",
                response_payload=json.dumps({
                    "data": {"rows": [{"productId": "C12321810105", "mainImage": "https://img.test/sandal.jpg"}]}
                }),
                created_at=datetime.datetime(2026, 9, 14, 8, 59, 0),
            ),
        ])
        self.db.commit()

        response = self.client.get(
            "/api/dashboard/blacklist-logs",
            params={"date": "2026-09-14", "brand": "atlas"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["imageUrl"], "https://img.test/sandal.jpg")

    def test_blacklist_logs_do_not_block_on_live_image_backfill(self):
        self.db.add(ActionLog(
            brand="atlas",
            product_id="SKU-NO-IMAGE",
            product_name="无图商品",
            query_text="无图查询",
            action_type=1,
            image_url=None,
            created_at=datetime.datetime(2026, 9, 14, 9, 0, 0),
        ))
        self.db.commit()

        with patch.object(dashboard_module._operational_service, "get_product_details", return_value=[]) as live_detail:
            response = self.client.get(
                "/api/dashboard/blacklist-logs",
                params={"date": "2026-09-14", "brand": "atlas"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["imageUrl"], None)
        live_detail.assert_not_called()

    def test_task_board_endpoints_are_retired(self):
        self.assertEqual(self.client.get("/api/dashboard/task-board").status_code, 404)
        self.assertEqual(self.client.get("/api/dashboard/task-board/tasks/1").status_code, 404)

    def test_product_action_log_endpoints_return_fields_and_cancel_state(self):
        started_at = datetime.datetime(2026, 7, 10, 9, 0, 0)
        self.db.add_all([
            ActionLog(
                brand="atlas",
                product_id="SKU-BLACK",
                product_name="不相关商品",
                query_text="连衣裙",
                action_type=1,
                reason="相关性不符",
                created_at=started_at,
            ),
            ActionLog(
                brand="atlas",
                product_id="SKU-DEMOTE",
                product_name="断码商品",
                query_text="连衣裙",
                action_type=2,
                reason="断码率过高",
                created_at=started_at,
            ),
            ActionLog(
                brand="atlas",
                product_id="SKU-BLACK",
                query_text="连衣裙",
                action_type=5,
                reason="运营手动撤回",
                created_at=started_at + datetime.timedelta(minutes=1),
            ),
            ActionLog(
                brand="atlas",
                product_id="SKU-DEMOTE",
                query_text="连衣裙",
                action_type=3,
                reason="运营手动撤回",
                created_at=started_at + datetime.timedelta(minutes=1),
            ),
        ])
        self.db.commit()

        blacklist_response = self.client.get(
            "/api/dashboard/blacklist-logs",
            params={"date": "2026-07-10", "brand": "atlas"},
        )
        demotion_response = self.client.get(
            "/api/dashboard/demoted-logs",
            params={"date": "2026-07-10", "brand": "atlas"},
        )

        self.assertEqual(blacklist_response.status_code, 200)
        self.assertEqual(demotion_response.status_code, 200)
        blacklist_item = blacklist_response.json()["items"][0]
        demotion_item = demotion_response.json()["items"][0]
        self.assertEqual(
            {key: blacklist_item[key] for key in ["sku", "brand", "query", "reason", "isCanceled"]},
            {
                "sku": "SKU-BLACK",
                "brand": "atlas",
                "query": "连衣裙",
                "reason": "相关性不符",
                "isCanceled": True,
            },
        )
        self.assertTrue(blacklist_item["createdAt"].startswith("2026-07-10"))
        self.assertEqual(
            {key: demotion_item[key] for key in ["sku", "brand", "query", "reason", "isCanceled"]},
            {
                "sku": "SKU-DEMOTE",
                "brand": "atlas",
                "query": "连衣裙",
                "reason": "断码率过高",
                "isCanceled": True,
            },
        )
        self.assertTrue(demotion_item["createdAt"].startswith("2026-07-10"))

    def test_list_no_result_governance_only_returns_search_pv_gte_five(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="completed",
            total_queries=2,
            changed_queries=1,
            retag_status="success",
            started_at=target_date,
            completed_at=target_date,
        )
        self.db.add(batch)
        self.db.flush()
        self.db.add_all([
            NoResultQuery(
                batch_id=batch.id,
                brand="summit",
                query_text="露营天幕",
                data_date=target_date,
                before_result_count=0,
                after_result_count=8,
                search_pv=42,
                status="completed",
            ),
            NoResultQuery(
                batch_id=batch.id,
                brand="summit",
                query_text="冷门配件",
                data_date=target_date,
                before_result_count=0,
                after_result_count=0,
                search_pv=4,
                status="completed",
            ),
        ])
        self.db.commit()

        response = self.client.get("/api/dashboard/no-result-governance", params={"date": "2026-06-22", "brand": "summit"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual([item["query"] for item in data["items"]], ["露营天幕"])
        self.assertTrue(all(item["searchPv"] >= 5 for item in data["items"]))

    def test_list_no_result_governance_and_rollback_batch(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="completed",
            total_queries=1,
            changed_queries=1,
            retag_status="success",
            started_at=target_date,
            completed_at=target_date,
        )
        self.db.add(batch)
        self.db.flush()
        no_result_query = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            data_date=target_date,
            before_result_count=0,
            after_result_count=8,
            search_pv=42,
            status="completed",
            ai_decision="process",
            ai_reason="购物意图明确",
            final_strategy="新增天幕标签并建立露营从属关系",
            processed_at=target_date,
        )
        self.db.add(no_result_query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=no_result_query.id,
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            action_type="add_relationship",
            target_interface="tagRelationship/addRelationship",
            request_payload='{"tagName":"天幕","responseTagName":"露营"}',
            response_payload='{"code":1,"message":"success"}',
            before_snapshot='{}',
            rollback_payload='{"id":"rel-1"}',
            status="success",
            rollback_status="pending",
            risk_level="medium",
            agent_reason="建立从属关系提升露营召回",
            error_message="等待人工复核前的风险说明",
            created_at=target_date,
        )
        self.db.add(action)
        self.db.add(NoResultAgentEvent(
            query_id=no_result_query.id,
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            stage="diagnosis_strategy",
            event_type="final_strategy",
            tool_name=None,
            summary="缺少从属关系",
            payload='{"attributionType":"subordinate_missing","actions":[{"actionType":"add_relationship"}]}',
            created_at=target_date,
        ))
        self.db.commit()

        response = self.client.get("/api/dashboard/no-result-governance", params={"date": "2026-06-22", "brand": "summit"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 1)
        item = data["items"][0]
        self.assertEqual(item["query"], "露营天幕")
        self.assertEqual(item["beforeResultCount"], 0)
        self.assertEqual(item["afterResultCount"], 8)
        self.assertEqual(item["actions"][0]["targetInterface"], "tagRelationship/addRelationship")
        self.assertEqual(item["actions"][0]["errorMessage"], "等待人工复核前的风险说明")
        self.assertEqual(item["agentTimeline"][0]["stage"], "diagnosis_strategy")
        self.assertEqual(item["agentTimeline"][0]["payload"]["attributionType"], "subordinate_missing")

        rollback_response = self.client.post(f"/api/dashboard/no-result-governance/batches/{batch.id}/rollback")

        self.assertEqual(rollback_response.status_code, 200)
        rollback_data = rollback_response.json()
        self.assertEqual(rollback_data["rolledBackActions"], 1)
        self.db.expire_all()
        updated_action = self.db.get(NoResultGovernanceAction, action.id)
        updated_query = self.db.get(NoResultQuery, no_result_query.id)
        updated_batch = self.db.get(NoResultGovernanceBatch, batch.id)
        self.assertEqual(updated_action.rollback_status, "success")
        self.assertEqual(updated_query.status, "rolled_back")
        self.assertEqual(updated_batch.status, "rolled_back")

    def test_rollback_no_result_query_and_brand_date(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="completed",
            total_queries=2,
            changed_queries=2,
            retag_status="success",
            started_at=target_date,
            completed_at=target_date,
        )
        self.db.add(batch)
        self.db.flush()
        query_one = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            data_date=target_date,
            status="completed",
        )
        query_two = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="防水包",
            data_date=target_date,
            status="completed",
        )
        self.db.add_all([query_one, query_two])
        self.db.flush()
        self.db.add_all([
            NoResultGovernanceAction(
                query_id=query_one.id,
                batch_id=batch.id,
                brand="summit",
                query_text="露营天幕",
                action_type="add_relationship",
                target_interface="tagRelationship/addRelationship",
                request_payload='{"actionType":"add_relationship"}',
                response_payload='{"code":1,"message":"success"}',
                before_snapshot='{}',
                rollback_payload='{"actionType":"delete_relationship","params":{"id":"rel-1"}}',
                status="success",
                rollback_status="pending",
                risk_level="medium",
                created_at=target_date,
            ),
            NoResultGovernanceAction(
                query_id=query_two.id,
                batch_id=batch.id,
                brand="summit",
                query_text="防水包",
                action_type="add_relationship",
                target_interface="tagRelationship/addRelationship",
                request_payload='{"actionType":"add_relationship"}',
                response_payload='{"code":1,"message":"success"}',
                before_snapshot='{}',
                rollback_payload='{"actionType":"delete_relationship","params":{"id":"rel-2"}}',
                status="success",
                rollback_status="pending",
                risk_level="medium",
                created_at=target_date + datetime.timedelta(seconds=1),
            ),
        ])
        self.db.commit()

        query_response = self.client.post(f"/api/dashboard/no-result-governance/queries/{query_one.id}/rollback")

        self.assertEqual(query_response.status_code, 200)
        self.assertEqual(query_response.json()["rolledBackActions"], 1)
        self.db.expire_all()
        self.assertEqual(self.db.get(NoResultQuery, query_one.id).status, "rolled_back")
        self.assertEqual(self.db.get(NoResultQuery, query_two.id).status, "completed")

        brand_date_response = self.client.post("/api/dashboard/no-result-governance/brands/summit/dates/2026-06-22/rollback")

        self.assertEqual(brand_date_response.status_code, 200)
        data = brand_date_response.json()
        self.assertEqual(data["brand"], "summit")
        self.assertEqual(data["batchCount"], 1)

    def test_query_rollback_gateway_replays_conflicts_and_binds_external_fact(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="completed",
            total_queries=1,
            changed_queries=1,
        )
        self.db.add(batch)
        self.db.flush()
        query = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            data_date=target_date,
            status="completed",
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            action_type="add_relationship",
            request_payload='{"actionType":"add_relationship"}',
            response_payload='{"code":1,"message":"success"}',
            before_snapshot='{}',
            rollback_payload='{"actionType":"delete_relationship","params":{"id":"rel-1"}}',
            status="success",
            rollback_status="pending",
            risk_level="medium",
            created_at=target_date,
        )
        self.db.add(action)
        self.db.commit()

        payload = {
            "requestId": "no-result-query-rollback-gateway-1",
            "reason": "恢复执行前关系",
        }

        def rollback_once(query_id, *, operator, reason, external_request_id):
            self.assertEqual(query_id, query.id)
            self.assertEqual(operator, "test_admin")
            self.assertEqual(reason, payload["reason"])
            self.assertEqual(external_request_id, payload["requestId"])
            db = SessionLocal()
            try:
                current_action = db.get(NoResultGovernanceAction, action.id)
                current_action.rollback_payload = json.dumps({
                    "actionType": "delete_relationship",
                    "externalRequestId": external_request_id,
                })
                current_action.rollback_status = "success"
                current_query = db.get(NoResultQuery, query.id)
                current_query.status = "rolled_back"
                db.commit()
            finally:
                db.close()
            return {
                "queryId": query.id,
                "batchId": batch.id,
                "rolledBackActions": 1,
                "status": "rolled_back",
            }

        endpoint = f"/api/dashboard/no-result-governance/queries/{query.id}/rollback"
        with patch.object(
            api_module.NoResultGovernanceService,
            "rollback_query",
            side_effect=rollback_once,
        ) as mock_rollback:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)
            conflict = self.client.post(
                endpoint,
                json={**payload, "reason": "不同回滚理由"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(conflict.status_code, 409)
        mock_rollback.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().action_type, "no_result_query_rollback")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "success")
        readback = self.db.query(ActionReadback).one()
        self.assertEqual(readback.authoritative, 1)
        observed = json.loads(readback.observed_payload)
        self.assertEqual(observed["externalRequestId"], payload["requestId"])
        self.assertEqual(observed["queryStatus"], "rolled_back")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")

    def test_query_rollback_gateway_does_not_replay_ambiguous_external_failure(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="completed",
            total_queries=1,
            changed_queries=1,
        )
        self.db.add(batch)
        self.db.flush()
        query = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            data_date=target_date,
            status="completed",
        )
        self.db.add(query)
        self.db.flush()
        self.db.add(NoResultGovernanceAction(
            query_id=query.id,
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            action_type="add_relationship",
            request_payload='{"actionType":"add_relationship"}',
            response_payload='{"code":1,"message":"success"}',
            before_snapshot="{}",
            rollback_payload='{"actionType":"delete_relationship"}',
            status="success",
            rollback_status="pending",
            risk_level="medium",
            created_at=target_date,
        ))
        self.db.commit()
        endpoint = f"/api/dashboard/no-result-governance/queries/{query.id}/rollback"
        payload = {
            "requestId": "no-result-query-rollback-timeout",
            "reason": "外部回滚超时",
        }
        with patch.object(
            api_module.NoResultGovernanceService,
            "rollback_query",
            side_effect=TimeoutError("external rollback timed out"),
        ) as mock_rollback:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_rollback.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_batch_rollback_gateway_replays_conflicts_and_binds_external_fact(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="completed",
            total_queries=1,
            changed_queries=1,
        )
        self.db.add(batch)
        self.db.flush()
        query = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            data_date=target_date,
            status="completed",
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            action_type="add_relationship",
            request_payload='{"actionType":"add_relationship"}',
            response_payload='{"code":1,"message":"success"}',
            before_snapshot="{}",
            rollback_payload='{"actionType":"delete_relationship"}',
            status="success",
            rollback_status="pending",
            risk_level="medium",
            created_at=target_date,
        )
        self.db.add(action)
        self.db.commit()

        payload = {
            "requestId": "no-result-batch-rollback-gateway-1",
            "reason": "恢复批次关系",
        }

        def rollback_once(batch_id, *, operator, reason, external_request_id):
            self.assertEqual(batch_id, batch.id)
            self.assertEqual(operator, "test_admin")
            self.assertEqual(reason, payload["reason"])
            self.assertEqual(external_request_id, payload["requestId"])
            db = SessionLocal()
            try:
                current_action = db.get(NoResultGovernanceAction, action.id)
                current_action.rollback_payload = json.dumps({
                    "actionType": "delete_relationship",
                    "externalRequestId": f"{external_request_id}:action:{action.id}",
                })
                current_action.rollback_status = "success"
                current_query = db.get(NoResultQuery, query.id)
                current_query.status = "rolled_back"
                current_batch = db.get(NoResultGovernanceBatch, batch.id)
                current_batch.status = "rolled_back"
                db.commit()
            finally:
                db.close()
            return {
                "batchId": batch.id,
                "rolledBackActions": 1,
                "status": "rolled_back",
            }

        endpoint = f"/api/dashboard/no-result-governance/batches/{batch.id}/rollback"
        with patch.object(
            api_module.NoResultGovernanceService,
            "rollback_batch",
            side_effect=rollback_once,
        ) as mock_rollback:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)
            conflict = self.client.post(
                endpoint,
                json={**payload, "reason": "不同批次回滚理由"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(conflict.status_code, 409)
        mock_rollback.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().action_type, "no_result_batch_rollback")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "success")
        readback = self.db.query(ActionReadback).one()
        self.assertEqual(readback.authoritative, 1)
        observed = json.loads(readback.observed_payload)
        self.assertEqual(observed["externalRequestId"], payload["requestId"])
        self.assertEqual(observed["batchStatus"], "rolled_back")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")

    def test_batch_rollback_gateway_does_not_replay_ambiguous_external_failure(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="completed",
            total_queries=1,
            changed_queries=1,
        )
        self.db.add(batch)
        self.db.flush()
        query = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            data_date=target_date,
            status="completed",
        )
        self.db.add(query)
        self.db.flush()
        self.db.add(NoResultGovernanceAction(
            query_id=query.id,
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            action_type="add_relationship",
            request_payload='{"actionType":"add_relationship"}',
            response_payload='{"code":1,"message":"success"}',
            before_snapshot="{}",
            rollback_payload='{"actionType":"delete_relationship"}',
            status="success",
            rollback_status="pending",
            risk_level="medium",
            created_at=target_date,
        ))
        self.db.commit()
        endpoint = f"/api/dashboard/no-result-governance/batches/{batch.id}/rollback"
        payload = {
            "requestId": "no-result-batch-rollback-timeout",
            "reason": "外部批次回滚超时",
        }
        with patch.object(
            api_module.NoResultGovernanceService,
            "rollback_batch",
            side_effect=TimeoutError("external rollback timed out"),
        ) as mock_rollback:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_rollback.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_batch_rollback_gateway_rejects_incomplete_batch_readback(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="completed",
            total_queries=1,
            changed_queries=0,
        )
        self.db.add(batch)
        self.db.flush()
        query = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            data_date=target_date,
            status="completed",
        )
        self.db.add(query)
        self.db.commit()

        def incomplete_readback(batch_id, *, operator, reason, external_request_id):
            db = SessionLocal()
            try:
                current_query = db.get(NoResultQuery, query.id)
                current_query.status = "rolled_back"
                db.commit()
            finally:
                db.close()
            return {
                "batchId": batch_id,
                "rolledBackActions": 0,
                "status": "rolled_back",
            }

        with patch.object(
            api_module.NoResultGovernanceService,
            "rollback_batch",
            side_effect=incomplete_readback,
        ):
            response = self.client.post(
                f"/api/dashboard/no-result-governance/batches/{batch.id}/rollback",
                json={
                    "requestId": "no-result-batch-rollback-incomplete",
                    "reason": "验证批次回读",
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionReadback).one().status, "failed")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_brand_date_rollback_gateway_replays_conflicts_and_binds_external_fact(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="completed",
            total_queries=1,
            changed_queries=1,
        )
        self.db.add(batch)
        self.db.flush()
        query = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            data_date=target_date,
            status="completed",
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            action_type="add_relationship",
            request_payload='{"actionType":"add_relationship"}',
            response_payload='{"code":1,"message":"success"}',
            before_snapshot="{}",
            rollback_payload='{"actionType":"delete_relationship"}',
            status="success",
            rollback_status="pending",
            risk_level="medium",
            created_at=target_date,
        )
        self.db.add(action)
        self.db.commit()

        payload = {
            "requestId": "no-result-brand-date-rollback-gateway-1",
            "reason": "恢复品牌日期批次",
        }

        def rollback_once(brand, target_date, *, operator, reason, external_request_id):
            self.assertEqual(brand, "summit")
            self.assertEqual(target_date, datetime.date(2026, 6, 22))
            self.assertEqual(operator, "test_admin")
            self.assertEqual(reason, payload["reason"])
            self.assertEqual(external_request_id, payload["requestId"])
            db = SessionLocal()
            try:
                current_action = db.get(NoResultGovernanceAction, action.id)
                current_action.rollback_payload = json.dumps({
                    "actionType": "delete_relationship",
                    "externalRequestId": f"{external_request_id}:batch:{batch.id}:action:{action.id}",
                })
                current_action.rollback_status = "success"
                current_query = db.get(NoResultQuery, query.id)
                current_query.status = "rolled_back"
                current_batch = db.get(NoResultGovernanceBatch, batch.id)
                current_batch.status = "rolled_back"
                db.commit()
            finally:
                db.close()
            return {
                "brand": brand,
                "date": target_date.isoformat(),
                "batchCount": 1,
                "rolledBackActions": 1,
                "status": "rolled_back",
            }

        endpoint = "/api/dashboard/no-result-governance/brands/summit/dates/2026-06-22/rollback"
        with patch.object(
            api_module.NoResultGovernanceService,
            "rollback_brand_date",
            side_effect=rollback_once,
        ) as mock_rollback:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)
            conflict = self.client.post(
                endpoint,
                json={**payload, "reason": "不同品牌日期回滚理由"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(conflict.status_code, 409)
        mock_rollback.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().action_type, "no_result_brand_date_rollback")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "success")
        readback = self.db.query(ActionReadback).one()
        self.assertEqual(readback.authoritative, 1)
        observed = json.loads(readback.observed_payload)
        self.assertEqual(observed["externalRequestId"], payload["requestId"])
        self.assertEqual(observed["batchCount"], 1)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")

    def test_brand_date_rollback_gateway_does_not_replay_ambiguous_external_failure(self):
        endpoint = "/api/dashboard/no-result-governance/brands/summit/dates/2026-06-22/rollback"
        payload = {
            "requestId": "no-result-brand-date-rollback-timeout",
            "reason": "外部品牌日期回滚超时",
        }
        with patch.object(
            api_module.NoResultGovernanceService,
            "rollback_brand_date",
            side_effect=TimeoutError("external rollback timed out"),
        ) as mock_rollback:
            first = self.client.post(endpoint, json=payload)
            replay = self.client.post(endpoint, json=payload)

        self.assertEqual(first.status_code, 409)
        self.assertEqual(replay.status_code, 409)
        mock_rollback.assert_called_once()
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionAttempt).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_brand_date_rollback_gateway_rejects_incomplete_batch_readback(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="completed",
            total_queries=0,
            changed_queries=0,
        )
        self.db.add(batch)
        self.db.commit()

        with patch.object(
            api_module.NoResultGovernanceService,
            "rollback_brand_date",
            return_value={
                "brand": "summit",
                "date": "2026-06-22",
                "batchCount": 1,
                "rolledBackActions": 0,
                "status": "rolled_back",
            },
        ):
            response = self.client.post(
                "/api/dashboard/no-result-governance/brands/summit/dates/2026-06-22/rollback",
                json={
                    "requestId": "no-result-brand-date-rollback-incomplete",
                    "reason": "验证品牌日期回读",
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.db.query(ActionIntentFact).one().status, "inconclusive")
        self.assertEqual(self.db.query(ActionReadback).one().status, "failed")
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "inconclusive")

    def test_brand_date_rollback_gateway_accepts_verified_empty_scope(self):
        with patch.object(
            api_module.NoResultGovernanceService,
            "rollback_brand_date",
            return_value={
                "brand": "summit",
                "date": "2026-06-22",
                "batchCount": 0,
                "rolledBackActions": 0,
                "status": "rolled_back",
            },
        ) as mock_rollback:
            response = self.client.post(
                "/api/dashboard/no-result-governance/brands/summit/dates/2026-06-22/rollback",
                json={
                    "requestId": "no-result-brand-date-rollback-empty",
                    "reason": "确认空范围",
                },
            )

        self.assertEqual(response.status_code, 200)
        mock_rollback.assert_called_once()
        readback = self.db.query(ActionReadback).one()
        self.assertEqual(readback.status, "success")
        self.assertEqual(json.loads(readback.observed_payload)["batchCount"], 0)
        self.assertEqual(self.db.query(ActionTerminalFact).one().status, "success")

    def test_approve_and_reject_no_result_manual_review_action(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="pending",
            total_queries=2,
            changed_queries=0,
            started_at=target_date,
        )
        self.db.add(batch)
        self.db.flush()
        query_one = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            data_date=target_date,
            status="manual_required",
        )
        query_two = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="防水包",
            data_date=target_date,
            status="manual_required",
        )
        self.db.add_all([query_one, query_two])
        self.db.flush()
        approve_action = NoResultGovernanceAction(
            query_id=query_one.id,
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            action_type="delete_relationship",
            target_interface="tagRelationship/delete",
            request_payload='{"actionType":"delete_relationship","targetInterface":"tagRelationship/delete","target":{"id":"rel-risky"},"riskLevel":"high"}',
            response_payload='{"message":"blocked_by_risk_gate"}',
            before_snapshot='{}',
            rollback_payload='{}',
            status="manual_review",
            rollback_status="not_required",
            risk_level="high",
            agent_reason="删除错误关系",
            error_message="risk level requires manual review",
            created_at=target_date,
        )
        reject_action = NoResultGovernanceAction(
            query_id=query_two.id,
            batch_id=batch.id,
            brand="summit",
            query_text="防水包",
            action_type="delete_relationship",
            target_interface="tagRelationship/delete",
            request_payload='{"actionType":"delete_relationship","targetInterface":"tagRelationship/delete","target":{"id":"rel-keep"},"riskLevel":"high"}',
            response_payload='{"message":"blocked_by_risk_gate"}',
            before_snapshot='{}',
            rollback_payload='{}',
            status="manual_review",
            rollback_status="not_required",
            risk_level="high",
            agent_reason="删除关系",
            error_message="risk level requires manual review",
            created_at=target_date,
        )
        self.db.add_all([approve_action, reject_action])
        self.db.commit()

        approve_response = self.client.post(
            f"/api/dashboard/no-result-governance/actions/{approve_action.id}/approve",
            json={"reason": "人工确认可删除"},
        )
        reject_response = self.client.post(
            f"/api/dashboard/no-result-governance/actions/{reject_action.id}/reject",
            json={"reason": "证据不足"},
        )

        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(reject_response.status_code, 200)
        self.db.expire_all()
        self.assertEqual(self.db.get(NoResultGovernanceAction, approve_action.id).status, "success")
        self.assertEqual(self.db.get(NoResultQuery, query_one.id).status, "awaiting_retag")
        self.assertEqual(self.db.get(NoResultGovernanceAction, reject_action.id).status, "rejected")
        self.assertEqual(self.db.get(NoResultQuery, query_two.id).status, "manual_required")

    def test_execute_pending_no_result_action_requires_backend_write_authorization(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        batch = NoResultGovernanceBatch(
            brand="summit",
            batch_date=target_date,
            status="pending_execute",
            total_queries=1,
            changed_queries=0,
            started_at=target_date,
        )
        self.db.add(batch)
        self.db.flush()
        query = NoResultQuery(
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            data_date=target_date,
            status="pending_execute",
        )
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            batch_id=batch.id,
            brand="summit",
            query_text="露营天幕",
            action_type="add_relationship",
            target_interface="tagRelationship/addRelationship",
            request_payload='{"actionType":"add_relationship","targetInterface":"tagRelationship/addRelationship","params":{"tagName":"天幕"},"riskLevel":"low"}',
            response_payload='{"message":"dry_run_pending_execute"}',
            before_snapshot='{}',
            rollback_payload='{}',
            status="pending_execute",
            rollback_status="not_required",
            risk_level="low",
            agent_reason="dry-run 阶段生成待人工执行动作",
            error_message="dry_run: waiting for manual execution",
            created_at=target_date,
        )
        self.db.add(action)
        self.db.commit()

        with patch.dict(os.environ, {"ENV": "test", "NO_RESULT_KG_REAL_WRITE": ""}, clear=False):
            response = self.client.post(
                f"/api/dashboard/no-result-governance/actions/{action.id}/execute",
                json={"reason": "人工确认执行 dry-run 动作"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending_execute")
        self.db.expire_all()
        updated_action = self.db.get(NoResultGovernanceAction, action.id)
        self.assertEqual(updated_action.status, "pending_execute")
        self.assertIn("real write requires backend authorization", updated_action.error_message)

    def test_assistant_context_uses_real_database_rows(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        atlas_query = DailyAbnormalQuery(
            brand="atlas",
            query_text="衬衫 女士",
            anomaly_type="both",
            action_taken="blacklisted",
            is_processed=1,
            ctr=0.082,
            cvr=0.024,
            reason_analysis="自动治理后仍有不相关商品",
            action_suggestions="补充拉黑并观察",
            data_date=target_date,
            created_at=target_date,
            updated_at=target_date,
        )
        summit_query = DailyAbnormalQuery(
            brand="summit",
            query_text="跑步鞋",
            anomaly_type="low_cvr",
            data_date=target_date,
            created_at=target_date,
        )
        harbor_query = DailyAbnormalQuery(
            brand="harbor",
            query_text="无权限品牌",
            anomaly_type="low_ctr",
            data_date=target_date,
            created_at=target_date,
        )
        self.db.add_all([atlas_query, summit_query, harbor_query])
        self.db.flush()
        self.db.add(ActionLog(
            brand="atlas",
            abnormal_query_id=atlas_query.id,
            product_id="SKU-1",
            product_name="男士宽松亚麻衬衫",
            query_text="衬衫 女士",
            action_type=1,
            reason="性别意图不符",
            created_at=target_date,
        ))
        self.db.add(InventoryRecoveryTask(
            brand="summit",
            product_id="RUN-1",
            product_name="跑步鞋",
            original_query="跑步鞋",
            last_check_stock=42,
            status="monitoring",
            created_at=target_date,
            updated_at=target_date,
        ))
        self.db.commit()

        response = self.client.get("/api/dashboard/assistant-context", params={"date": "2026-06-22"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["taskCount"], 3)
        self.assertEqual(data["summary"]["actionCount"], 1)
        self.assertEqual(data["summary"]["monitoringCount"], 1)
        self.assertIn("衬衫 女士", data["suggestedPrompts"][0])
        self.assertEqual(data["messages"][0]["title"], "衬衫 女士")
        self.assertIn("补充拉黑并观察", data["messages"][0]["content"])
        self.assertNotIn("无权限品牌", str(data))

    def test_assistant_context_includes_inventory_only_tasks(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        self.db.add_all([
            InventoryRecoveryTask(
                brand="atlas",
                product_id="SKU-1",
                product_name="尺码覆盖商品",
                original_query="亚麻衬衫 女士",
                last_check_stock=42,
                status="recovered",
                created_at=target_date,
                updated_at=target_date,
            ),
            InventoryRecoveryTask(
                brand="atlas",
                product_id="SKU-2",
                product_name="尺码覆盖商品2",
                original_query="亚麻衬衫 女士",
                last_check_stock=39,
                status="monitoring",
                created_at=target_date,
                updated_at=target_date,
            ),
        ])
        self.db.commit()

        response = self.client.get("/api/dashboard/assistant-context", params={"date": "2026-06-22"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"]["taskCount"], 1)
        self.assertEqual(data["summary"]["monitoringCount"], 2)
        self.assertIn("亚麻衬衫 女士", data["suggestedPrompts"][0])
        self.assertIn("断码", data["messages"][0]["content"])

    def test_cancel_blacklist(self):
        # 模拟 remove_from_blacklist 被正确调用；写接口成功即成功，无回读
        from src.api import service
        with patch.object(
            service,
            "remove_from_blacklist",
            return_value=True,
        ) as mock_remove:
            payload = {
                "brand": "atlas",
                "query": "牛仔裤",
                "sku": "sku_cancel"
            }
            response = self.client.post("/api/dashboard/actions/cancel-blacklist", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["code"], 0)
            mock_remove.assert_called_once()

    def test_health_endpoint(self):
        health_session = TaskRunSession(task_name="service_health_check_job", current_phase="Finished", status="success")
        self.db.add(health_session)
        self.db.commit()

        with patch("src.api.app.OperationalService.get_runtime_status", return_value={
            "available": True,
            "consecutive_failures": 0,
            "is_circuit_broken": False,
            "limiter": {"inFlight": 0, "rateLimitedResponses": 0},
            "circuit": {"openCircuitCount": 0, "lastRecoveryAt": None},
        }):
            response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["database"]["connected"])
        self.assertTrue(data["healthy"])
        self.assertEqual(data["tasks"]["serviceHealthCheck"]["taskName"], "service_health_check_job")
        self.assertFalse(data["tasks"]["serviceHealthCheck"]["isStale"])

    def test_health_endpoint_fails_safe_for_open_circuit_without_leaking_identity(self):
        runtime = {
            "available": True,
            "consecutive_failures": 0,
            "is_circuit_broken": True,
            "limiter": {"inFlight": 1, "rateLimitedResponses": 2},
            "circuit": {"openCircuitCount": 1, "lastRecoveryAt": None},
        }
        with patch("src.api.app.OperationalService.get_runtime_status", return_value=runtime):
            response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["healthy"])
        self.assertEqual(payload["runtime"], runtime)
        self.assertNotIn("openKeys", payload["runtime"]["circuit"])
        self.assertNotIn("test|search.example.test", json.dumps(payload, ensure_ascii=False))

    def test_health_endpoint_fails_safe_when_runtime_state_is_unavailable(self):
        runtime = {
            "available": False,
            "consecutive_failures": 0,
            "is_circuit_broken": False,
            "limiter": {},
            "circuit": {"openCircuitCount": None, "lastRecoveryAt": None},
        }
        with patch("src.api.app.OperationalService.get_runtime_status", return_value=runtime):
            response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["healthy"])

    def test_health_endpoint_keeps_historical_429_telemetry_healthy(self):
        runtime = {
            "available": True,
            "consecutive_failures": 0,
            "is_circuit_broken": False,
            "limiter": {
                "inFlight": 0,
                "rateLimitedResponses": 7,
                "waitSeconds": 3.25,
            },
            "circuit": {"openCircuitCount": 0, "lastRecoveryAt": 123.0},
        }
        with patch("src.api.app.OperationalService.get_runtime_status", return_value=runtime):
            response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["healthy"])
        self.assertEqual(payload["runtime"], runtime)

    def test_health_endpoint_returns_unhealthy_when_health_job_is_stale(self):
        stale_end_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=91)
        health_session = TaskRunSession(
            task_name="service_health_check_job",
            current_phase="Finished",
            status="success",
            start_time=stale_end_time - datetime.timedelta(minutes=1),
            end_time=stale_end_time,
        )
        self.db.add(health_session)
        self.db.commit()

        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["healthy"])
        self.assertTrue(data["tasks"]["serviceHealthCheck"]["isStale"])
        self.assertEqual(data["tasks"]["serviceHealthCheck"]["staleThresholdMinutes"], 90)

if __name__ == "__main__":
    unittest.main()
