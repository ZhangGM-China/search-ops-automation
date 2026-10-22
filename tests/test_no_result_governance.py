import datetime
import hashlib
import json
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from src.persistence import (
    Base,
    NoResultAgentEvent,
    NoResultGovernanceAction,
    NoResultIgnoreRecord,
    NoResultQuery,
    NoResultRetagTask,
    SessionLocal,
    TopQueryExperiment,
    TopQueryExperimentSample,
)
from src.integrations.knowledge_graph import DryRunKnowledgeGraphClient, KnowledgeGraphClient, NullKnowledgeGraphClient
from src.integrations.business_http import BusinessCircuitOpen, BusinessRequestGovernor, CircuitRegistry
from src.no_result.governance import NoResultGovernanceService
from src.llm.node_config import NodeConfigError, NodeConfigSnapshot, capture_snapshot
from src.top_query.experiment_service import ExperimentValidationError
from src.no_result.workflow import (
    no_result_diagnose_node,
    no_result_execute_plan_node,
    no_result_filter_intent_node,
    no_result_graph_context_node,
    no_result_normalize_node,
    no_result_queue_retag_node,
    no_result_review_recall_node,
    no_result_segment_node,
)


def test_no_result_workflow_uses_active_runtime_model_snapshot():
    snapshot = capture_snapshot(node_keys=("no_result.intent", "no_result.diagnosis"))
    service = MagicMock()
    service.filter_query_intent.return_value = {"decision": "process"}

    with patch("src.no_result.workflow.capture_runtime_snapshot", return_value=snapshot):
        result = no_result_filter_intent_node({
            "service": service,
            "query_id": 7,
        })

    assert result["model_config_snapshot"] is snapshot
    assert service.filter_query_intent.call_args.kwargs["model_config_snapshot"] is snapshot


class FakeKnowledgeGraphClient:
    def __init__(self):
        self.calls = []
        self.conflicts = []

    def snapshot(self, action):
        return {"target": action.get("target") or action.get("params"), "before": "old-value"}

    def execute(self, action):
        self.calls.append(action)
        return {"code": 1, "message": "success", "id": f"kg-{len(self.calls)}"}

    def build_rollback(self, action, snapshot, response):
        return {
            "actionType": f"rollback_{action['actionType']}",
            "target": action.get("target") or action.get("params"),
            "snapshot": snapshot,
            "responseId": response["id"],
        }

    def detect_conflict(self, action, before_snapshot):
        if self.conflicts:
            return self.conflicts.pop(0)
        return {"hasConflict": False}


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PauseAfterFirstActionClient(FakeKnowledgeGraphClient):
    def __init__(self, service, query_id):
        super().__init__()
        self.service = service
        self.query_id = query_id

    def execute(self, action):
        response = super().execute(action)
        if len(self.calls) == 1:
            self.service.pause_query_execution(self.query_id, operator="ops-user", reason="暂停后续动作")
        return response


class FakeOperationalService:
    def __init__(self, retag_success=True, products=None):
        self.retag_success = retag_success
        self.products = products or []
        self.retag_calls = []
        self.search_calls = []

    def retag_products(self, brand_alias):
        self.retag_calls.append(brand_alias)
        return self.retag_success

    def search_products(self, brand_alias, query, page_size=None):
        self.search_calls.append((brand_alias, query, page_size))
        return self.products


class FakeNoResultIntentAgent:
    def __init__(self, decision="process"):
        self.decision = decision

    def classify(self, query, brand):
        return {"decision": self.decision, "reason": "测试判断", "confidence": 0.9}


class FakeNoResultDiagnosisAgent:
    def diagnose(self, query, candidate_tags, graph_context):
        return {
            "outputType": "strategy",
            "reason": "缺少同义关系",
            "confidence": 0.8,
            "_llm_metrics": {"timeoutSeconds": 100, "requestChars": 321, "responseChars": 123},
            "actions": [
                {
                    "actionType": "add_relationship",
                    "targetInterface": "tagRelationship/addRelationship",
                    "riskLevel": "low",
                    "reason": "补同义",
                    "params": {"tagName": candidate_tags[0] if candidate_tags else query},
                }
            ],
        }


class FakeQueryRequestDiagnosisAgent:
    def diagnose(self, query, candidate_tags, graph_context):
        return {
            "outputType": "query_request",
            "reason": "需要继续查询标签关系",
            "confidence": 0.7,
            "queryIntents": [{"type": "query_relationship", "params": {"tagName": query}}],
        }


class FakeHttpResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if self.responses:
            return FakeHttpResponse(self.responses.pop(0))
        return FakeHttpResponse({"code": 1, "message": "success", "data": "ok"})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if self.responses:
            return FakeHttpResponse(self.responses.pop(0))
        return FakeHttpResponse({"code": 0, "data": "{\"sceneId\":\"scene-from-config\"}", "msg": ""})


