from dataclasses import dataclass, field
import sys
from types import ModuleType

import pytest

from src.query_governance.langfuse import (
    LangfuseGenerationCallback,
    QueryGovernanceObservability,
    sanitize_trace_data,
)
from src.query_governance.model_runtime import QueryGovernanceModelRuntime
from src.query_governance.models import NodeResult, QuerySpec
from src.query_governance.ports import ActionGateway, NodeRunner, SearchGateway, StateStore
from src.query_governance.workflow import QueryGovernanceWorkflow
from src.llm.node_config import capture_snapshot


class FakeClient:
    def __init__(self):
        self.trace_seeds = []
        self.flush_calls = 0

    def create_trace_id(self, *, seed=None):
        self.trace_seeds.append(seed)
        return f"trace-{len(self.trace_seeds)}"

    def flush(self):
        self.flush_calls += 1


class FakeHandler:
    def __init__(self, trace_context):
        self.trace_context = trace_context


@pytest.mark.parametrize("batched", [True, False])
def test_langfuse_generation_callback_preserves_chat_message_roles_and_content(batched):
    from langchain_core.messages import HumanMessage, SystemMessage

    class Observation:
        def update(self, **_kwargs):
            pass

        def end(self):
            pass

    class Client:
        def __init__(self):
            self.started = None

        def start_observation(self, **kwargs):
            self.started = kwargs
            return Observation()

    client = Client()
    callback = LangfuseGenerationCallback(
        client=client,
        trace_context={"trace_id": "trace-1"},
    )

    messages = [
        SystemMessage(content="system instructions"),
        HumanMessage(content=[
            {"type": "text", "text": "query and products"},
            {"type": "image_url", "image_url": {"url": "https://example.com/p.jpg"}},
        ]),
    ]
    callback.on_chat_model_start(
        {},
        [messages] if batched else messages,
        run_id="run-1",
        metadata={},
    )

    assert client.started["input"] == [
        {"role": "system", "content": "system instructions"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "query and products"},
                {"type": "image_url", "image_url": {"url": "https://example.com/p.jpg"}},
            ],
        },
    ]


@pytest.mark.parametrize("messages", [
    [],
    [[], []],
    [[{"role": "user", "content": "query one"}], [{"role": "user", "content": "query two"}]],
    [[{"unexpected": "unsupported message"}]],
])
def test_generation_callback_keeps_batch_boundaries_and_survives_conversion_failure(messages):
    recorded = []

    class Client:
        def start_observation(self, **kwargs):
            recorded.append(kwargs)
            return object()

    callback = LangfuseGenerationCallback(client=Client(), trace_context={"trace_id": "trace-1"})
    callback.on_chat_model_start({}, messages, run_id="run-1", metadata={})

    assert recorded[0]["input"] == messages
    assert recorded[0]["as_type"] == "generation"


