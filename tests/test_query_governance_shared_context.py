from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import threading
import time
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from src.persistence.models import Base
from src.query_governance.context import (
    ConfigBrandContextSource,
    InMemorySharedContextStore,
    SqlXhsBrandContextSource,
    SharedContextService,
    SqlAlchemySharedContextStore,
)
from src.query_governance.models import (
    NodeResult,
    NodeStatus,
    QueryGovernanceContextSnapshot,
    QueryGovernanceTask,
    QuerySpec,
)
from src.query_governance.workflow import QueryGovernanceWorkflow


DATA_DATE = date(2026, 8, 28)


@dataclass
class Facts:
    payload: dict
    calls: list[tuple[date, list[dict]]] = field(default_factory=list)

    def collect(self, value, weather_cities):
        self.calls.append((value, list(weather_cities)))
        return dict(self.payload)


@dataclass
class Brands:
    calls: list[tuple[str, date]] = field(default_factory=list)

    def collect(self, brand, value):
        self.calls.append((brand, value))
        return {
            "brand_info": {"name": brand.upper()},
            "social_trends": [{"name": f"{brand}-trend"}],
            "warnings": [],
        }


def available_facts(**overrides):
    payload = {
        "date": DATA_DATE.isoformat(),
        "season": "summer",
        "weather": {"status": "available", "cities": []},
        "temperature": "high",
        "festival": {"status": "available", "type": "normal"},
        "warnings": [],
        "external_facts_available": True,
    }
    payload.update(overrides)
    return payload


def test_shared_context_collects_global_facts_once_and_brand_data_once_per_brand():
    facts = Facts(available_facts())
    brands = Brands()
    service = SharedContextService(
        facts=facts,
        brands=brands,
        store=InMemorySharedContextStore(),
        weather_cities=[{"city": "上海", "city_code": 101020100}],
    )

    first = service.prepare(
        batch_id="batch-1", data_date=DATA_DATE, brands=["atlas", "harbor", "atlas"]
    )
    second = service.prepare(
        batch_id="batch-1", data_date=DATA_DATE, brands=["harbor", "atlas"]
    )

    assert len(facts.calls) == 1
    assert brands.calls == [("atlas", DATA_DATE), ("harbor", DATA_DATE)]
    assert first["atlas"]["global_facts"] == first["harbor"]["global_facts"]
    assert second == {"harbor": first["harbor"], "atlas": first["atlas"]}
    assert first["atlas"]["brand_context"]["brand_info"] == {"name": "ATLAS"}


def test_shared_context_keeps_partial_failure_warning_and_allows_reorder():
    warning = {
        "source": "weather",
        "error_code": "FACT_WEATHER_UNAVAILABLE",
        "error_message": "天气事实读取失败",
    }
    service = SharedContextService(
        facts=Facts(
            available_facts(
                weather={"status": "error", "cities": []},
                warnings=[warning],
            )
        ),
        brands=Brands(),
        store=InMemorySharedContextStore(),
    )

    context = service.prepare(
        batch_id="batch-1", data_date=DATA_DATE, brands=["atlas"]
    )["atlas"]

    assert context["reorder_available"] is True
    assert context["warnings"] == [warning]
    assert context["global_facts"]["festival"]["status"] == "available"


def test_shared_context_removes_raw_weather_values_before_reorder_input():
    service = SharedContextService(
        facts=Facts(
            available_facts(
                weather={
                    "status": "available",
                    "cities": [
                        {
                            "city": "上海",
                            "weather": "晴",
                            "weather_state": "clear",
                            "temperature": 31,
                            "temperature_level": "high",
                        }
                    ],
                }
            )
        ),
        brands=Brands(),
        store=InMemorySharedContextStore(),
    )

    facts = service.prepare(
        batch_id="batch-1", data_date=DATA_DATE, brands=["atlas"]
    )["atlas"]["global_facts"]

    assert facts["weather"] == {"status": "available", "states": ["clear"]}
    assert facts["temperature"] == "high"
    assert facts["weather_cities"] == [{
        "city": "上海", "weather": "晴", "weather_state": "clear", "temperature": 31
    }]


