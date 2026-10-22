import datetime
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.async_tasks import (
    QueryGovernanceAsyncTaskRepository,
    QueryGovernanceAsyncTaskService,
)
from src.query_governance.kg_agent import (
    KGAgent,
    KGAgentError,
    KGAgentResult,
    KGAgentStatus,
    KGPlanConflictChecker,
    KGPlanConflictError,
    KGPlanExecutionGuard,
    KGPlanExecutor,
    KnowledgeGraphReadTools,
    QueryGovernanceActionPlanRepository,
    QueryGovernanceKGWorker,
)
from src.query_governance.models import (
    NodeResult,
    QueryGovernanceActionPlan,
    QueryGovernanceAsyncTask,
    QueryGovernanceTask,
)
from src.query_governance.langfuse import QueryGovernanceObservability


class _FakeRuntime:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []
        self.kwargs = []

    def invoke(self, messages, **kwargs):
        self.messages.append(messages)
        self.kwargs.append(kwargs)
        return self.responses.pop(0)


class _FakeKGClient:
    def __init__(self):
        self.calls = []
        self.responses = {
            "query_tag": {"data": [{"id": "tag-1", "name": "裙子"}]},
            "query_relationship": {"data": []},
        }

    def execute(self, action):
        self.calls.append(action)
        return self.responses[action["actionType"]]

    def snapshot(self, action):
        return {"before": self.responses.get("query_relationship", {"data": []})}

    def build_rollback(self, action, snapshot, response):
        return {
            "brand": action["brand"],
            "actionType": "delete_relationship",
            "params": {"id": response.get("data", {}).get("id")},
        }


class _FakeSearch:
    def __init__(self, counts=None):
        self.counts = counts or {"裙子": 12, "连衣裙": 42, "牛仔裤": 18}
        self.calls = []

    def mixed_with_total(self, brand, query, *, page_size=None, page_num=None):
        self.calls.append((brand, query, page_size, page_num))
        count = self.counts.get(query, 0)
        return {
            "success": True,
            "total_count": count,
            "products": [{"product_id": f"{query}-1", "product_name": query}] if count else [],
        }


class _FakeObservation:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.updates = []
        self.end_calls = 0

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return self

    def end(self):
        self.end_calls += 1
        return self


class _ObservingClient(_FakeKGClient):
    def __init__(self):
        super().__init__()
        self.observations = []

    def start_observation(self, **kwargs):
        observation = _FakeObservation(**kwargs)
        self.observations.append(observation)
        return observation


@pytest.fixture
def task_fixture():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            QueryGovernanceTask.__table__,
            QueryGovernanceAsyncTask.__table__,
            QueryGovernanceActionPlan.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        task = QueryGovernanceTask(
            batch_id="batch-1",
            brand="atlas",
            data_date=datetime.date(2026, 8, 29),
            query_text="裙子",
            status="pending",
        )
        session.add(task)
        session.commit()
        task_id = task.id
    async_service = QueryGovernanceAsyncTaskService(
        QueryGovernanceAsyncTaskRepository(factory)
    )
    submitted = async_service.enqueue(
        task_id=task_id,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={"result_count": 0},
        idempotency_key="kg:batch-1:atlas:裙子:recall",
    )
    yield factory, async_service, submitted.task_id
    engine.dispose()


def test_read_tools_expose_graph_product_search_and_dimension_query():
    client = _FakeKGClient()
    search = _FakeSearch()
    tools = KnowledgeGraphReadTools(client, search=search)

    assert tools.query_tag("atlas", "裙子")["data"][0]["name"] == "裙子"
    assert tools.query_relationship("atlas", "裙子")["data"] == []
    assert tools.search_products("atlas", "裙子")["result_count"] == 12
    assert {call["actionType"] for call in client.calls} == {
        "query_tag",
        "query_relationship",
    }
    assert hasattr(tools, "query_dimension")
    with pytest.raises(PermissionError):
        tools.execute({"actionType": "add_tag", "params": {"name": "裙子"}})


