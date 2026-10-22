from __future__ import annotations

import json

from src.assistant.service import PiAgentAssistantService
from src.llm.agents import NoResultQueryIntentAgent, ReportAgent
from src.query_governance.langfuse import QueryGovernanceObservability
from src.query_governance.manual_relevance import ManualRelevanceCheckService
from src.query_governance.models import QuerySpec
from src.query_governance.product_adapter import AdaptedProduct


class _FakeClient:
    def __init__(self):
        self.trace_ids = []

    def create_trace_id(self, *, seed):
        self.trace_ids.append(seed)
        return f"trace-{len(self.trace_ids)}"


class _FakeHandler:
    def __init__(self, trace_context):
        self.trace_context = trace_context


def _observability():
    client = _FakeClient()
    handlers = []

    def handler_factory(*, trace_context):
        handler = _FakeHandler(trace_context)
        handlers.append(handler)
        return handler

    return (
        QueryGovernanceObservability(
            client=client,
            handler_factory=handler_factory,
            base_metadata={"environment": "test"},
        ),
        client,
        handlers,
    )


def test_batch_trace_exposes_batch_callbacks_for_batch_model_nodes():
    observability, _client, handlers = _observability()
    batch = observability.start_batch(
        "batch-1", [QuerySpec("q1", "atlas", "裙子")]
    )

    assert batch.callbacks("preference_generation") == [handlers[0]]


def test_no_result_agent_forwards_langfuse_config_to_model():
    agent = NoResultQueryIntentAgent()
    captured = {}

    class FakeModel:
        def invoke(self, messages, **kwargs):
            captured.update(kwargs)
            return type("Response", (), {"content": '{"decision":"process"}'})()

    agent.llm = FakeModel()
    agent.langfuse_config = {
        "callbacks": ["handler"],
        "metadata": {"node": "no_result.intent"},
        "run_name": "no-result.intent",
    }
    agent._invoke([{"role": "user", "content": "裙子"}])

    assert captured["config"] == agent.langfuse_config


def test_manual_relevance_forwards_query_trace_config(monkeypatch):
    observability, _client, _handlers = _observability()
    captured = {}

    class FakeRepository:
        pass

    class FakeGateway:
        pass

    class FakeRuntime:
        def invoke(self, messages, **kwargs):
            captured.update(kwargs)
            return type(
                "Invocation",
                (),
                {"response": type("Response", (), {"content": '{"decisions": [{"isRelevant": true, "reason": "匹配"}]}'})()},
            )()

    class FakeCache:
        def prepare_products(self, products, *, brand):
            return []

    monkeypatch.setattr("src.query_governance.manual_relevance.build_relevance_messages", lambda *args, **kwargs: [])

    service = ManualRelevanceCheckService(
        repository=FakeRepository(),
        gateway=FakeGateway(),
        runtime_factory=lambda _brand: FakeRuntime(),
        image_cache=FakeCache(),
        batch_size=10,
        guidance_reader=lambda _brand, _query: "",
        observability=observability,
    )
    product = AdaptedProduct(
        brand="atlas",
        rank=1,
        product_id="p1",
        sku_id="s1",
        spu_id=None,
        product_name="衬衫",
        image_url="https://example.com/p.jpg",
        original_price=100,
        discount_price=80,
    )
    products = [{"sku": "s1", "status": "pending", "isRelevant": None, "reason": None}]
    service._evaluate_chunk(
        brand="atlas",
        query_index=0,
        query="衬衫",
        guidance="",
        chunk=[(0, product)],
        products=products,
        runtime_provider=lambda: FakeRuntime(),
        trace_seed="check-1",
    )

    assert captured["callbacks"]
    assert captured["metadata"]["node"] == "query_governance.relevance_multimodal"


def test_assistant_wraps_external_runtime_call_in_langfuse_agent_observation():
    observability, _client, _handlers = _observability()
    events = [
        json.dumps({"type": "delta", "content": "已完成"}),
        json.dumps({"type": "agent_end"}),
    ]
    observations = []

    def start_agent_observation(**kwargs):
        observations.append(["start", kwargs])
        return {"name": kwargs["name"]}

    def finish_agent_observation(observation, **kwargs):
        observations.append(["finish", observation, kwargs])

    observability.start_agent_observation = start_agent_observation
    observability.finish_agent_observation = finish_agent_observation
    service = PiAgentAssistantService(
        model_name="test-model",
        observability=observability,
        runtime_transport=lambda _url, _payload, _timeout: iter(events),
    )

    result = service.chat("汇总今天", 1, "admin", ["atlas"], {}, "session-1")

    assert result["message"] == "已完成"
    assert observations[0][0] == "start"
    assert observations[-1][0] == "finish"


def test_report_agent_forwards_langfuse_config(monkeypatch):
    monkeypatch.setattr("src.llm.agents.ChatOpenAI", lambda **_kwargs: object())
    agent = ReportAgent()
    captured = {}

    class FakeModel:
        def invoke(self, messages, **kwargs):
            captured.update(kwargs)
            return type("Response", (), {"content": "报告"})()

    agent.llm = FakeModel()
    agent.langfuse_config = {
        "callbacks": ["handler"],
        "metadata": {"node": "report.daily"},
        "run_name": "report.daily",
    }

    assert agent.generate({"atlas": {}}) == "报告"
    assert captured["config"] == agent.langfuse_config