class TestNoResultGovernanceService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_session_bind = SessionLocal.kw.get("bind")
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SessionLocal.configure(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        SessionLocal.configure(bind=cls.previous_session_bind)
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_normalizes_brand_specific_rows_to_common_contract(self):
        service = NoResultGovernanceService()
        target_date = datetime.datetime(2026, 6, 22)

        records = service.normalize_rows(
            brand="summit",
            target_date=target_date,
            rows=[
                {"keyword": "露营天幕", "no_result_count": 12},
                {"keyword": "123456", "no_result_count": 99},
                {"keyword": "", "no_result_count": 1},
            ],
            excluded_keywords=[],
        )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["brand"], "summit")
        self.assertEqual(record["query"], "露营天幕")
        self.assertEqual(record["no_result_count"], 12)
        self.assertEqual(record["before_result_count"], 0)
        self.assertEqual(record["data_date"], target_date)

    def test_agent_tool_registry_separates_query_plan_and_execution_tools(self):
        service = NoResultGovernanceService()

        registry = service.get_agent_tool_registry()

        tools = {tool["name"]: tool for tool in registry}
        self.assertEqual(tools["query_tag"]["layer"], "query")
        self.assertTrue(tools["query_tag"]["autoCallableInDryRun"])
        self.assertEqual(tools["add_tag"]["layer"], "write_plan")
        self.assertFalse(tools["add_tag"]["autoCallableInDryRun"])
        self.assertTrue(tools["execute_pending_action"]["writeGateRequired"])

    def test_agent_harness_records_query_reasoning_tool_results_and_plan_timeline(self):
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        target_date = datetime.datetime(2026, 6, 22)
        batch = service.create_batch_from_records(
            "summit",
            target_date,
            [{"query": "粉红色大衣", "data_date": target_date, "search_pv": 10, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()

        service.filter_query_intent(query.id, agent=FakeNoResultIntentAgent("process"), now=target_date)
        tags = service.segment_query(query.id)
        context = service.fetch_graph_context(query.id, candidate_tags=tags)
        plan = service.diagnose_query(query.id, graph_context=context, agent=FakeNoResultDiagnosisAgent(), max_rounds=4)
        service.execute_agent_plan(query.id, {**plan, "executionMode": "dry_run"})

        events = self.db.query(NoResultAgentEvent).filter(
            NoResultAgentEvent.query_id == query.id,
        ).order_by(NoResultAgentEvent.id.asc()).all()
        stages = [event.stage for event in events]
        self.assertIn("intent_filter", stages)
        self.assertIn("segmentation", stages)
        self.assertIn("graph_context_request", stages)
        self.assertIn("graph_context_result", stages)
        self.assertIn("diagnosis_strategy", stages)
        self.assertIn("write_plan", stages)
        diagnosis_event = next(event for event in events if event.stage == "diagnosis_strategy")
        diagnosis_payload = json.loads(diagnosis_event.payload)
        self.assertEqual(diagnosis_payload["llmMetrics"]["timeoutSeconds"], 100)
        write_event = next(event for event in events if event.stage == "write_plan")
        payload = json.loads(write_event.payload)
        self.assertEqual(payload["actions"][0]["actionType"], "add_relationship")
        self.assertEqual(payload["actions"][0]["requiresHumanConfirmation"], True)

    def test_normalize_rows_filters_out_queries_below_global_search_pv_threshold(self):
        service = NoResultGovernanceService()
        target_date = datetime.datetime(2026, 6, 22)

        records = service.normalize_rows(
            brand="summit",
            target_date=target_date,
            rows=[
                {"keyword": "露营天幕", "no_result_count": 12, "search_pv": 8},
                {"keyword": "冷门配件", "no_result_count": 7, "search_pv": 3},
            ],
            excluded_keywords=[],
        )

        self.assertEqual([record["query"] for record in records], ["露营天幕"])
        self.assertTrue(all((record.get("search_pv") or 0) >= 5 for record in records))

    def test_executes_agent_actions_with_snapshot_and_rollback_payload(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())

        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[
                {
                    "brand": "summit",
                    "query": "露营天幕",
                    "data_date": target_date,
                    "search_pv": 42,
                    "before_result_count": 0,
                    "status": "pending",
                }
            ],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()

        result = service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "新增天幕标签并建立露营从属关系",
                "actions": [
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "天幕", "responseTagName": "露营"},
                        "riskLevel": "medium",
                        "reason": "建立从属关系提升露营召回",
                    }
                ],
            },
        )

        self.assertEqual(result["executedActions"], 1)
        self.db.expire_all()
        updated_query = self.db.get(NoResultQuery, query.id)
        action = self.db.query(NoResultGovernanceAction).filter(NoResultGovernanceAction.query_id == query.id).one()
        self.assertEqual(updated_query.status, "awaiting_retag")
        self.assertEqual(updated_query.final_strategy, "新增天幕标签并建立露营从属关系")
        self.assertEqual(action.status, "success")
        self.assertIn("old-value", action.before_snapshot)
        self.assertIn("rollback_add_relationship", action.rollback_payload)

    def test_dry_run_records_write_actions_as_pending_execute_without_calling_write_api(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=fake_client)

        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()

        result = service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "新增天幕标签并建立露营从属关系",
                "executionMode": "dry_run",
                "actions": [
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "天幕", "responseTagName": "露营"},
                        "riskLevel": "medium",
                        "reason": "建立从属关系提升露营召回",
                    }
                ],
            },
        )

        self.assertEqual(result["executedActions"], 0)
        self.assertEqual(result["pendingActions"], 1)
        self.assertEqual(fake_client.calls, [])
        self.db.expire_all()
        updated_query = self.db.get(NoResultQuery, query.id)
        action = self.db.query(NoResultGovernanceAction).filter(NoResultGovernanceAction.query_id == query.id).one()
        self.assertEqual(updated_query.status, "pending_execute")
        self.assertEqual(action.status, "pending_execute")
        self.assertEqual(action.rollback_status, "not_required")
        self.assertIn("dry_run", action.error_message)

    def test_execute_agent_plan_preserves_structured_strategy(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        batch = service.create_batch_from_records(
            brand="harbor",
            target_date=target_date,
            records=[{"brand": "harbor", "query": "STELLA", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        plan = {
            "outputType": "strategy",
            "reason": "新增品牌标签",
            "confidence": 0.85,
            "executionMode": "dry_run",
            "actions": [
                {
                    "actionType": "add_tag",
                    "targetInterface": "/api/tag/add",
                    "riskLevel": "low",
                    "reason": "无结果新词需入库补充",
                    "params": {"tagName": "STELLA"},
                }
            ],
        }

        service.execute_agent_plan(query.id, plan)

        self.db.expire_all()
        updated_query = self.db.get(NoResultQuery, query.id)
        saved_strategy = json.loads(updated_query.final_strategy)
        self.assertEqual(updated_query.ai_decision, "strategy")
        self.assertEqual(saved_strategy["outputType"], "strategy")
        self.assertEqual(saved_strategy["actions"][0]["actionType"], "add_tag")

    def test_query_one_click_execution_runs_pending_actions_in_order(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=fake_client)
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "创建标签并建立关系",
                "executionMode": "dry_run",
                "actions": [
                    {
                        "actionType": "add_tag",
                        "targetInterface": "tag/addTag",
                        "target": {"tagName": "天幕"},
                        "riskLevel": "low",
                        "reason": "创建标签",
                    },
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "天幕", "responseTagName": "露营"},
                        "riskLevel": "low",
                        "reason": "建立关系",
                    },
                ],
            },
        )

        result = service.start_query_execution(query.id, operator="ops-user", reason="一键执行", now=target_date)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["executedActions"], 2)
        self.assertEqual([call["actionType"] for call in fake_client.calls], ["add_tag", "add_relationship"])
        self.db.expire_all()
        actions = self.db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.query_id == query.id
        ).order_by(NoResultGovernanceAction.id.asc()).all()
        self.assertEqual([action.status for action in actions], ["success", "success"])
        self.assertEqual(self.db.get(NoResultQuery, query.id).status, "awaiting_retag")

    def test_query_one_click_execution_pauses_after_current_action_and_resumes_next(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        service = NoResultGovernanceService()
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.kg_client = PauseAfterFirstActionClient(service, query.id)
        service._kg_client_injected = True
        service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "创建标签并建立关系",
                "executionMode": "dry_run",
                "actions": [
                    {
                        "actionType": "add_tag",
                        "targetInterface": "tag/addTag",
                        "target": {"tagName": "天幕"},
                        "riskLevel": "low",
                        "reason": "创建标签",
                    },
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "天幕", "responseTagName": "露营"},
                        "riskLevel": "low",
                        "reason": "建立关系",
                    },
                ],
            },
        )

        paused = service.start_query_execution(query.id, operator="ops-user", reason="一键执行", now=target_date)

        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["executedActions"], 1)
        self.assertEqual(len(service.kg_client.calls), 1)
        self.db.expire_all()
        actions = self.db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.query_id == query.id
        ).order_by(NoResultGovernanceAction.id.asc()).all()
        self.assertEqual([action.status for action in actions], ["success", "pending_execute"])
        self.assertEqual(self.db.get(NoResultQuery, query.id).status, "execution_paused")

        resumed = service.resume_query_execution(query.id, operator="ops-user", reason="继续任务", now=target_date)

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["executedActions"], 1)
        self.assertEqual(len(service.kg_client.calls), 2)
        self.db.expire_all()
        updated_actions = self.db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.query_id == query.id
        ).order_by(NoResultGovernanceAction.id.asc()).all()
        self.assertEqual([action.status for action in updated_actions], ["success", "success"])

    def test_query_one_click_execution_does_not_repeat_successful_actions(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=fake_client)
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "创建标签并建立关系",
                "executionMode": "dry_run",
                "actions": [
                    {
                        "actionType": "add_tag",
                        "targetInterface": "tag/addTag",
                        "target": {"tagName": "天幕"},
                        "riskLevel": "low",
                        "reason": "创建标签",
                    }
                ],
            },
        )

        first = service.start_query_execution(query.id, operator="ops-user", reason="一键执行", now=target_date)
        second = service.start_query_execution(query.id, operator="ops-user", reason="重复点击", now=target_date)

        self.assertEqual(first["executedActions"], 1)
        self.assertEqual(second["executedActions"], 0)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(fake_client.calls), 1)

    def test_query_one_click_execution_keeps_pending_when_real_write_is_not_authorized(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=fake_client)
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "创建标签",
                "executionMode": "dry_run",
                "actions": [
                    {
                        "actionType": "add_tag",
                        "targetInterface": "tag/addTag",
                        "target": {"tagName": "天幕"},
                        "riskLevel": "low",
                        "reason": "创建标签",
                    }
                ],
            },
        )

        with patch.object(service, "_real_write_authorized", return_value=False):
            blocked = service.start_query_execution(query.id, operator="ops-user", reason="一键执行", now=target_date)

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["executedActions"], 0)
        self.assertEqual(len(fake_client.calls), 0)
        self.db.expire_all()
        action = self.db.query(NoResultGovernanceAction).filter(NoResultGovernanceAction.query_id == query.id).one()
        self.assertEqual(action.status, "pending_execute")
        self.assertIn("real write requires backend authorization", action.error_message)

    def test_executes_pending_dry_run_action_through_gateway_and_queues_retag(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=fake_client)

        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "新增关系",
                "executionMode": "dry_run",
                "actions": [
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "天幕", "responseTagName": "露营"},
                        "riskLevel": "low",
                        "reason": "新增关系",
                    }
                ],
            },
        )
        action = self.db.query(NoResultGovernanceAction).filter(NoResultGovernanceAction.query_id == query.id).one()

        executed = service.execute_pending_action(action.id, operator="ops-user", reason="确认执行", now=target_date)

        self.assertEqual(executed["status"], "success")
        self.assertEqual(len(fake_client.calls), 1)
        self.db.expire_all()
        updated_action = self.db.get(NoResultGovernanceAction, action.id)
        updated_query = self.db.get(NoResultQuery, query.id)
        retag_task = self.db.query(NoResultRetagTask).one()
        self.assertEqual(updated_action.status, "success")
        self.assertEqual(updated_action.rollback_status, "pending")
        self.assertIn("确认执行", updated_action.error_message)
        self.assertEqual(updated_query.status, "awaiting_retag")
        self.assertIn(str(batch.id), retag_task.batch_ids)

    def test_no_result_kg_write_rejects_running_control_but_allows_ended_history(self):
        experiment = TopQueryExperiment(
            brand="atlas",
            status="running",
            planned_start_date=datetime.date(2026, 7, 1),
        )
        self.db.add(experiment)
        self.db.flush()
        self.db.add(TopQueryExperimentSample(
            experiment_id=experiment.id,
            brand="atlas",
            query_text="连衣裙",
            group_type="control",
        ))
        query = NoResultQuery(brand="atlas", query_text="连衣裙 女士", status="pending_execute")
        self.db.add(query)
        self.db.flush()
        action = NoResultGovernanceAction(
            query_id=query.id,
            brand="atlas",
            query_text=query.query_text,
            action_type="add_relationship",
            status="pending_execute",
            risk_level="low",
            request_payload=json.dumps({"actionType": "add_relationship", "params": {"tagName": "连衣裙"}}),
        )
        self.db.add(action)
        rollback_action = NoResultGovernanceAction(
            query_id=query.id,
            brand="atlas",
            query_text=query.query_text,
            action_type="add_tag",
            status="success",
            rollback_status="pending",
            rollback_payload=json.dumps({"actionType": "rollback_add_tag", "params": {"tagName": "连衣裙"}}),
            request_payload=json.dumps({"actionType": "add_tag"}),
        )
        self.db.add(rollback_action)
        self.db.commit()

        client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=client)
        with self.assertRaisesRegex(ExperimentValidationError, "对照组禁止治理"):
            service.execute_pending_action(action.id, operator="ops")
        with self.assertRaisesRegex(ExperimentValidationError, "对照组禁止治理"):
            service.rollback_query(query.id)
        self.assertEqual(client.calls, [])
        self.db.expire_all()
        self.assertEqual(self.db.get(NoResultQuery, query.id).status, "pending_execute")
        self.assertEqual(self.db.get(NoResultGovernanceAction, rollback_action.id).rollback_status, "pending")

        experiment.status = "ended"
        self.db.commit()
        result = service.execute_pending_action(action.id, operator="ops")
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(client.calls), 1)

    def test_skips_duplicate_agent_actions_by_fingerprint(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=fake_client)
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[
                {
                    "brand": "summit",
                    "query": "露营天幕",
                    "data_date": target_date,
                    "status": "pending",
                }
            ],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        duplicate_action = {
            "actionType": "add_relationship",
            "targetInterface": "tagRelationship/addRelationship",
            "target": {"tagName": "天幕", "responseTagName": "露营"},
            "riskLevel": "low",
            "reason": "补关系",
        }

        result = service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "补充图谱关系",
                "actions": [duplicate_action, dict(duplicate_action)],
            },
        )

        self.assertEqual(result["executedActions"], 1)
        self.assertEqual(len(fake_client.calls), 1)
        self.assertEqual(
            self.db.query(NoResultGovernanceAction).filter(NoResultGovernanceAction.query_id == query.id).count(),
            1,
        )

    def test_records_high_risk_actions_for_manual_review_when_risk_gate_blocks_execution(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=fake_client)
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()

        result = service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "关系错误但需人工复核",
                "finalStrategy": "删除错误关系",
                "maxRiskLevel": "medium",
                "actions": [
                    {
                        "actionType": "delete_relationship",
                        "targetInterface": "tagRelationship/delete",
                        "params": {"id": "rel-risky"},
                        "riskLevel": "high",
                        "reason": "删除错误关系",
                    }
                ],
            },
        )

        self.assertEqual(result["executedActions"], 0)
        self.assertEqual(len(fake_client.calls), 0)
        self.db.expire_all()
        action = self.db.query(NoResultGovernanceAction).filter(NoResultGovernanceAction.query_id == query.id).one()
        self.assertEqual(action.status, "manual_review")
        self.assertEqual(action.risk_level, "high")
        self.assertEqual(self.db.get(NoResultQuery, query.id).status, "manual_required")

    def test_approves_or_rejects_manual_review_action_through_gateway(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=fake_client)
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "关系错误但需人工复核",
                "finalStrategy": "删除错误关系",
                "maxRiskLevel": "medium",
                "actions": [
                    {
                        "actionType": "delete_relationship",
                        "targetInterface": "tagRelationship/delete",
                        "params": {"id": "rel-risky"},
                        "riskLevel": "high",
                        "reason": "删除错误关系",
                    }
                ],
            },
        )
        action = self.db.query(NoResultGovernanceAction).filter(NoResultGovernanceAction.query_id == query.id).one()

        approved = service.approve_manual_action(action.id, operator="ops-user", reason="确认关系错误")

        self.assertEqual(approved["status"], "success")
        self.db.expire_all()
        approved_action = self.db.get(NoResultGovernanceAction, action.id)
        self.assertEqual(approved_action.status, "success")
        self.assertEqual(approved_action.rollback_status, "pending")
        self.assertIn("确认关系错误", approved_action.error_message)
        self.assertEqual(self.db.get(NoResultQuery, query.id).status, "awaiting_retag")

        batch_two = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "防水包", "data_date": target_date, "status": "pending"}],
        )
        query_two = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch_two.id).one()
        service.execute_agent_plan(
            query_id=query_two.id,
            plan={
                "decision": "process",
                "reason": "关系需人工复核",
                "finalStrategy": "删除关系",
                "maxRiskLevel": "medium",
                "actions": [
                    {
                        "actionType": "delete_relationship",
                        "targetInterface": "tagRelationship/delete",
                        "params": {"id": "rel-keep"},
                        "riskLevel": "high",
                        "reason": "删除关系",
                    }
                ],
            },
        )
        rejected_action = self.db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.query_id == query_two.id
        ).one()

        rejected = service.reject_manual_action(rejected_action.id, operator="ops-user", reason="证据不足")

        self.assertEqual(rejected["status"], "rejected")
        self.db.expire_all()
        updated_rejected_action = self.db.get(NoResultGovernanceAction, rejected_action.id)
        self.assertEqual(updated_rejected_action.status, "rejected")
        self.assertIn("证据不足", updated_rejected_action.error_message)
        self.assertEqual(self.db.get(NoResultQuery, query_two.id).status, "manual_required")

    def test_rolls_back_successful_actions_in_reverse_order(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=fake_client)
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[
                {
                    "brand": "summit",
                    "query": "露营天幕",
                    "data_date": target_date,
                    "status": "pending",
                }
            ],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "新增标签并新增关系",
                "actions": [
                    {
                        "actionType": "add_tag",
                        "targetInterface": "tag/addTag",
                        "target": {"tagName": "天幕"},
                        "riskLevel": "medium",
                        "reason": "新增标签",
                    },
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "天幕", "responseTagName": "露营"},
                        "riskLevel": "medium",
                        "reason": "新增关系",
                    },
                ],
            },
        )

        rollback = service.rollback_batch(batch.id)

        self.assertEqual(rollback["rolledBackActions"], 2)
        self.assertEqual(fake_client.calls[-2]["actionType"], "rollback_add_relationship")
        self.assertEqual(fake_client.calls[-1]["actionType"], "rollback_add_tag")
        self.db.expire_all()
        actions = self.db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.batch_id == batch.id
        ).all()
        original_actions = [action for action in actions if not action.action_type.startswith("rollback_")]
        rollback_actions = [action for action in actions if action.action_type.startswith("rollback_")]
        self.assertTrue(all(action.rollback_status == "success" for action in original_actions))
        self.assertEqual(len(rollback_actions), 2)
        self.assertTrue(all(action.rollback_status == "not_required" for action in rollback_actions))
        self.assertEqual(self.db.get(NoResultQuery, query.id).status, "rolled_back")

    def test_stops_batch_rollback_when_current_graph_conflicts_with_before_snapshot(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        fake_client.conflicts.append({"hasConflict": True, "reason": "关系已被其他批次修改"})
        service = NoResultGovernanceService(kg_client=fake_client)
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "新增关系",
                "actions": [
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "天幕", "responseTagName": "露营"},
                        "riskLevel": "medium",
                        "reason": "新增关系",
                    },
                ],
            },
        )
        self.assertEqual(len(fake_client.calls), 1)

        rollback = service.rollback_batch(batch.id)

        self.assertEqual(rollback["status"], "conflict_manual_review")
        self.assertEqual(rollback["rolledBackActions"], 0)
        self.assertEqual(len(fake_client.calls), 1)
        self.db.expire_all()
        updated_action = self.db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.batch_id == batch.id
        ).one()
        updated_batch = self.db.get(type(batch), batch.id)
        updated_query = self.db.get(NoResultQuery, query.id)
        self.assertEqual(updated_action.rollback_status, "conflict")
        self.assertIn("关系已被其他批次修改", updated_action.error_message)
        self.assertEqual(updated_batch.rollback_status, "conflict_manual_review")
        self.assertEqual(updated_query.status, "manual_required")

    def test_successful_rollback_requeues_retag_for_batch(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[{"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.execute_agent_plan(
            query_id=query.id,
            plan={
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "新增关系",
                "actions": [
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "天幕", "responseTagName": "露营"},
                        "riskLevel": "medium",
                        "reason": "新增关系",
                    },
                ],
            },
        )

        rollback = service.rollback_batch(batch.id, now=target_date)

        self.assertEqual(rollback["status"], "rolled_back")
        self.db.expire_all()
        updated_batch = self.db.get(type(batch), batch.id)
        retag_task = self.db.query(NoResultRetagTask).one()
        self.assertEqual(updated_batch.retag_status, "pending")
        self.assertEqual(retag_task.brand, "summit")
        self.assertIn(str(batch.id), retag_task.batch_ids)

    def test_rolls_back_only_one_query_inside_batch(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        fake_client = FakeKnowledgeGraphClient()
        service = NoResultGovernanceService(kg_client=fake_client)
        batch = service.create_batch_from_records(
            brand="summit",
            target_date=target_date,
            records=[
                {"brand": "summit", "query": "露营天幕", "data_date": target_date, "status": "pending"},
                {"brand": "summit", "query": "防水包", "data_date": target_date, "status": "pending"},
            ],
        )
        queries = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).order_by(NoResultQuery.id).all()
        for query in queries:
            service.execute_agent_plan(
                query_id=query.id,
                plan={
                    "decision": "process",
                    "reason": "购物意图明确",
                    "finalStrategy": "新增关系",
                    "actions": [
                        {
                            "actionType": "add_relationship",
                            "targetInterface": "tagRelationship/addRelationship",
                            "target": {"tagName": query.query_text, "responseTagName": "露营"},
                            "riskLevel": "medium",
                            "reason": "新增关系",
                        },
                    ],
                },
            )

        rollback = service.rollback_query(queries[0].id, now=target_date)

        self.assertEqual(rollback["queryId"], queries[0].id)
        self.assertEqual(rollback["rolledBackActions"], 1)
        self.db.expire_all()
        first_query = self.db.get(NoResultQuery, queries[0].id)
        second_query = self.db.get(NoResultQuery, queries[1].id)
        first_actions = self.db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.query_id == queries[0].id,
            NoResultGovernanceAction.action_type == "add_relationship",
        ).all()
        second_actions = self.db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.query_id == queries[1].id,
            NoResultGovernanceAction.action_type == "add_relationship",
        ).all()
        self.assertEqual(first_query.status, "rolled_back")
        self.assertEqual(second_query.status, "awaiting_retag")
        self.assertTrue(all(action.rollback_status == "success" for action in first_actions))
        self.assertTrue(all(action.rollback_status == "pending" for action in second_actions))

    def test_workflow_nodes_expose_no_result_governance_steps(self):
        target_date = datetime.datetime(2026, 6, 22)
        normalized = no_result_normalize_node({
            "brand": "summit",
            "target_date": target_date,
            "raw_rows": [{"keyword": "露营天幕", "no_result_count": 12}],
            "excluded_keywords": [],
        })

        self.assertEqual(normalized["records"][0]["query"], "露营天幕")

        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        batch = service.create_batch_from_records("summit", target_date, normalized["records"])
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        result = no_result_execute_plan_node({
            "query_id": query.id,
            "plan": {
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "建立露营从属关系",
                "actions": [
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "天幕", "responseTagName": "露营"},
                        "riskLevel": "medium",
                        "reason": "建立从属关系提升召回",
                    }
                ],
            },
            "service": service,
        })

        self.assertEqual(result["execution_result"]["executedActions"], 1)

    def test_workflow_nodes_expose_retag_and_recall_review(self):
        target_date = datetime.datetime(2026, 6, 22)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        batch = service.create_batch_from_records(
            "harbor",
            target_date,
            [{"query": "粉色大衣", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.execute_agent_plan(
            query.id,
            {
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "新增同义关系",
                "actions": [
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "粉红色", "responseTagName": "粉色"},
                        "riskLevel": "low",
                        "reason": "补同义关系",
                    }
                ],
            },
        )

        queued = no_result_queue_retag_node({
            "batch_id": batch.id,
            "service": service,
            "now": target_date,
        })
        service.run_due_retag_tasks(ops_service=FakeOperationalService(), now=target_date)
        reviewed = no_result_review_recall_node({
            "query_id": query.id,
            "service": service,
            "ops_service": FakeOperationalService(products=[{"skuId": "SKU-1", "productName": "粉色大衣"}]),
            "top_n": 16,
        })

        self.assertTrue(queued["retag_queue_result"]["queued"])
        self.assertEqual(reviewed["review_result"]["status"], "completed")

    def test_workflow_nodes_expose_intent_segment_context_and_diagnosis(self):
        target_date = datetime.datetime(2026, 6, 22)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        batch = service.create_batch_from_records(
            "harbor",
            target_date,
            [{"query": "粉红色大衣", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()

        intent = no_result_filter_intent_node({
            "query_id": query.id,
            "service": service,
            "agent": FakeNoResultIntentAgent("process"),
        })
        segmented = no_result_segment_node({
            "query_id": query.id,
            "service": service,
        })
        context = no_result_graph_context_node({
            "query_id": query.id,
            "service": service,
            "candidate_tags": segmented["candidate_tags"],
        })
        diagnosed = no_result_diagnose_node({
            "query_id": query.id,
            "service": service,
            "agent": FakeNoResultDiagnosisAgent(),
            "graph_context": context["graph_context"],
        })

        self.assertEqual(intent["intent_result"]["decision"], "process")
        self.assertIn("大衣", segmented["candidate_tags"])
        self.assertIn("tags", context["graph_context"])
        self.assertEqual(diagnosed["diagnosis_result"]["outputType"], "strategy")
        self.db.expire_all()
        self.assertIn("add_relationship", self.db.get(NoResultQuery, query.id).final_strategy)

    def test_workflow_nodes_share_a_captured_no_result_model_snapshot(self):
        target_date = datetime.datetime(2026, 6, 22)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        batch = service.create_batch_from_records(
            "harbor",
            target_date,
            [{"query": "粉红色大衣", "data_date": target_date, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        snapshot = capture_snapshot(
            node_keys=("no_result.intent", "no_result.diagnosis"),
        )

        intent = no_result_filter_intent_node({
            "query_id": query.id,
            "service": service,
            "agent": FakeNoResultIntentAgent("process"),
            "model_config_snapshot": snapshot,
        })
        diagnosis = no_result_diagnose_node({
            "query_id": query.id,
            "service": service,
            "agent": FakeNoResultDiagnosisAgent(),
            "model_config_snapshot": intent["model_config_snapshot"],
        })

        self.assertIs(intent["model_config_snapshot"], snapshot)
        self.assertIs(diagnosis["model_config_snapshot"], snapshot)

    def test_no_result_workflow_rejects_missing_snapshot_node_before_query_lookup(self):
        with self.assertRaises(NodeConfigError):
            no_result_filter_intent_node({
                "query_id": 1,
                "service": NoResultGovernanceService(),
                "model_config_snapshot": NodeConfigSnapshot(captured_at="", nodes={}),
            })

    def test_diagnosis_query_request_stops_at_max_rounds(self):
        target_date = datetime.datetime(2026, 6, 22)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        batch = service.create_batch_from_records(
            "harbor",
            target_date,
            [{
                "query": "粉红色大衣",
                "data_date": target_date,
                "status": "pending_diagnosis",
                "raw_payload": {"noResultDiagnosisRounds": 1},
            }],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()

        result = service.diagnose_query(
            query.id,
            agent=FakeQueryRequestDiagnosisAgent(),
            max_rounds=1,
        )

        self.assertEqual(result["outputType"], "manual_required")
        self.db.expire_all()
        updated_query = self.db.get(NoResultQuery, query.id)
        self.assertEqual(updated_query.status, "manual_required")
        self.assertIn("最大查询轮次", updated_query.final_strategy)

    def test_queues_retag_once_per_brand_cooldown_window(self):
        now = datetime.datetime(2026, 6, 22, 10, 0, 0)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        batch_one = service.create_batch_from_records(
            "summit",
            now,
            [{"query": "露营天幕", "data_date": now, "status": "pending"}],
        )
        batch_two = service.create_batch_from_records(
            "summit",
            now,
            [{"query": "防水包", "data_date": now, "status": "pending"}],
        )
        query_one = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch_one.id).one()
        query_two = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch_two.id).one()
        plan = {
            "decision": "process",
            "reason": "购物意图明确",
            "finalStrategy": "补充图谱关系",
            "actions": [
                {
                    "actionType": "add_relationship",
                    "targetInterface": "tagRelationship/addRelationship",
                    "target": {"tagName": "天幕", "responseTagName": "露营"},
                    "riskLevel": "low",
                    "reason": "补关系",
                }
            ],
        }
        service.execute_agent_plan(query_one.id, plan)
        service.execute_agent_plan(query_two.id, plan)

        first = service.queue_retag_for_batch(batch_one.id, now=now)
        second = service.queue_retag_for_batch(batch_two.id, now=now + datetime.timedelta(minutes=10))

        self.assertEqual(first["taskId"], second["taskId"])
        self.db.expire_all()
        task = self.db.query(NoResultRetagTask).one()
        self.assertEqual(task.brand, "summit")
        self.assertIn(str(batch_one.id), task.batch_ids)
        self.assertIn(str(batch_two.id), task.batch_ids)
        self.assertEqual(task.status, "pending")

    def test_runs_due_retag_task_and_marks_queries_waiting_review(self):
        now = datetime.datetime(2026, 6, 22, 10, 0, 0)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        batch = service.create_batch_from_records(
            "harbor",
            now,
            [{"query": "粉色大衣", "data_date": now, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()
        service.execute_agent_plan(
            query.id,
            {
                "decision": "process",
                "reason": "购物意图明确",
                "finalStrategy": "新增同义关系",
                "actions": [
                    {
                        "actionType": "add_relationship",
                        "targetInterface": "tagRelationship/addRelationship",
                        "target": {"tagName": "粉红色", "responseTagName": "粉色"},
                        "riskLevel": "low",
                        "reason": "补同义关系",
                    }
                ],
            },
        )
        service.queue_retag_for_batch(batch.id, now=now)

        result = service.run_due_retag_tasks(ops_service=FakeOperationalService(), now=now)

        self.assertEqual(result["success"], 1)
        self.db.expire_all()
        updated_query = self.db.get(NoResultQuery, query.id)
        task = self.db.query(NoResultRetagTask).one()
        self.assertEqual(updated_query.status, "retagged_pending_review")
        self.assertEqual(task.status, "success")
        self.assertIsNotNone(task.triggered_at)

    def test_reviews_recall_result_to_completed_or_manual_required(self):
        target_date = datetime.datetime(2026, 6, 22, 9, 0, 0)
        service = NoResultGovernanceService()
        batch = service.create_batch_from_records(
            "riviera",
            target_date,
            [{"query": "绞花毛衣", "data_date": target_date, "status": "retagged_pending_review"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()

        completed = service.review_query_recall(
            query.id,
            ops_service=FakeOperationalService(products=[{"productName": "绞花羊毛毛衣", "skuId": "SKU-1"}]),
            top_n=16,
        )

        self.assertEqual(completed["status"], "completed")
        self.db.expire_all()
        updated_query = self.db.get(NoResultQuery, query.id)
        self.assertEqual(updated_query.after_result_count, 1)
        self.assertEqual(updated_query.status, "completed")

        batch_two = service.create_batch_from_records(
            "riviera",
            target_date,
            [{"query": "不存在词", "data_date": target_date, "status": "retagged_pending_review"}],
        )
        query_two = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch_two.id).one()

        manual = service.review_query_recall(
            query_two.id,
            ops_service=FakeOperationalService(products=[]),
            top_n=16,
        )

        self.assertEqual(manual["status"], "manual_required")
        self.db.expire_all()
        self.assertEqual(self.db.get(NoResultQuery, query_two.id).status, "manual_required")

    def test_governance_cycle_advances_pending_query_to_reviewed_result(self):
        now = datetime.datetime(2026, 6, 22, 10, 0, 0)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        batch = service.create_batch_from_records(
            "harbor",
            now,
            [{"query": "粉红色大衣", "data_date": now, "status": "pending"}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()

        result = service.run_governance_cycle(
            intent_agent=FakeNoResultIntentAgent("process"),
            diagnosis_agent=FakeNoResultDiagnosisAgent(),
            ops_service=FakeOperationalService(products=[{"productName": "粉色大衣", "skuId": "SKU-1"}]),
            now=now,
            review_wait_minutes=0,
            limit=10,
        )

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["executedActions"], 0)
        self.assertEqual(result["retagSuccess"], 0)
        self.assertEqual(result["reviewed"], 0)
        self.db.expire_all()
        updated_query = self.db.get(NoResultQuery, query.id)
        self.assertEqual(updated_query.status, "pending_execute")
        self.assertIsNone(updated_query.after_result_count)
        self.assertEqual(
            self.db.query(NoResultGovernanceAction).filter(NoResultGovernanceAction.query_id == query.id).count(),
            1,
        )
        action = self.db.query(NoResultGovernanceAction).filter(NoResultGovernanceAction.query_id == query.id).one()
        self.assertEqual(action.status, "pending_execute")

    def test_governance_cycle_injects_one_snapshot_into_default_agents(self):
        now = datetime.datetime(2026, 6, 22, 10, 0, 0)
        service = NoResultGovernanceService(kg_client=FakeKnowledgeGraphClient())
        service.create_batch_from_records(
            "harbor",
            now,
            [{"query": "粉红色大衣", "data_date": now, "status": "pending"}],
        )
        snapshot = capture_snapshot(
            node_keys=("no_result.intent", "no_result.diagnosis"),
        )

        with patch("src.llm.agents.NoResultQueryIntentAgent", return_value=FakeNoResultIntentAgent("process")) as intent_agent, patch(
            "src.llm.agents.NoResultDiagnosisAgent",
            return_value=FakeNoResultDiagnosisAgent(),
        ) as diagnosis_agent:
            service.run_governance_cycle(
                ops_service=FakeOperationalService(),
                now=now,
                model_config_snapshot=snapshot,
            )

        self.assertIs(intent_agent.call_args.kwargs["model_config_snapshot"], snapshot)
        self.assertIs(diagnosis_agent.call_args.kwargs["model_config_snapshot"], snapshot)

    def test_ignored_query_is_recorded_with_ttl_and_filtered_until_expired(self):
        now = datetime.datetime(2026, 6, 22, 10, 0, 0)
        service = NoResultGovernanceService()
        batch = service.create_batch_from_records(
            "harbor",
            now,
            [{"query": "积分兑换", "data_date": now, "status": "pending", "search_pv": 20}],
        )
        query = self.db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch.id).one()

        result = service.filter_query_intent(
            query.id,
            agent=FakeNoResultIntentAgent("ignore"),
            now=now,
            ignore_ttl_days=7,
        )

        self.assertEqual(result["decision"], "ignore")
        self.db.expire_all()
        ignore = self.db.query(NoResultIgnoreRecord).filter_by(brand="harbor", query_text="积分兑换").one()
        self.assertEqual(ignore.reason, "测试判断")
        self.assertEqual(ignore.source, "llm_intent_filter")
        self.assertEqual(ignore.expires_at, now + datetime.timedelta(days=7))

        filtered = service.normalize_rows(
            "harbor",
            now + datetime.timedelta(days=1),
            [{"search_text": "积分兑换", "search_pv": 30}, {"search_text": "粉色大衣", "search_pv": 10}],
            now=now + datetime.timedelta(days=1),
        )
        self.assertEqual([row["query"] for row in filtered], ["粉色大衣"])

        after_expiry = service.normalize_rows(
            "harbor",
            now + datetime.timedelta(days=8),
            [{"search_text": "积分兑换", "search_pv": 30}],
            now=now + datetime.timedelta(days=8),
        )
        self.assertEqual([row["query"] for row in after_expiry], ["积分兑换"])

    def test_normalize_rows_applies_pv_and_uv_thresholds(self):
        target_date = datetime.datetime(2026, 6, 22)
        service = NoResultGovernanceService()

        rows = service.normalize_rows(
            "harbor",
            target_date,
            [
                {"search_text": "低PV", "search_pv": 2, "search_uv": 10},
                {"search_text": "低UV", "search_pv": 20, "search_uv": 1},
                {"search_text": "有效词", "search_pv": 20, "search_uv": 6},
            ],
            min_search_pv=5,
            min_search_uv=3,
        )

        self.assertEqual([row["query"] for row in rows], ["有效词"])

    def test_knowledge_graph_client_executes_add_relationship_with_scene_id(self):
        http = FakeHttpClient()
        client = KnowledgeGraphClient(http_client=http, token="kg-token")

        result = client.execute({
            "brand": "summit",
            "actionType": "add_relationship",
            "params": {
                "relationships": KnowledgeGraphClient.RELATION_SUBORDINATE,
                "tagId": "tag-child",
                "tagName": "大衣",
                "dimensionNames": "品类",
                "responseTagId": "tag-parent",
                "responseTagName": "外套",
            },
        })

        self.assertEqual(result["code"], 1)
        method, url, kwargs = http.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/admin/tagRelationship/addRelationship"))
        self.assertEqual(kwargs["headers"]["authorization"], "Bearer kg-token")
        params = kwargs["json"]["params"]
        self.assertEqual(params["sceneId"], "1258129681140809728")
        self.assertEqual(params["tagId"], "tag-child")

    def test_knowledge_graph_client_injects_external_request_id_headers(self):
        http = FakeHttpClient()
        client = KnowledgeGraphClient(http_client=http, token="kg-token")

        client.execute(
            {
                "brand": "summit",
                "actionType": "add_relationship",
                "params": {"tagId": "child", "responseTagId": "parent"},
            },
            external_request_id="request-kg-1",
        )

        _, _, kwargs = http.calls[0]
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], "request-kg-1")
        self.assertEqual(kwargs["headers"]["X-Request-ID"], "request-kg-1")

    def test_knowledge_graph_reads_and_writes_use_shared_governor(self):
        acquired_keys = []

        class RecordingGovernor:
            @contextmanager
            def acquire(self, key, request_timeout):
                acquired_keys.append((key, request_timeout))
                yield

            def request_with_rate_limit_retry(
                self, *, key, method, url, request_func, request_timeout, **kwargs
            ):
                kwargs.pop("max_attempts", None)
                with self.acquire(key, request_timeout):
                    return request_func(method, url, **kwargs)

        http = FakeHttpClient()
        client = KnowledgeGraphClient(http_client=http, token="kg-token")
        client.request_governor = RecordingGovernor()
        client.environment = "test"

        client.execute({
            "brand": "atlas",
            "actionType": "add_relationship",
            "params": {
                "tagId": "child",
                "responseTagId": "parent",
            },
        })

        token_hash = hashlib.sha256(b"kg-token").hexdigest()[:16]
        self.assertEqual(
            acquired_keys,
            [
                ("test|search.example.test|tenant:235", 15.0),
                (f"test|kg.example.test|kg:{token_hash}", 15.0),
            ],
        )

    def test_knowledge_graph_write_retries_429_with_retry_after(self):
        clock = FakeClock()
        http = MagicMock()
        http.post.side_effect = [
            MagicMock(
                status_code=429,
                headers={"Retry-After": "2"},
                text="limited",
                json=lambda: {"code": 429, "message": "limited"},
            ),
            MagicMock(
                status_code=200,
                headers={},
                text="success",
                json=lambda: {"code": 1, "message": "success"},
            ),
        ]
        client = KnowledgeGraphClient(http_client=http, token="kg-token")
        client.request_governor = BusinessRequestGovernor(
            clock=clock.time,
            sleep=clock.sleep,
        )

        result = client.execute({
            "brand": "summit",
            "actionType": "add_relationship",
            "params": {"tagId": "child", "responseTagId": "parent"},
        })

        self.assertEqual(result["code"], 1)
        self.assertEqual(http.post.call_count, 2)
        self.assertEqual(clock.sleeps, [2.0])

    def test_knowledge_graph_write_respects_identity_circuit(self):
        class OpenCircuit:
            def admit(self, key):
                return None

        http = MagicMock()
        client = KnowledgeGraphClient(http_client=http, token="kg-token")
        client.circuit_registry = OpenCircuit()

        with self.assertRaises(BusinessCircuitOpen):
            client.execute({
                "brand": "summit",
                "actionType": "add_relationship",
                "params": {"tagId": "child", "responseTagId": "parent"},
            })

        http.post.assert_not_called()

    def test_knowledge_graph_write_records_availability_failure(self):
        http = MagicMock()
        http.post.return_value = MagicMock(
            status_code=503,
            headers={},
            text="unavailable",
            json=lambda: {"code": 503, "message": "unavailable"},
        )
        client = KnowledgeGraphClient(http_client=http, token="kg-token")
        client.circuit_registry = CircuitRegistry(failure_threshold=2, cooldown_seconds=60)

        client.execute({
            "brand": "summit",
            "actionType": "add_relationship",
            "params": {"tagId": "child", "responseTagId": "parent"},
        })

        token_hash = hashlib.sha256(b"kg-token").hexdigest()[:16]
        status = client.circuit_registry.status(
            f"test|kg.example.test|kg:{token_hash}"
        )
        self.assertEqual(status["consecutive_failures"], 1)

    def test_knowledge_graph_client_normalizes_add_tag_params_for_real_api(self):
        http = FakeHttpClient()
        client = KnowledgeGraphClient(http_client=http, token="kg-token")

        client.execute({
            "brand": "riviera",
            "actionType": "add_tag",
            "params": {
                "tagName": "v领T恤",
                "dimensionId": "1487492577607548938",
                "sceneId": "1506343548977807360",
            },
        })

        method, url, kwargs = http.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/admin/tag/addTag"))
        params = kwargs["json"]["params"]
        self.assertEqual(params["name"], "v领T恤")
        self.assertEqual(params["viewTag"], "v领T恤")
        self.assertEqual(params["dimensions"], "1487492577607548938")
        self.assertEqual(params["sceneId"], "1506343548977807360")
        self.assertEqual(params["isEnglish"], 0)
        self.assertEqual(params["isRoot"], "1")
        self.assertEqual(params["segmentingAgain"], "1")
        self.assertEqual(params["isCompose"], "0")
        self.assertEqual(params["weight"], 200)
        self.assertEqual(params["privateIds"], [])
        self.assertEqual(params["privateId"], "")
        self.assertEqual(params["privateName"], "")
        self.assertEqual(params["extendType"], "")
        self.assertEqual(params["extendContent"], "")
        self.assertEqual(params["homophonic"], "")
        self.assertNotIn("tagName", params)
        self.assertNotIn("dimensionId", params)
        self.assertNotIn("dimensionName", params)

    def test_knowledge_graph_client_does_not_infer_english_from_ascii_tag(self):
        http = FakeHttpClient()
        client = KnowledgeGraphClient(http_client=http, token="kg-token")

        client.execute({
            "brand": "harbor",
            "actionType": "add_tag",
            "params": {
                "tagName": "STELLA",
                "dimensionId": "1487492577607548938",
                "sceneId": "1487491598584774656",
            },
        })

        params = http.calls[0][2]["json"]["params"]
        self.assertEqual(params["isEnglish"], 0)

    def test_knowledge_graph_client_builds_add_tag_rollback_from_response_key_and_request_scene(self):
        client = KnowledgeGraphClient(http_client=FakeHttpClient(), token="kg-token")
        action = {
            "brand": "harbor",
            "actionType": "add_tag",
            "params": {
                "name": "测试标签",
                "sceneId": "scene-1",
                "dimensions": "dimension-1",
            },
        }
        response = {
            "code": 1,
            "data": {
                "id": "kg_tag/tag-key-1",
                "key": "tag-key-1",
                "properties": {
                    "scene_id": "",
                    "tag_id": "tag-business-1",
                },
            },
        }

        rollback = client.build_rollback(action, {"target": {}}, response)

        self.assertEqual(rollback["actionType"], "blacklist_tag")
        self.assertEqual(rollback["params"]["id"], "tag-key-1")
        self.assertEqual(rollback["params"]["sceneId"], "scene-1")
        self.assertEqual(rollback["params"]["tagId"], "tag-business-1")
        self.assertEqual(rollback["params"]["isBlack"], 1)

    def test_knowledge_graph_client_snapshots_deleted_relationship_and_builds_restore_rollback(self):
        http = FakeHttpClient(responses=[
            {
                "code": 1,
                "data": [
                    {
                        "id": "rel-1",
                        "sceneId": "1258129681140809728",
                        "relationships": ["902227092548485120"],
                        "tagName": "大衣",
                        "tagDimensions": ["品类"],
                        "responseTagName": "外套",
                    }
                ],
            },
            {"code": 1, "message": "success", "data": "rel-1"},
        ])
        client = KnowledgeGraphClient(http_client=http, token="kg-token")
        action = {
            "brand": "summit",
            "actionType": "delete_relationship",
            "params": {"id": "rel-1"},
        }

        snapshot = client.snapshot(action)
        response = client.execute(action)
        rollback = client.build_rollback(action, snapshot, response)

        self.assertEqual(snapshot["data"][0]["id"], "rel-1")
        self.assertEqual(rollback["actionType"], "add_relationship")
        self.assertEqual(rollback["params"]["tagName"], "大衣")
        self.assertEqual(rollback["params"]["responseTagName"], "外套")

    def test_default_kg_client_requires_explicit_real_write_flag(self):
        with patch.dict("os.environ", {}, clear=True):
            dry_run_service = NoResultGovernanceService()
            self.assertIsInstance(dry_run_service.kg_client, DryRunKnowledgeGraphClient)

        with patch.dict("os.environ", {"NO_RESULT_KG_REAL_WRITE": "true", "KG_API_TOKEN": "token"}, clear=True):
            real_service = NoResultGovernanceService()
            self.assertIsInstance(real_service.kg_client, KnowledgeGraphClient)

    def test_dry_run_kg_client_allows_queries_and_blocks_writes(self):
        real_client = FakeKnowledgeGraphClient()
        client = DryRunKnowledgeGraphClient(query_client=real_client)

        query_response = client.execute({"brand": "summit", "actionType": "query_tag", "params": {"name": "天幕"}})
        write_response = client.execute({"brand": "summit", "actionType": "add_tag", "params": {"name": "天幕"}})

        self.assertEqual(query_response["message"], "success")
        self.assertEqual(len(real_client.calls), 1)
        self.assertEqual(real_client.calls[0]["actionType"], "query_tag")
        self.assertEqual(write_response["message"], "dry_run_write_blocked")
        self.assertEqual(len(real_client.calls), 1)


if __name__ == "__main__":
    unittest.main()