def test_read_tools_emit_sanitized_tool_observation_on_query_trace():
    client = _ObservingClient()
    client.responses["query_tag"] = {
        "data": [{"id": "tag-1", "name": "裙子", "api_key": "must-not-leak"}],
        "raw_response": {"secret": "must-not-leak"},
    }
    observability = QueryGovernanceObservability(client=client, enabled=True)
    tools = KnowledgeGraphReadTools(client, search=_FakeSearch(), observability=observability)

    result = tools.query_tag(
        "atlas",
        "裙子",
        langfuse_config={
            "metadata": {
                "query_trace_id": "query-trace-1",
                "query_id": "q1",
                "api_key": "must-not-leak",
            }
        },
    )

    assert result["data"][0]["name"] == "裙子"
    assert len(client.observations) == 1
    observation = client.observations[0]
    assert observation.kwargs["name"] == "kg.query_tag"
    assert observation.kwargs["as_type"] == "tool"
    assert observation.kwargs["trace_context"] == {"trace_id": "query-trace-1"}
    assert observation.kwargs["metadata"]["query_trace_id"] == "query-trace-1"
    assert observation.kwargs["metadata"]["query_id"] == "q1"
    assert observation.kwargs["input"] == {
        "brand": "atlas",
        "params": {"name": "裙子", "nameAcc": "裙子", "pageNum": 1, "pageSize": 30},
    }
    rendered = repr(observation.updates)
    assert "must-not-leak" not in rendered
    assert observation.end_calls == 1


def test_read_tool_failure_is_observed_and_graph_collection_continues():
    class FailingClient(_ObservingClient):
        def execute(self, action):
            self.calls.append(action)
            if action["actionType"] == "query_relationship":
                raise TimeoutError("graph timeout")
            return self.responses[action["actionType"]]

    client = FailingClient()
    observability = QueryGovernanceObservability(client=client, enabled=True)
    tools = KnowledgeGraphReadTools(client, search=_FakeSearch(), observability=observability)

    context = tools.collect_context(
        "atlas",
        ["裙子"],
        langfuse_config={"metadata": {"query_trace_id": "query-trace-1"}},
    )

    assert context["tags"]["裙子"]["data"][0]["name"] == "裙子"
    assert context["relationships"]["裙子"]["error"]["code"] == "KG_GRAPH_READ_FAILED"
    assert context["searches"]["裙子"]["result_count"] == 12
    assert len(client.observations) == 3
    relationship_observation = next(
        item for item in client.observations if item.kwargs["name"] == "kg.query_relationship"
    )
    assert relationship_observation.updates[0]["level"] == "ERROR"
    assert relationship_observation.end_calls == 1