def test_config_brand_context_only_keeps_brand_info_and_active_social_trends():
    source = ConfigBrandContextSource(
        config_provider=lambda _brand: {
            "top_query_world_knowledge_config": {
                "brand_info": {"market": "CN"},
                "social_trends": [
                    {"name": "active", "enabled": True},
                    {"name": "disabled", "enabled": False},
                    {"name": "expired", "end_date": "2026-08-27"},
                    {"name": "future", "start_date": "2026-08-29"},
                ],
                "tag_rules": [{"tags": ["不应读取"]}],
                "default_after_bucket": "top",
            }
        }
    )

    context = source.collect("atlas", DATA_DATE)

    assert context == {
        "brand_info": {"market": "CN"},
        "social_trends": [{"name": "active", "enabled": True}],
        "warnings": [],
    }


def test_xhs_brand_context_reads_all_titles_and_descriptions_for_brand_date():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE xhs_note_snapshots (crawled_date TEXT, brand_name TEXT, note_title TEXT, note_description TEXT, overall_score REAL, engagement_count INTEGER)"))
        connection.execute(text("INSERT INTO xhs_note_snapshots VALUES ('2026-08-28','ATLAS','早秋穿搭','轻薄针织',0.9,10),('2026-08-27','ATLAS','旧帖','忽略',1,99),('2026-08-28','Harbor','其他','其他',1,1)"))
    source = SqlXhsBrandContextSource(engine, base_source=ConfigBrandContextSource(lambda _brand: {"top_query_world_knowledge_config": {"brand_info": {"market": "CN"}}}))
    result = source.collect("atlas", DATA_DATE)
    assert result["brand_info"] == {"market": "CN"}
    assert result["social_trends"] == [{"title": "早秋穿搭", "description": "轻薄针织"}]
    engine.dispose()


def test_sql_store_persists_and_reads_context_by_batch_brand_and_query():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with Session(engine) as session:
            session.add(
                QueryGovernanceTask(
                    batch_id="batch-1",
                    brand="atlas",
                    data_date=DATA_DATE,
                    query_text="裙子",
                )
            )
            session.commit()
        store = SqlAlchemySharedContextStore(factory)
        service = SharedContextService(
            facts=Facts(available_facts()),
            brands=Brands(),
            store=store,
        )

        prepared = service.prepare(
            batch_id="batch-1", data_date=DATA_DATE, brands=["atlas"]
        )["atlas"]
        stored = store.read_for_query("batch-1", "atlas", "裙子")

        assert stored == prepared
        assert "tag_rules" not in str(stored)
        assert "baseline" not in str(stored)
    finally:
        engine.dispose()


def test_sql_store_replaces_legacy_failed_snapshot_with_successful_retry():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with Session(engine) as session:
            session.add(
                QueryGovernanceContextSnapshot(
                    batch_id="shared-facts:2026-08-28",
                    brand="__global__",
                    context_type="global_facts",
                    payload='{"external_facts_available":false}',
                )
            )
            session.commit()

        facts = Facts(available_facts())
        store = SqlAlchemySharedContextStore(factory)
        context = SharedContextService(
            facts=facts, brands=Brands(), store=store
        ).prepare(batch_id="batch-1", data_date=DATA_DATE, brands=["atlas"])["atlas"]

        assert context["global_facts"]["external_facts_available"] is True
        assert store.get_global_facts(DATA_DATE) == context["global_facts"]
        assert len(facts.calls) == 1
    finally:
        engine.dispose()


def test_existing_persisted_context_is_authoritative_and_not_overwritten():
    store = InMemorySharedContextStore()
    first_facts = Facts(available_facts(temperature="normal"))
    service = SharedContextService(
        facts=first_facts,
        brands=Brands(),
        store=store,
    )
    first = service.prepare(
        batch_id="batch-1", data_date=DATA_DATE, brands=["atlas"]
    )["atlas"]
    second_facts = Facts(available_facts(temperature="high"))
    retried = SharedContextService(
        facts=second_facts,
        brands=Brands(),
        store=store,
    ).prepare(batch_id="batch-1", data_date=DATA_DATE, brands=["atlas"])["atlas"]

    assert first["global_facts"]["temperature"] == "normal"
    assert retried == first
    assert second_facts.calls == []