def test_custom_generation_callback_exports_structured_masked_chat_with_real_sdk():
    import json

    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-custom-callback-offline",
        secret_key="sk-custom-callback-offline",
        base_url="https://langfuse.invalid",
        environment="test",
        mask=sanitize_trace_data,
        span_exporter=exporter,
    )
    image_url = "https://example.com/p.jpg?access_token=must-not-leak&size=small"
    messages = [
        SystemMessage(content="检测相关性"),
        HumanMessage(content=[
            {"type": "text", "text": "裙子"},
            {"type": "image_url", "image_url": {"url": image_url}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,must-not-leak"}},
        ]),
        AIMessage(content="", tool_calls=[{
            "name": "product_detail", "args": {"id": "p1"}, "id": "call-1", "type": "tool_call",
        }]),
        ToolMessage(content="商品详情", tool_call_id="call-1"),
    ]
    try:
        callback = LangfuseGenerationCallback(
            client=client,
            trace_context={"trace_id": client.create_trace_id(seed="custom-callback")},
        )
        response = FakeListChatModel(responses=["相关"]).invoke(
            messages,
            config={"callbacks": [callback], "run_name": "query-governance.relevance"},
        )
        client.flush()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes
        recorded = json.loads(attrs["langfuse.observation.input"])
        assert [message["role"] for message in recorded] == ["system", "user", "assistant", "tool"]
        assert recorded[0]["content"] == "检测相关性"
        assert recorded[1]["content"][0] == {"type": "text", "text": "裙子"}
        assert recorded[1]["content"][1]["image_url"]["url"].startswith("https://example.com/p.jpg?")
        assert "size=small" in recorded[1]["content"][1]["image_url"]["url"]
        assert recorded[1]["content"][2]["image_url"]["url"] == "<redacted:base64>"
        assert "must-not-leak" not in attrs["langfuse.observation.input"]
        assert recorded[2]["tool_calls"][0]["id"] == recorded[3]["tool_call_id"] == "call-1"
        assert attrs["langfuse.observation.type"] == "generation"
        assert json.loads(attrs["langfuse.observation.output"]) == [response.content]
    finally:
        client.shutdown()


def _observability(client=None, handlers=None):
    client = client or FakeClient()
    handlers = handlers if handlers is not None else []

    def handler_factory(*, trace_context):
        handler = FakeHandler(trace_context)
        handlers.append(handler)
        return handler

    return QueryGovernanceObservability(
        client=client,
        handler_factory=handler_factory,
        base_metadata={"environment": "test", "release": "release-1"},
    ), client, handlers


def test_unconfigured_langfuse_is_disabled_without_blocking_or_flush():
    observability = QueryGovernanceObservability.from_environment(
        {"LANGFUSE_TRACING_ENABLED": "true"}
    )

    batch = observability.start_batch(
        "batch-1", [QuerySpec("q1", "atlas", "裙子")]
    )

    assert observability.enabled is False
    assert batch.query_config("q1", "relevance")["callbacks"] == []
    assert observability.error_events
    assert observability.client is None


def test_query_and_batch_configs_share_batch_trace_and_redact_sensitive_metadata():
    observability, client, handlers = _observability()
    specs = [
        QuerySpec("q1", "atlas", "裙子"),
        QuerySpec("q2", "atlas", "外套"),
    ]
    batch = observability.start_batch(
        "batch-1",
        specs,
        metadata={
            "model_config_version": 7,
            "ab_group": "treatment",
            "api_key": "must-not-leak",
        },
    )

    query_config = batch.query_config("q1", "relevance")
    batch_config = batch.batch_config("preference_generation", ["q1", "q2"])

    assert client.trace_seeds == [
        "query-governance:batch-1",
        "query-governance:batch-1:q1",
        "query-governance:batch-1:q2",
    ]
    assert query_config["callbacks"][0] is handlers[1]
    assert query_config["metadata"] == {
        "environment": "test",
        "release": "release-1",
        "batch_id": "batch-1",
        "batch_trace_id": "trace-1",
        "query_id": "q1",
        "brand": "atlas",
        "query": "裙子",
        "query_trace_id": "trace-2",
        "model_config_version": 7,
        "ab_group": "treatment",
        "node": "relevance",
        "langfuse_session_id": "batch-1",
        "langfuse_trace_name": "query-governance",
        "langfuse_tags": ["query-governance"],
    }
    assert batch_config["metadata"]["batch_trace_id"] == "trace-1"
    assert batch_config["metadata"]["query_ids"] == ["q1", "q2"]
    assert batch_config["metadata"]["query_trace_ids"] == {
        "q1": "trace-2",
        "q2": "trace-3",
    }
    assert "api_key" not in query_config["metadata"]
    assert handlers[0].trace_context == {"trace_id": "trace-1"}
    assert handlers[1].trace_context == {"trace_id": "trace-2"}
    assert client.flush_calls == 0


def test_query_trace_context_is_serializable_and_keeps_trace_identity():
    observability, _, _ = _observability()
    batch = observability.start_batch(
        "batch-1", [QuerySpec("q1", "atlas", "裙子")], metadata={"model_config_version": 7}
    )

    context = batch.query_trace_context("q1", "kg_agent", model_config_version=7)

    assert context == {
        "batch_id": "batch-1",
        "batch_trace_id": "trace-1",
        "query_id": "q1",
        "query_trace_id": "trace-2",
        "brand": "atlas",
        "query": "裙子",
        "node": "kg_agent",
        "metadata": {
            "environment": "test",
            "release": "release-1",
            "batch_id": "batch-1",
            "batch_trace_id": "trace-1",
            "query_id": "q1",
            "brand": "atlas",
            "query": "裙子",
            "model_config_version": 7,
            "node": "kg_agent",
            "query_trace_id": "trace-2",
            "langfuse_session_id": "batch-1",
            "langfuse_trace_name": "query-governance",
            "langfuse_tags": ["query-governance"],
        },
    }


def test_experiment_metadata_is_attached_only_to_experiment_query_traces():
    observability, _, _ = _observability()
    batch = observability.start_batch(
        "batch-1",
        [
            QuerySpec(
                "q1",
                "atlas",
                "裙子",
                experiment_id=7,
                group_type="treatment",
            ),
            QuerySpec("q2", "atlas", "外套"),
        ],
    )

    q1 = batch.query_config("q1", "relevance")["metadata"]
    q2 = batch.query_config("q2", "relevance")["metadata"]

    assert q1["experiment_id"] == 7
    assert q1["group_type"] == "treatment"
    assert "experiment_id" not in q2
    assert "group_type" not in q2


def test_sanitize_trace_data_removes_secrets_and_binary_payloads():
    value = sanitize_trace_data(
        {
            "query": "裙子",
            "api_key": "secret",
            "authorization": "Bearer secret",
            "nested": {"password": "secret", "ok": True},
            "blob": b"binary",
            "image": "data:image/jpeg;base64,c2VjcmV0",
            "url": "https://image.example/p.jpg?token=secret&size=large",
            "raw_response": {"large": "external payload"},
            "items": [{"access_token": "secret", "id": "p1"}],
        }
    )

    assert value == {
        "query": "裙子",
        "nested": {"ok": True},
        "blob": "<redacted:bytes>",
        "image": "<redacted:base64>",
        "url": "https://image.example/p.jpg?token=%3Credacted%3E&size=large",
        "items": [{"id": "p1"}],
    }


def test_environment_factory_uses_async_batching_and_global_mask_without_flushing():
    created = []

    def client_factory(**kwargs):
        created.append(kwargs)
        return FakeClient()

    observability = QueryGovernanceObservability.from_environment(
        {
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
            "LANGFUSE_BASE_URL": "https://langfuse.internal",
            "LANGFUSE_FLUSH_AT": "30",
            "LANGFUSE_FLUSH_INTERVAL": "7.5",
            "LANGFUSE_SAMPLE_RATE": "0.25",
            "ENV": "test",
            "RELEASE_SHA": "abc123",
        },
        client_factory=client_factory,
        handler_factory=lambda **kwargs: FakeHandler(kwargs["trace_context"]),
    )

    observability.start_batch(
        "batch-1", [QuerySpec("q1", "atlas", "裙子")]
    )

    assert created[0]["flush_at"] == 30
    assert created[0]["flush_interval"] == 7.5
    assert created[0]["sample_rate"] == 0.25
    assert created[0]["mask"]({"secret_key": "x", "ok": True}) == {"ok": True}
    assert observability.client.flush_calls == 0


def test_environment_factory_binds_public_key_once(monkeypatch):
    langchain_module = ModuleType("langfuse.langchain")

    created_handlers = []

    def callback_handler(**kwargs):
        created_handlers.append(kwargs)
        return FakeHandler(kwargs["trace_context"])

    langchain_module.CallbackHandler = callback_handler
    monkeypatch.setitem(sys.modules, "langfuse.langchain", langchain_module)
    observability = QueryGovernanceObservability.from_environment(
        {
            "LANGFUSE_PUBLIC_KEY": "pk-environment",
            "LANGFUSE_SECRET_KEY": "sk-environment",
            "LANGFUSE_BASE_URL": "https://langfuse.internal",
            "ENV": "test",
        },
        client_factory=lambda **_kwargs: FakeClient(),
    )

    config = observability.start_batch(
        "batch-environment",
        [QuerySpec("q-environment", "atlas", "裙子")],
    ).query_config("q-environment", "relevance")

    assert config["callbacks"]
    assert created_handlers[0]["public_key"] == "pk-environment"


def test_real_langfuse_callback_consumes_runnable_config_and_exports_masked_span(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    monkeypatch.setitem(sys.modules, "langfuse.langchain", None)
    client = Langfuse(
        public_key="pk-query-governance-offline",
        secret_key="sk-query-governance-offline",
        base_url="https://langfuse.invalid",
        environment="test",
        flush_at=100,
        flush_interval=60,
        mask=lambda *, data: sanitize_trace_data(data),
        span_exporter=exporter,
    )
    try:
        observability = QueryGovernanceObservability.from_environment(
            {
                "LANGFUSE_PUBLIC_KEY": "pk-query-governance-offline",
                "LANGFUSE_SECRET_KEY": "sk-query-governance-offline",
                "LANGFUSE_BASE_URL": "https://langfuse.invalid",
                "ENV": "test",
            },
            client_factory=lambda **_kwargs: client,
        )
        batch = observability.start_batch(
            "batch-offline",
            [QuerySpec("q-offline", "atlas", "裙子")],
            metadata={"api_key": "must-not-leak"},
        )
        config = batch.query_config("q-offline", "relevance")
        assert isinstance(config["callbacks"][0], LangfuseGenerationCallback)

        response = FakeListChatModel(responses=["相关"]).invoke(
            "裙子", config=config
        )
        client.flush()

        assert response.content == "相关"
        spans = exporter.get_finished_spans()
        assert spans
        trace_ids = {format(span.context.trace_id, "032x") for span in spans}
        assert batch.query_traces["q-offline"].trace_id in trace_ids
        rendered = repr([span.attributes for span in spans])
        assert "must-not-leak" not in rendered
        assert any(span.name == "query-governance.relevance" for span in spans)
        assert "batch-offline" in rendered
    finally:
        client.shutdown()


def test_multiple_langfuse_clients_keep_callback_project_isolation(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    clients = []
    exporters = []
    monkeypatch.setitem(sys.modules, "langfuse.langchain", None)
    try:
        for index in range(2):
            exporter = InMemorySpanExporter()
            key = f"pk-query-governance-isolation-{index}"
            client = Langfuse(
                public_key=key,
                secret_key=f"sk-query-governance-isolation-{index}",
                base_url="https://langfuse.invalid",
                environment="test",
                flush_at=100,
                flush_interval=60,
                mask=lambda *, data: sanitize_trace_data(data),
                span_exporter=exporter,
            )
            clients.append(client)
            exporters.append(exporter)
            observability = QueryGovernanceObservability.from_environment(
                {
                    "LANGFUSE_PUBLIC_KEY": key,
                    "LANGFUSE_SECRET_KEY": f"sk-query-governance-isolation-{index}",
                    "LANGFUSE_BASE_URL": "https://langfuse.invalid",
                    "ENV": "test",
                },
                client_factory=lambda **_kwargs: client,
            )
            batch = observability.start_batch(
                f"batch-isolation-{index}",
                [QuerySpec(f"q-isolation-{index}", "atlas", "裙子")],
            )
            FakeListChatModel(responses=["相关"]).invoke(
                "裙子",
                config=batch.query_config(
                    f"q-isolation-{index}", "relevance"
                ),
            )
            client.flush()

        for index, exporter in enumerate(exporters):
            spans = exporter.get_finished_spans()
            assert spans
            rendered = repr([span.attributes for span in spans])
            assert f"batch-isolation-{index}" in rendered
            assert f"batch-isolation-{1 - index}" not in rendered
    finally:
        for client in clients:
            client.shutdown()


def test_model_runtime_forwards_langfuse_config_to_primary_and_fallback():
    class Model:
        def __init__(self, response=None, error=None):
            self.response = response
            self.error = error
            self.calls = []

        def invoke(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            if self.error:
                raise self.error
            return self.response

    primary = Model(error=RuntimeError("down"))
    fallback = Model(response={"ok": True})
    snapshot = capture_snapshot(
        node_keys=("query_governance.preference_generation",), version=3
    )
    observability, _, _ = _observability()
    batch = observability.start_batch(
        "batch-1", [QuerySpec("q1", "atlas", "裙子")], metadata={"model_config_version": 3}
    )
    config = batch.query_config("q1", "preference_generation")

    result = QueryGovernanceModelRuntime(
        "query_governance.preference_generation",
        snapshot,
        llm=__import__("src.llm.rd_client", fromlist=["FallbackChatModel"]).FallbackChatModel(
            primary=primary, fallback=fallback
        ),
    ).invoke([{"role": "user", "content": "test"}], config=config)

    assert result.response == {"ok": True}
    assert primary.calls[0][1]["config"] == config
    assert fallback.calls[0][1]["config"] == config


@dataclass
class Search(SearchGateway):
    def mixed(self, brand: str, query: str):
        return [{"product_id": "p1", "rank": 1}]


@dataclass
class Store(StateStore):
    writes: list[tuple[str, str]] = field(default_factory=list)

    def get_s0(self, brand, query):
        return None

    def save_s0_if_absent(self, brand, query, snapshot):
        return snapshot

    def save_step(self, brand, query, node, result):
        self.writes.append((query, node))

    def save_s0_failure(self, brand, query, error_code, error_message):
        pass


class Runner(NodeRunner):
    def __init__(self):
        self.contexts = []

    def run(self, spec, context):
        self.contexts.append(context)
        return NodeResult.executed()


class Actions(ActionGateway):
    def execute_and_verify(self, spec, action, context):
        return NodeResult.executed()


def test_workflow_entry_exposes_query_langfuse_config_to_each_node():
    observability, _, _ = _observability()
    runners = {node: Runner() for node in ("recall", "reorder", "relevance")}
    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=Store(),
        runners=runners,
        action_gateway=Actions(),
        observability=observability,
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            ),
            version=1,
        ),
    )

    workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子"), QuerySpec("q2", "atlas", "外套")],
        batch_id="batch-1",
    )

    for runner in runners.values():
        assert len(runner.contexts) == 2
        for context in runner.contexts:
            assert context["callbacks"]
            assert context["langfuse_config"]["callbacks"] == context["callbacks"]
            assert context["langfuse_config"]["metadata"]["batch_id"] == "batch-1"