@pytest.mark.parametrize("trigger_source,evidence", [("recall_anomaly", {"result_count": 0})])
def test_agent_generates_structured_plan_for_recall_trigger(trigger_source, evidence):
    runtime = _FakeRuntime(
        [
            '{"core_tags":["裙子"]}',
            json.dumps(
                {
                    "reason": "补齐 Query 标签关系",
                    "actions": [
                        {
                            "actionType": "add_relationship",
                            "targetInterface": "/admin/unsupported/write",
                            "riskLevel": "medium",
                            "params": {
                                "tagName": "裙子",
                                "responseTagName": "连衣裙",
                            },
                            "reason": "当前关系不存在",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        ]
    )
    client = _FakeKGClient()
    result = KGAgent(runtime=runtime, tools=KnowledgeGraphReadTools(client, search=_FakeSearch())).generate(
        brand="atlas",
        query_text="裙子",
        trigger_source=trigger_source,
        evidence=evidence,
    )

    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert result.plan["trigger_source"] == trigger_source
    assert result.plan["actions"][0]["actionType"] == "add_relationship"
    assert (
        result.plan["actions"][0]["targetInterface"]
        == "/admin/tagRelationship/addRelationship"
    )
    assert result.plan["decision"] == "adjust"
    assert result.plan["tool_evidence"]["searches"]["裙子"]["result_count"] == 12
    assert len(runtime.messages) == 2
    assert runtime.kwargs == [
        {"response_format": {"type": "json_object"}},
        {"response_format": {"type": "json_object"}},
    ]


def test_agent_forwards_langfuse_config_to_each_model_call():
    runtime = _FakeRuntime(
        ['{"core_tags":["裙子"]}', '{"reason":"无需调整","actions":[]}']
    )
    langfuse_config = {
        "callbacks": ["query-handler"],
        "metadata": {"batch_id": "batch-1", "query_id": "q1"},
        "run_name": "query-governance.kg_agent",
    }

    result = KGAgent(
        runtime=runtime,
        tools=KnowledgeGraphReadTools(_FakeKGClient(), search=_FakeSearch()),
    ).generate(
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={},
        langfuse_config=langfuse_config,
    )

    assert result.status is KGAgentStatus.PLAN_GENERATED
    expected_kwargs = {**langfuse_config, "response_format": {"type": "json_object"}}
    assert runtime.kwargs == [expected_kwargs, expected_kwargs]


def test_worker_forwards_langfuse_config_to_agent(task_fixture):
    factory, async_service, async_task_id = task_fixture
    runtime = _FakeRuntime(
        ['{"core_tags":["裙子"]}', '{"reason":"无需调整","actions":[]}']
    )
    langfuse_config = {
        "callbacks": ["query-handler"],
        "metadata": {"batch_id": "batch-1", "query_id": "q1"},
        "run_name": "query-governance.kg_agent",
    }
    worker = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=KGAgent(
            runtime=runtime,
            tools=KnowledgeGraphReadTools(_FakeKGClient(), search=_FakeSearch()),
        ),
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
    )

    result = worker.process(async_task_id, langfuse_config=langfuse_config)

    assert result.status is KGAgentStatus.PLAN_GENERATED
    expected_kwargs = {**langfuse_config, "response_format": {"type": "json_object"}}
    assert runtime.kwargs == [expected_kwargs, expected_kwargs]


def test_worker_restores_persisted_langfuse_context_when_processing_pending(task_fixture):
    factory, async_service, async_task_id = task_fixture
    persisted_context = {
        "batch_id": "batch-1",
        "batch_trace_id": "batch-trace-1",
        "query_id": "q1",
        "query_trace_id": "query-trace-1",
        "brand": "atlas",
        "query": "裙子",
        "node": "kg_agent",
        "metadata": {"batch_id": "batch-1", "query_id": "q1"},
    }
    async_service.repository.update(
        async_task_id,
        langfuse_context_json=persisted_context,
    )

    class _Observability:
        def __init__(self):
            self.contexts = []

        def resume_query_config(self, context, *, node=None):
            self.contexts.append(context)
            return {
                "callbacks": ["restored-query-handler"],
                "metadata": {"query_trace_id": context["query_trace_id"]},
                "run_name": "query-governance.kg_agent",
            }

    runtime = _FakeRuntime(
        ['{"core_tags":["裙子"]}', '{"reason":"无需调整","actions":[]}']
    )
    observability = _Observability()
    worker = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=KGAgent(runtime=runtime, tools=KnowledgeGraphReadTools(_FakeKGClient(), search=_FakeSearch())),
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
        observability=observability,
    )

    result = worker.process(async_task_id)

    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert observability.contexts == [persisted_context]
    assert runtime.kwargs == [
        {
            "callbacks": ["restored-query-handler"],
            "metadata": {"query_trace_id": "query-trace-1"},
            "run_name": "query-governance.kg_agent",
            "response_format": {"type": "json_object"},
        },
        {
            "callbacks": ["restored-query-handler"],
            "metadata": {"query_trace_id": "query-trace-1"},
            "run_name": "query-governance.kg_agent",
            "response_format": {"type": "json_object"},
        },
    ]


def test_agent_rejects_invalid_structured_output():
    runtime = _FakeRuntime(['{"core_tags":["裙子"]}', '{"actions":[{"actionType":"delete_all"}]}'])
    result = KGAgent(
        runtime=runtime,
        tools=KnowledgeGraphReadTools(_FakeKGClient(), search=_FakeSearch()),
    ).generate(
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={},
    )

    assert result.status is KGAgentStatus.ERROR
    assert result.error_code == "KG_AGENT_INVALID_PLAN"


def test_agent_falls_back_to_query_when_model_returns_no_tags():
    runtime = _FakeRuntime(
        ['{"core_tags":[]}', '{"reason":"无需调整","actions":[]}']
    )
    client = _FakeKGClient()
    result = KGAgent(
        runtime=runtime,
        tools=KnowledgeGraphReadTools(client, search=_FakeSearch()),
    ).generate(
        brand="atlas",
        query_text="连衣裙",
        trigger_source="recall_anomaly",
        evidence={},
    )

    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert result.plan["core_tags"] == ["连衣裙"]
    assert client.calls[0]["params"]["name"] == "连衣裙"


def test_agent_reports_model_and_json_failures():
    class _FailRuntime:
        def invoke(self, _messages, **_kwargs):
            raise TimeoutError("model timeout")

    model_failed = KGAgent(
        runtime=_FailRuntime(),
        tools=KnowledgeGraphReadTools(_FakeKGClient(), search=_FakeSearch()),
    ).generate(
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={},
    )
    assert model_failed.error_code == "KG_AGENT_MODEL_FAILED"

    invalid_json = KGAgent(
        runtime=_FakeRuntime(["not-json"]),
        tools=KnowledgeGraphReadTools(_FakeKGClient(), search=_FakeSearch()),
    ).generate(
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={},
    )
    assert invalid_json.error_code == "KG_AGENT_INVALID_JSON"


def test_read_tools_fail_closed_on_invalid_graph_response():
    client = _FakeKGClient()
    client.responses["query_tag"] = []
    with pytest.raises(KGAgentError, match="KG_GRAPH_READ_INVALID"):
        KnowledgeGraphReadTools(client).query_tag("atlas", "裙子")


def test_kg_agent_is_available_from_query_governance_package():
    import src.query_governance as query_governance

    assert query_governance.KGAgent is KGAgent
    assert query_governance.KGPlanExecutionGuard is KGPlanExecutionGuard


@pytest.mark.parametrize(
    "plan_json,error_message",
    [
        ('{"actions":[1]}', "must be an object"),
        ('{"actions":[{"actionType":"add_tag","params":{}}]}', "requires params"),
        (
            '{"actions":[{"actionType":"add_tag","params":{},"reason":"x",'
            '"riskLevel":"critical"}]}',
            "invalid risk",
        ),
    ],
)
def test_agent_rejects_malformed_action_details(plan_json, error_message):
    result = KGAgent(
        runtime=_FakeRuntime(['{"core_tags":["裙子"]}', plan_json]),
        tools=KnowledgeGraphReadTools(_FakeKGClient()),
    ).generate(
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={},
    )
    assert result.status is KGAgentStatus.ERROR
    assert result.error_code == "KG_AGENT_INVALID_PLAN"
    assert error_message in result.error_message


def test_worker_persists_plan_to_async_task_and_new_action_plan_table(task_fixture):
    factory, async_service, async_task_id = task_fixture
    runtime = _FakeRuntime(
        ['{"core_tags":["裙子"]}', '{"reason":"补齐标签","actions":[]}']
    )
    agent = KGAgent(runtime=runtime, tools=KnowledgeGraphReadTools(_FakeKGClient(), search=_FakeSearch()))
    action_repo = QueryGovernanceActionPlanRepository(factory)
    worker = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=agent,
        action_plan_repository=action_repo,
    )

    result = worker.process(async_task_id)

    assert result.status is KGAgentStatus.PLAN_GENERATED
    record = async_service.get(async_task_id)
    assert record.status.value == "plan_generated"
    assert record.plan["actions"] == []
    with factory() as session:
        rows = session.query(QueryGovernanceActionPlan).all()
        assert len(rows) == 1
        assert rows[0].node_key == "query_governance.kg_agent"
        assert json.loads(rows[0].response_json)["trigger_source"] == "recall_anomaly"

    repeated = worker.process(async_task_id)
    assert repeated.status is KGAgentStatus.PLAN_GENERATED
    with factory() as session:
        assert session.query(QueryGovernanceActionPlan).count() == 1


def test_action_plan_persistence_compacts_large_tool_payloads(task_fixture):
    factory, async_service, async_task_id = task_fixture
    task = async_service.get(async_task_id)
    assert task is not None
    oversized_products = [
        {
            "product_id": f"P{index}",
            "product_name": "商品" * 200,
            "childTags": [{"name": "子标签" * 100}] * 20,
        }
        for index in range(40)
    ]
    raw_context = {
        "tags": {"裙子": {"data": [{"id": "tag-1", "name": "裙子", "childTags": oversized_products}]}},
        "relationships": {"裙子": {"data": [{"id": "rel-1", "raw": oversized_products}]}},
        "searches": {
            "裙子": {
                "result_count": 40,
                "products": oversized_products,
                "raw": {"products": oversized_products},
            }
        },
    }
    plan = {
        "brand": "atlas",
        "query_text": "裙子",
        "trigger_source": "recall_anomaly",
        "core_tags": ["裙子"],
        "decision": "adjust",
        "reason": "补齐关系",
        "actions": [{
            "actionType": "add_relationship",
            "params": {"tagName": "裙子", "responseTagName": "连衣裙"},
            "reason": "关系缺失",
        }],
        "evidence": {"products": oversized_products},
        "graph_context": raw_context,
        "tool_evidence": raw_context,
    }

    plan_id = QueryGovernanceActionPlanRepository(factory).create_or_get(task, plan)

    with factory() as session:
        row = session.get(QueryGovernanceActionPlan, plan_id)
        persisted = json.loads(row.response_json)
    assert len(row.response_json.encode("utf-8")) < 65_535
    assert persisted["decision"] == "adjust"
    assert persisted["actions"][0]["params"]["responseTagName"] == "连衣裙"
    assert persisted["graph_context_digest"]
    assert persisted["tool_evidence"]["searches"]["裙子"] == {"result_count": 40}
    serialized = json.dumps(persisted, ensure_ascii=False)
    assert "childTags" not in serialized
    assert '"raw"' not in serialized
    assert '"products"' not in serialized


def test_worker_records_safe_message_when_plan_persistence_fails(task_fixture):
    _factory, async_service, async_task_id = task_fixture

    class GeneratedPlanAgent:
        def generate(self, **_kwargs):
            return KGAgentResult(
                status=KGAgentStatus.PLAN_GENERATED,
                plan={"decision": "adjust", "reason": "补齐关系", "actions": []},
            )

    class BrokenActionPlanRepository:
        def create_or_get(self, _task, _plan):
            raise RuntimeError(
                "(pymysql.err.DataError) (1406, Data too long for column "
                "'response_json') INSERT INTO query_governance_action_plan"
            )

    result = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=GeneratedPlanAgent(),
        action_plan_repository=BrokenActionPlanRepository(),
    ).process(async_task_id)

    record = async_service.get(async_task_id)
    assert result.status is KGAgentStatus.ERROR
    assert result.error_code == "KG_AGENT_PERSIST_FAILED"
    assert result.error_message == "KG Agent 方案保存失败"
    assert record.status.value == "error"
    assert record.error_message == "KG Agent 方案保存失败"
    assert "pymysql" not in record.error_message