def test_fact_provider_failure_is_not_persisted_and_retries_same_date(caplog):
    class BrokenFacts:
        def collect(self, _value, _cities):
            raise RuntimeError("supplier credential leaked")

    store = InMemorySharedContextStore()
    context = SharedContextService(
        facts=BrokenFacts(), brands=Brands(), store=store
    ).prepare(batch_id="batch-1", data_date=DATA_DATE, brands=["atlas"])["atlas"]

    assert context["reorder_available"] is False
    assert context["warnings"][0]["error_code"] == "FACTS_UNAVAILABLE"
    assert "credential" not in str(context)
    assert "shared world facts collection failed" in caplog.text
    assert "credential" not in caplog.text
    assert store.get_global_facts(DATA_DATE) is None

    recovered_facts = Facts(available_facts())
    recovered = SharedContextService(
        facts=recovered_facts, brands=Brands(), store=store
    ).prepare(batch_id="batch-2", data_date=DATA_DATE, brands=["atlas"])["atlas"]

    assert recovered["global_facts"]["external_facts_available"] is True
    assert len(recovered_facts.calls) == 1
    assert store.get_global_facts(DATA_DATE) == recovered["global_facts"]


class Search:
    def mixed(self, _brand, query):
        return [{"product_id": f"{query}-1", "rank": 1}]


class State:
    def __init__(self):
        self.snapshots = {}
        self.steps = []

    def get_s0(self, brand, query):
        return self.snapshots.get((brand, query))

    def save_s0_if_absent(self, brand, query, snapshot):
        return self.snapshots.setdefault((brand, query), snapshot)

    def save_s0_failure(self, *_args):
        return None

    def save_step(self, brand, query, node, result):
        self.steps.append((brand, query, node, result))


class Runner:
    def __init__(self, node, contexts):
        self.node = node
        self.contexts = contexts

    def run(self, spec, context):
        self.contexts.setdefault(self.node, []).append((spec.query, dict(context)))
        return NodeResult.executed({"node": self.node})


class Actions:
    def execute_and_verify(self, _spec, _action, _context):
        return NodeResult.executed()


def workflow_with_facts(facts_payload):
    contexts = {}
    runners = {
        node: Runner(node, contexts) for node in ("recall", "reorder", "relevance")
    }
    shared_context = SharedContextService(
        facts=Facts(facts_payload),
        brands=Brands(),
        store=InMemorySharedContextStore(),
    )
    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=State(),
        runners=runners,
        action_gateway=Actions(),
        shared_context=shared_context,
    )
    return workflow, contexts


def test_workflow_logs_shared_context_exception(caplog):
    class BrokenSharedContext:
        def prepare(self, **_kwargs):
            raise RuntimeError("snapshot write failed")

    contexts = {}
    runners = {
        node: Runner(node, contexts) for node in ("recall", "reorder", "relevance")
    }
    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=State(),
        runners=runners,
        action_gateway=Actions(),
        shared_context=BrokenSharedContext(),
    )

    result = workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")],
        batch_id="batch-1",
        data_date=DATA_DATE,
    )["q1"]

    assert result.nodes["reorder"].error_code == "SHARED_CONTEXT_FAILED"
    assert "shared context preparation failed" in caplog.text
    assert "batch-1" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "snapshot write failed" not in caplog.text


def test_workflow_passes_shared_context_to_reorder():
    workflow, contexts = workflow_with_facts(available_facts())

    result = workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")],
        batch_id="batch-1",
        data_date=DATA_DATE,
    )

    assert result["q1"].nodes["reorder"].status is NodeStatus.EXECUTED
    assert contexts["reorder"][0][1]["shared_context"]["brand_context"] == {
        "brand_info": {"name": "ATLAS"},
        "social_trends": [{"name": "atlas-trend"}],
        "warnings": [],
    }