def test_downstream_runner_can_forward_workflow_config_to_model_runtime():
    from src.llm.rd_client import FallbackChatModel

    class Model:
        def __init__(self):
            self.calls = []

        def invoke(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return {"ok": True}

    class ModelRunner(NodeRunner):
        def __init__(self, snapshot):
            self.model = Model()
            self.snapshot = snapshot
            self.contexts = []

        def run(self, spec, context):
            self.contexts.append(context)
            QueryGovernanceModelRuntime(
                "query_governance.preference_generation",
                self.snapshot,
                llm=FallbackChatModel(primary=self.model),
            ).invoke(
                [{"role": "user", "content": spec.query}],
                config=context["langfuse_config"],
            )
            return NodeResult.executed()

    snapshot = capture_snapshot(
        node_keys=("query_governance.preference_generation",), version=1
    )
    observability, _, _ = _observability()
    runner = ModelRunner(snapshot)
    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=Store(),
        runners={node: runner for node in ("recall", "reorder", "relevance")},
        action_gateway=Actions(),
        observability=observability,
        model_config_snapshot=snapshot,
    )

    workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子"), QuerySpec("q2", "atlas", "外套")],
        batch_id="batch-1",
    )

    assert len(runner.contexts) == 6
    assert len(runner.model.calls) == 6
    for context, (_, kwargs) in zip(runner.contexts, runner.model.calls):
        assert kwargs["config"] == context["langfuse_config"]
        assert kwargs["config"]["callbacks"]