def test_worker_persists_compact_plan_to_async_task(task_fixture):
    _factory, async_service, async_task_id = task_fixture
    oversized_products = [{"product_id": "P1", "childTags": ["子标签"] * 100}]

    class GeneratedPlanAgent:
        def generate(self, **_kwargs):
            return KGAgentResult(
                status=KGAgentStatus.PLAN_GENERATED,
                plan={
                    "brand": "atlas",
                    "query_text": "裙子",
                    "trigger_source": "recall_anomaly",
                    "core_tags": ["裙子"],
                    "decision": "no_change",
                    "reason": "证据完整",
                    "actions": [],
                    "graph_context": {"tags": {"裙子": {"data": oversized_products}}},
                    "tool_evidence": {"searches": {"裙子": {"result_count": 2, "products": oversized_products}}},
                },
            )

    result = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=GeneratedPlanAgent(),
        action_plan_repository=QueryGovernanceActionPlanRepository(_factory),
    ).process(async_task_id)

    record = async_service.get(async_task_id)
    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert record.plan["tool_evidence"]["searches"]["裙子"] == {"result_count": 2}
    assert "childTags" not in json.dumps(record.plan, ensure_ascii=False)


def test_worker_persists_agent_error_without_action_plan(task_fixture):
    factory, async_service, async_task_id = task_fixture
    worker = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=KGAgent(
            runtime=_FakeRuntime(["not-json"]),
            tools=KnowledgeGraphReadTools(_FakeKGClient(), search=_FakeSearch()),
        ),
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
    )

    result = worker.process(async_task_id)

    assert result.status is KGAgentStatus.ERROR
    assert result.error_code == "KG_AGENT_INVALID_JSON"
    record = async_service.get(async_task_id)
    assert record.status.value == "error"
    assert record.error_code == "KG_AGENT_INVALID_JSON"
    with factory() as session:
        assert session.query(QueryGovernanceActionPlan).count() == 0