def test_partial_external_facts_still_runs_reorder_with_warning():
    warning = {
        "source": "weather",
        "error_code": "FACT_WEATHER_UNAVAILABLE",
        "error_message": "天气事实读取失败",
    }
    workflow, contexts = workflow_with_facts(
        available_facts(
            weather={"status": "error", "cities": []},
            festival={"status": "available", "type": "normal"},
            warnings=[warning],
            external_facts_available=True,
        )
    )

    result = workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")],
        batch_id="batch-1",
        data_date=DATA_DATE,
    )["q1"]

    assert result.nodes["reorder"].status is NodeStatus.EXECUTED
    assert contexts["reorder"][0][1]["shared_context"]["warnings"] == [warning]


def test_all_external_facts_unavailable_runs_reorder_with_warning_and_continues_relevance():
    workflow, contexts = workflow_with_facts(
        available_facts(
            weather={"status": "error", "cities": []},
            festival={"status": "error"},
            warnings=[{"source": "weather"}, {"source": "festival"}],
            external_facts_available=False,
        )
    )

    result = workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")],
        batch_id="batch-1",
        data_date=DATA_DATE,
    )["q1"]

    assert result.nodes["reorder"].status is NodeStatus.EXECUTED
    assert result.nodes["reorder"].error_code is None
    assert contexts["reorder"][0][1]["shared_context"]["global_facts"][
        "external_facts_available"
    ] is False
    assert contexts["reorder"][0][1]["shared_context"]["warnings"] == [
        {"source": "weather"},
        {"source": "festival"},
    ]
    assert [query for query, _ in contexts["relevance"]] == ["裙子"]


def test_preference_provider_still_invokes_agent_when_all_facts_are_missing():
    from src.query_governance.composition import QueryGovernancePreferenceProvider

    calls = []

    class Evidence:
        def collect(self, **_kwargs):
            return SimpleNamespace(products=[])

    class PreferenceRunner:
        def run(self, inputs, **_kwargs):
            calls.extend(inputs)
            return {inputs[0].spec.query_id: NodeResult.executed({"should_reorder": True})}

    provider = QueryGovernancePreferenceProvider(
        evidence=Evidence(), runner=PreferenceRunner()
    )
    result = provider.run(
        specs=[QuerySpec("q1", "atlas", "裙子")],
        shared_contexts={
            "atlas": {
                "reorder_available": False,
                "global_facts": {
                    "weather": {"status": "error", "states": []},
                    "festival": {"status": "error"},
                    "external_facts_available": False,
                },
                "brand_context": {"brand_info": {"name": "ATLAS"}},
                "warnings": [{"source": "facts", "error_code": "FACTS_UNAVAILABLE"}],
            }
        },
        data_date=DATA_DATE,
        batch_id="batch-1",
    )

    assert result["q1"].status is NodeStatus.EXECUTED
    assert len(calls) == 1


def test_preference_provider_collects_behavior_evidence_concurrently():
    from src.query_governance.composition import QueryGovernancePreferenceProvider

    class Evidence:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self._lock = threading.Lock()

        def collect(self, **_kwargs):
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.03)
                return SimpleNamespace(products=[])
            finally:
                with self._lock:
                    self.active -= 1

    class PreferenceRunner:
        class Config:
            concurrency = 2

        config = Config()

        def run(self, inputs, **_kwargs):
            return {
                item.spec.query_id: NodeResult.executed({"should_reorder": True})
                for item in inputs
            }

    evidence = Evidence()
    provider = QueryGovernancePreferenceProvider(
        evidence=evidence, runner=PreferenceRunner()
    )
    specs = [QuerySpec(f"q{i}", "atlas", f"query-{i}") for i in range(4)]
    started = time.perf_counter()
    result = provider.run(
        specs=specs,
        shared_contexts={"atlas": {"global_facts": {}, "brand_context": {}}},
        data_date=DATA_DATE,
        batch_id="batch-evidence-parallel",
    )

    assert set(result) == {f"q{i}" for i in range(4)}
    assert evidence.max_active == 2
    assert time.perf_counter() - started < 0.11