def test_workflow_exposes_batch_langfuse_config_with_all_query_trace_ids():
    observability, _, _ = _observability()
    runners = {node: Runner() for node in ("recall", "reorder", "relevance")}
    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=Store(),
        runners=runners,
        action_gateway=Actions(),
        observability=observability,
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            ),
            version=1,
        ),
    )

    workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子"), QuerySpec("q2", "atlas", "外套")],
        batch_id="batch-1",
    )

    for runner in runners.values():
        for context in runner.contexts:
            config = context["langfuse_batch_config"]
            assert config["callbacks"]
            assert config["metadata"]["batch_id"] == "batch-1"
            assert config["metadata"]["query_ids"] == ["q1", "q2"]
            assert config["metadata"]["query_trace_ids"] == {
                "q1": "trace-2",
                "q2": "trace-3",
            }
            assert config["metadata"]["model_config_version"] == 1


def test_observability_failure_does_not_change_workflow_result():
    class BrokenObservability:
        def start_batch(self, *_args, **_kwargs):
            raise TimeoutError("langfuse unavailable")

    runners = {node: Runner() for node in ("recall", "reorder", "relevance")}
    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=Store(),
        runners=runners,
        action_gateway=Actions(),
        observability=BrokenObservability(),
    )

    result = workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子")], batch_id="batch-1"
    )

    assert result["q1"].nodes["relevance"].status.value == "executed"
    for runner in runners.values():
        assert runner.contexts[0]["callbacks"] == []


def test_workflow_uses_a_unique_trace_batch_id_for_each_adhoc_run():
    class RecordingObservability:
        def __init__(self):
            self.batch_ids = []

        def start_batch(self, batch_id, specs, **_kwargs):
            self.batch_ids.append(batch_id)
            return QueryGovernanceObservability.disabled().start_batch(batch_id, specs)

    observability = RecordingObservability()
    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=Store(),
        runners={node: Runner() for node in ("recall", "reorder", "relevance")},
        action_gateway=Actions(),
        observability=observability,
    )

    workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])
    workflow.run_batch([QuerySpec("q1", "atlas", "裙子")])

    assert observability.batch_ids[0].startswith("query-governance-adhoc-")
    assert observability.batch_ids[0] != observability.batch_ids[1]