def test_worker_processes_pending_tasks_in_submission_order(task_fixture):
    factory, async_service, _async_task_id = task_fixture
    second = async_service.enqueue(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙装",
        trigger_source="recall_anomaly",
        evidence={"result_count": 0},
        idempotency_key="kg:batch-1:atlas:裙装:recall",
    )
    assert second.created is True

    class _RecordingAgent:
        def __init__(self):
            self.queries = []

        def generate(self, *, brand, query_text, trigger_source, evidence):
            self.queries.append(query_text)
            return KGAgentResult(
                status=KGAgentStatus.PLAN_GENERATED,
                plan={
                    "brand": brand,
                    "query_text": query_text,
                    "trigger_source": trigger_source,
                    "evidence": dict(evidence or {}),
                    "actions": [],
                },
            )

    recorder = _RecordingAgent()
    worker = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=recorder,
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
    )
    results = worker.process_pending(limit=2)

    assert len(results) == 2
    assert recorder.queries == ["裙子", "裙装"]


def test_worker_uses_independent_root_cause_agent_before_generating_kg_plan(task_fixture):
    factory, async_service, _ = task_fixture
    submitted = async_service.enqueue(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="relevance_root_cause",
        evidence={
            "blacklist_candidates": [{"product_id": "P1", "reason": "错误召回"}],
        },
        idempotency_key="relevance-root-cause:batch-1:atlas:裙子",
    )

    class RootAgent:
        def analyze(self, **kwargs):
            return {
                "query": "裙子",
                "queryTags": ["裙子"],
                "kgContext": {},
                "errorProductIds": ["P1"],
                "causeGroups": [{
                    "groupId": "kg_issue:wide",
                    "rootCause": "kg_issue",
                    "productIds": ["P1"],
                    "conclusion": "关系过宽",
                    "actions": [],
                }],
            }

    class KGPlanAgent:
        def generate(self, **kwargs):
            assert kwargs["evidence"]["causeGroups"][0]["rootCause"] == "kg_issue"
            return KGAgentResult(
                status=KGAgentStatus.PLAN_GENERATED,
                plan={"reason": "删除错误关系", "actions": []},
            )

    worker = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=KGPlanAgent(),
        relevance_root_cause_agent=RootAgent(),
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
    )
    result = worker.process(submitted.task_id)

    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert result.plan["causeGroups"][0]["productIds"] == ["P1"]
    with factory() as session:
        assert session.query(QueryGovernanceActionPlan).count() == 0


def test_worker_reports_root_cause_analysis_failure_separately_from_persistence(task_fixture):
    _factory, async_service, submitted = task_fixture
    submitted = async_service.enqueue(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="relevance_root_cause",
        evidence={"blacklist_candidates": [{"product_id": "P1", "reason": "错误召回"}]},
        idempotency_key="relevance-root-cause:analysis-failure",
    )

    class BrokenRootAgent:
        def analyze(self, **_kwargs):
            raise ValueError("product details missing for: P1")

    result = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=type("UnusedAgent", (), {})(),
        relevance_root_cause_agent=BrokenRootAgent(),
        action_plan_repository=type("UnusedRepository", (), {})(),
    ).process(submitted.task_id)

    record = async_service.get(submitted.task_id)
    assert result.status is KGAgentStatus.ERROR
    assert result.error_code == "KG_AGENT_ANALYSIS_FAILED"
    assert result.error_message == "相关性治理 Agent 分析失败：product details missing for: P1"
    assert record.error_code == "KG_AGENT_ANALYSIS_FAILED"


def test_worker_retries_transient_root_cause_analysis_failure(task_fixture):
    factory, async_service, _ = task_fixture
    submitted = async_service.enqueue(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="relevance_root_cause",
        evidence={"blacklist_candidates": [{"product_id": "P1", "reason": "错误召回"}]},
        idempotency_key="relevance-root-cause:transient-retry",
    )

    class FlakyRootAgent:
        def __init__(self):
            self.calls = 0

        def analyze(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("model timeout")
            return {
                "queryTags": [],
                "kgContext": {},
                "causeGroups": [{"groupId": "uncertain:one", "rootCause": "uncertain", "productIds": ["P1"], "actions": []}],
            }

    root_agent = FlakyRootAgent()
    result = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=type("UnusedAgent", (), {})(),
        relevance_root_cause_agent=root_agent,
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
    ).process(submitted.task_id)

    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert root_agent.calls == 2


def test_relevance_worker_never_repeats_blacklisting_from_root_cause_actions(task_fixture):
    factory, async_service, _ = task_fixture
    submitted = async_service.enqueue(
        task_id=1, batch_id="batch-1", brand="atlas", query_text="裙子",
        trigger_source="relevance_root_cause",
        evidence={"blacklist_candidates": [{"product_id": "P1", "reason": "不相关"}, {"product_id": "P2", "reason": "不相关"}]},
        idempotency_key="relevance-root-cause:individual-only",
    )

    class RootAgent:
        def analyze(self, **_kwargs):
            return {
                "queryTags": [], "kgContext": {},
                "causeGroups": [
                    {"groupId": "individual:one", "rootCause": "individual", "productIds": ["P1"],
                     "actions": [{"actionType": "product_blacklist", "params": {"product_ids": ["P1"]}}]},
                    {"groupId": "kg_issue:wide", "rootCause": "kg_issue", "productIds": ["P2"],
                     "actions": [{"actionType": "delete_relationship", "params": {"id": "rel-1"}}]},
                ],
            }

    class KGPlanAgent:
        def generate(self, **_kwargs):
            pytest.fail("KG Agent must not run for relevance errors")

    worker = QueryGovernanceKGWorker(
        async_service=async_service, agent=KGPlanAgent(),
        relevance_root_cause_agent=RootAgent(),
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
    )
    result = worker.process(submitted.task_id)

    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert result.plan["causeGroups"][0]["actions"] == []
    assert result.plan["causeGroups"][0]["executable"] is False


def test_conflict_checker_rejects_changed_graph_before_execution():
    client = _FakeKGClient()
    tools = KnowledgeGraphReadTools(client, search=_FakeSearch())
    plan = {
        "brand": "atlas",
        "query_text": "裙子",
        "core_tags": ["裙子"],
        "graph_context": tools.collect_context("atlas", ["裙子"]),
    }
    client.responses["query_tag"] = {"data": [{"id": "tag-2", "name": "裙子"}]}

    with pytest.raises(KGPlanConflictError, match="KG_PLAN_CONFLICT"):
        KGPlanConflictChecker(tools).assert_current(plan)


def test_execution_guard_records_conflict_on_action_plan(task_fixture):
    factory, async_service, async_task_id = task_fixture
    runtime = _FakeRuntime(
        ['{"core_tags":["裙子"]}', '{"reason":"补齐标签","actions":[]}']
    )
    client = _FakeKGClient()
    agent = KGAgent(runtime=runtime, tools=KnowledgeGraphReadTools(client, search=_FakeSearch()))
    action_repo = QueryGovernanceActionPlanRepository(factory)
    worker = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=agent,
        action_plan_repository=action_repo,
    )
    worker.process(async_task_id)
    with factory() as session:
        row = session.query(QueryGovernanceActionPlan).one()
        plan_id = row.id
        plan = json.loads(row.response_json)

    client.responses["query_tag"] = {"data": [{"id": "tag-2", "name": "裙子"}]}
    guard = KGPlanExecutionGuard(
        checker=KGPlanConflictChecker(KnowledgeGraphReadTools(client, search=_FakeSearch())),
        action_plan_repository=action_repo,
    )
    with pytest.raises(KGPlanConflictError, match="KG_PLAN_CONFLICT"):
        guard.assert_current(plan_id, plan, executor_user_id="ops-user")

    with factory() as session:
        row = session.get(QueryGovernanceActionPlan, plan_id)
        assert row.status == "failed"
        assert row.executor_type == "human"
        assert row.executor_user_id == "ops-user"
        assert row.failure_stage == "verification"
        assert row.error_code == "KG_PLAN_CONFLICT"
        assert "knowledge graph changed" in row.error_message


def test_agent_rejects_non_contiguous_candidate_and_searches_related_existing_tag():
    client = _FakeKGClient()
    client.responses["query_tag"] = {
        "data": [
            {"id": "tag-denim", "name": "牛仔"},
            {"id": "tag-jeans", "name": "牛仔裤"},
        ]
    }
    search = _FakeSearch({"白色": 5, "连衣裙": 9, "白连衣裙": 99, "牛仔裤": 18})
    runtime = _FakeRuntime([
        '{"core_tags":["白连衣裙","连衣裙"]}',
        '{"decision":"no_change","reason":"已有有效召回，不需要调整","actions":[]}',
    ])

    result = KGAgent(runtime=runtime, tools=KnowledgeGraphReadTools(client, search=search)).generate(
        brand="atlas",
        query_text="白色连衣裙",
        trigger_source="recall_anomaly",
        evidence={"segmented_tags": ["白色", "连衣裙"]},
    )

    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert result.plan["core_tags"] == ["连衣裙", "白色"]
    assert all(call[1] != "白连衣裙" for call in search.calls)
    assert result.plan["decision"] == "no_change"


def test_missing_search_capability_forces_manual_review_instead_of_no_change():
    runtime = _FakeRuntime([
        '{"core_tags":["裙子"]}',
        '{"decision":"no_change","reason":"看起来无需调整","actions":[]}',
    ])

    result = KGAgent(
        runtime=runtime,
        tools=KnowledgeGraphReadTools(_FakeKGClient()),
    ).generate(
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={},
    )

    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert result.plan["decision"] == "manual_review"
    assert result.plan["actions"] == []
    assert "搜索工具不可用" in result.plan["reason"]


def test_missing_tool_evidence_cannot_produce_an_executable_adjustment():
    runtime = _FakeRuntime([
        '{"core_tags":["裙子"]}',
        '{"decision":"adjust","reason":"补齐关系","actions":[{"actionType":"add_relationship","params":{"tagName":"裙子","responseTagName":"连衣裙"},"reason":"关系缺失"}]}',
    ])

    result = KGAgent(
        runtime=runtime,
        tools=KnowledgeGraphReadTools(_FakeKGClient()),
    ).generate(
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={},
    )

    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert result.plan["decision"] == "manual_review"
    assert result.plan["actions"] == []


def test_kg_plan_executor_claims_executes_verifies_and_is_idempotent(task_fixture):
    factory, async_service, async_task_id = task_fixture
    runtime = _FakeRuntime([
        '{"core_tags":["裙子"]}',
        json.dumps({
            "decision": "adjust",
            "reason": "补齐从属关系",
            "actions": [{
                "actionType": "add_relationship",
                "riskLevel": "medium",
                "params": {"tagName": "裙子", "responseTagName": "连衣裙"},
                "reason": "连衣裙有召回且关系缺失",
            }],
        }, ensure_ascii=False),
    ])
    client = _FakeKGClient()
    client.responses["add_relationship"] = {"code": 1, "data": {"id": "rel-1"}}
    worker = QueryGovernanceKGWorker(
        async_service=async_service,
        agent=KGAgent(runtime=runtime, tools=KnowledgeGraphReadTools(client, search=_FakeSearch())),
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
    )
    worker.process(async_task_id)
    with factory() as session:
        plan_id = session.query(QueryGovernanceActionPlan).one().id

    executor = KGPlanExecutor(
        client=client,
        tools=KnowledgeGraphReadTools(client, search=_FakeSearch()),
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
    )
    first = executor.execute(plan_id, executor_user_id="ops-user")
    second = executor.execute(plan_id, executor_user_id="ops-user")

    assert first["status"] == "verified"
    assert second["status"] == "verified"
    assert len([call for call in client.calls if call["actionType"] == "add_relationship"]) == 1
    with factory() as session:
        row = session.get(QueryGovernanceActionPlan, plan_id)
        payload = json.loads(row.response_json)
        assert row.status == "verified"
        assert row.executor_type == "human"
        assert row.executor_user_id == "ops-user"
        assert payload["plan"]["decision"] == "adjust"
        assert payload["execution"]["steps"][0]["status"] == "verified"


@pytest.mark.parametrize('agent_mode', ['auto', 'human_approval', 'disabled'])
@pytest.mark.parametrize('tool_mode', ['auto', 'human_approval', 'disabled'])
def test_relevance_kg_worker_respects_agent_mode_before_auto_execution(task_fixture, agent_mode, tool_mode):
    expected = max((agent_mode, tool_mode), key=['auto', 'human_approval', 'disabled'].index)
    factory, async_service, _ = task_fixture
    submitted = async_service.enqueue(
        task_id=1, batch_id='batch-1', brand='atlas', query_text='裙子',
        trigger_source='relevance_root_cause', evidence={},
        idempotency_key=f'priority:{agent_mode}:{tool_mode}',
    )
    class RootAgent:
        def analyze(self, **kwargs):
            return {'queryTags': [], 'kgContext': {}, 'causeGroups': [
                {'groupId': 'kg:1', 'rootCause': 'kg_issue', 'productIds': ['P1'],
                 'actions': [{'actionType': 'delete_relationship', 'params': {'id': 'rel-1'}}]},
            ]}
    class Executor:
        def __init__(self): self.calls = []
        def execute(self, *args, **kwargs): self.calls.append((args, kwargs))
    executor = Executor()
    worker = QueryGovernanceKGWorker(
        async_service=async_service, agent=None, relevance_root_cause_agent=RootAgent(),
        agent_execution_modes={'relevance': agent_mode},
        kg_execution_modes={'kg_relation': tool_mode}, kg_action_executor=executor,
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
    )
    result = worker.process(submitted.task_id)
    assert result.plan['causeGroups'][0]['executionMode'] == expected
    assert result.plan['causeGroups'][0]['executable'] is (expected != 'disabled')
    assert len(executor.calls) == (1 if expected == 'auto' else 0)


@pytest.mark.parametrize('tag_mode,relation_mode,expected', [
    ('auto', 'human_approval', 'human_approval'),
    ('human_approval', 'auto', 'human_approval'),
    ('auto', 'disabled', 'disabled'),
    ('disabled', 'auto', 'disabled'),
    ('auto', 'auto', 'auto'),
])
def test_mixed_kg_actions_respect_both_tools(task_fixture, tag_mode, relation_mode, expected):
    factory, async_service, _ = task_fixture
    worker = QueryGovernanceKGWorker(
        async_service=async_service, agent=None,
        agent_execution_modes={'relevance': 'auto'},
        kg_execution_modes={'kg_tag': tag_mode, 'kg_relation': relation_mode},
        action_plan_repository=QueryGovernanceActionPlanRepository(factory),
    )
    assert worker._execution_mode_for_actions([
        {'actionType': 'create_tag'}, {'actionType': 'delete_relationship'},
    ], check_key='relevance') == expected
