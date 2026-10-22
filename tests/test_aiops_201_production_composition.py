from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from src.query_governance.models import NodeResult, QuerySpec


class _Operational:
    def search_products_with_total(self, brand, query, *, page_size=16, page_num=1):
        return {
            "success": True,
            "products": [{"productId": f"{brand}-{query}-1"}],
            "total_count": 9,
            "result_complete": False,
        }


class _Runtime:
    def invoke(self, messages, **kwargs):  # pragma: no cover - construction only
        raise AssertionError("production composition test must not call a model")


def _runtime_factory(node_key, snapshot, **kwargs):
    return _Runtime()


def test_production_factory_builds_all_mainline_boundaries(monkeypatch):
    from src.query_governance.composition import (
        OperationalSearchGateway,
        build_query_governance_workflow,
    )
    from src.query_governance.action_execution import RoutingActionGateway
    from src.query_governance.checkpoint import SqlAlchemyCheckpointStore
    from src.query_governance.workflow import QueryGovernanceWorkflow

    snapshot = SimpleNamespace(version=17, nodes={})
    workflow = build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=_Operational(),
        model_config_snapshot=snapshot,
        model_runtime_factory=_runtime_factory,
        behavior_engine=create_engine("sqlite:///:memory:"),
    )

    assert isinstance(workflow, QueryGovernanceWorkflow)
    assert isinstance(workflow.search, OperationalSearchGateway)
    assert isinstance(workflow.checkpoint, SqlAlchemyCheckpointStore)
    assert set(workflow.runners) == {"recall", "reorder", "relevance"}
    assert isinstance(workflow.action_gateway, RoutingActionGateway)
    assert set(workflow.action_gateway.gateways) == {
        "query_reorder",
        "product_blacklist",
    }
    assert workflow._fixed_model_config_snapshot is snapshot
    assert workflow.preference_provider is not None


def test_production_factory_uses_brand_product_check_limit_for_mixed(monkeypatch):
    import src.query_governance.composition as composition

    from src.query_governance.composition import build_query_governance_workflow

    monkeypatch.setattr(
        composition,
        "get_brand_business_config",
        lambda brand: {"top_query_check_product_limit": 7},
    )
    snapshot = SimpleNamespace(version=17, nodes={})

    workflow = build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=_Operational(),
        model_config_snapshot=snapshot,
        model_runtime_factory=_runtime_factory,
        behavior_engine=create_engine("sqlite:///:memory:"),
    )

    assert workflow.search.page_size == 7


def test_production_factory_bounds_summit_query_concurrency():
    from src.query_governance.composition import _query_concurrency_for_brand

    assert _query_concurrency_for_brand("summit", 10) == 2
    assert _query_concurrency_for_brand("summit", 1) == 1
    assert _query_concurrency_for_brand("atlas", 10) == 10


def test_production_factory_wires_image_proxy_to_all_relevance_fetchers(monkeypatch):
    import src.query_governance.composition as composition

    monkeypatch.setenv(
        "QUERY_GOVERNANCE_IMAGE_PROXY_URL",
        "http://proxy.example:8080",
    )
    snapshot = SimpleNamespace(version=17, nodes={})

    workflow = composition.build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=_Operational(),
        model_config_snapshot=snapshot,
        model_runtime_factory=_runtime_factory,
        behavior_engine=create_engine("sqlite:///:memory:"),
    )

    assert workflow.runners["relevance"].image_cache.fetcher.proxy_url == (
        "http://proxy.example:8080"
    )
    assert workflow.relevance_batch_runner.image_cache.fetcher.proxy_url == (
        "http://proxy.example:8080"
    )


def test_manual_relevance_factory_wires_image_proxy(monkeypatch):
    import src.query_governance.composition as composition

    monkeypatch.setenv(
        "QUERY_GOVERNANCE_IMAGE_PROXY_URL",
        "https://image-proxy.example:443",
    )
    monkeypatch.setattr(composition, "_image_storage", lambda: object())

    service = composition.build_manual_relevance_check_service(
        session_factory=lambda: None,
        operational_service=_Operational(),
        model_runtime_factory=_runtime_factory,
    )

    image_cache = service.image_cache("atlas")
    assert image_cache.fetcher.proxy_url == "https://image-proxy.example:443"


def test_production_search_gateway_preserves_mixed_total_with_top_n_products():
    from src.query_governance.composition import OperationalSearchGateway

    response = OperationalSearchGateway(_Operational(), page_size=1).mixed_with_total("atlas", "裙子")

    assert response["total_count"] == 9
    assert len(response["products"]) == 1
    assert response["result_complete"] is False


def test_production_search_gateway_skips_summit_detail_enrichment():
    from src.query_governance.composition import OperationalSearchGateway

    class SummitOperational:
        def __init__(self):
            self.calls = []

        def search_products_with_total(self, brand, query, **kwargs):
            self.calls.append((brand, query, kwargs))
            return {
                "success": True,
                "products": [{"productId": "M1"}],
                "total_count": None,
                "result_complete": False,
            }

    operational = SummitOperational()
    response = OperationalSearchGateway(operational).mixed_with_total("summit", "冲锋衣")

    assert response["products"] == [{"productId": "M1"}]
    assert operational.calls == [
        (
            "summit",
            "冲锋衣",
            {"page_size": 16, "page_num": 1, "enrich_details": False},
        )
    ]


def test_production_factory_uses_configured_preference_generation_timeout(monkeypatch):
    import src.query_governance.composition as composition
    from src.query_governance.composition import build_query_governance_workflow

    calls = []

    def runtime_factory(node_key, snapshot, **kwargs):
        calls.append((node_key, kwargs))
        return _Runtime()

    monkeypatch.setattr(
        composition,
        "get_brand_business_config",
        lambda brand: {
            "query_governance_preference_generation_batch_query_limit": 10,
            "query_governance_preference_generation_timeout_seconds": 1200,
            "query_governance_preference_generation_max_completion_tokens": 131072,
        },
    )
    snapshot = SimpleNamespace(version=17, nodes={})

    build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=_Operational(),
        model_config_snapshot=snapshot,
        model_runtime_factory=runtime_factory,
        behavior_engine=create_engine("sqlite:///:memory:"),
    )

    preference_call = next(
        kwargs for node_key, kwargs in calls
        if node_key == "query_governance.preference_generation"
    )
    assert preference_call["timeout"] == 1200


def test_production_factory_disables_preference_thinking_and_uses_model_default_output_budget(monkeypatch):
    import src.query_governance.composition as composition
    from src.query_governance.composition import build_query_governance_workflow

    calls = []

    def runtime_factory(node_key, snapshot, **kwargs):
        calls.append((node_key, kwargs))
        return _Runtime()

    monkeypatch.setattr(
        composition,
        "get_brand_business_config",
        lambda brand: {
            "query_governance_preference_generation_max_completion_tokens": 131072,
        },
    )
    snapshot = SimpleNamespace(version=17, nodes={})

    build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=_Operational(),
        model_config_snapshot=snapshot,
        model_runtime_factory=runtime_factory,
        behavior_engine=create_engine("sqlite:///:memory:"),
    )

    preference_call = next(
        kwargs for node_key, kwargs in calls
        if node_key == "query_governance.preference_generation"
    )
    assert preference_call["max_completion_tokens"] is None
    assert preference_call["extra_body"] == {"enable_thinking": False}


def test_workflow_exposes_data_date_to_every_node_context():
    from src.llm.node_config import NodeConfigSnapshot
    from src.query_governance.workflow import QueryGovernanceWorkflow

    contexts = []

    class Search:
        def mixed(self, brand, query):
            return [{"productId": "p-1", "productName": "裙子"}]

    class Store:
        def get_s0(self, brand, query):
            return None

        def save_s0_if_absent(self, brand, query, snapshot):
            return snapshot

        def save_s0_failure(self, *args):
            pass

        def save_step(self, brand, query, node, result):
            pass

        def save_model_config_version(self, batch_id, version):
            pass

    class Runner:
        def run(self, spec, context):
            contexts.append(context)
            return NodeResult.executed({"node": spec.query})

    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=Store(),
        runners={node: Runner() for node in ("recall", "reorder", "relevance")},
        action_gateway=SimpleNamespace(),
        model_config_snapshot=NodeConfigSnapshot(
            captured_at="2026-09-01T00:00:00Z", nodes={}, version=None
        ),
    )
    workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子")],
        data_date=dt.date(2026, 9, 1),
    )

    assert contexts
    assert all(context["data_date"] == dt.date(2026, 9, 1) for context in contexts)


def test_workflow_runs_production_preference_provider_before_reorder():
    from src.llm.node_config import NodeConfigSnapshot
    from src.query_governance.workflow import QueryGovernanceWorkflow

    contexts = []

    class Search:
        def mixed(self, brand, query):
            return [{"productId": f"{brand}-{query}"}]

    class Store:
        def get_s0(self, brand, query):
            return None

        def save_s0_if_absent(self, brand, query, snapshot):
            return snapshot

        def save_s0_failure(self, *args):
            pass

        def save_step(self, brand, query, node, result):
            pass

        def save_model_config_version(self, batch_id, version):
            pass

    class Runner:
        def __init__(self, node):
            self.node = node

        def run(self, spec, context):
            contexts.append((self.node, context))
            if self.node == "reorder":
                assert context["preference_result"]["preference_description"] == "轻薄裙装"
            return NodeResult.executed({"node": self.node})

    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return {
            "q1": NodeResult.executed(
                {
                    "should_reorder": True,
                    "preference_description": "轻薄裙装",
                }
            )
        }

    workflow = QueryGovernanceWorkflow(
        search=Search(),
        store=Store(),
        runners={node: Runner(node) for node in ("recall", "reorder", "relevance")},
        action_gateway=SimpleNamespace(),
        preference_provider=provider,
        model_config_snapshot=NodeConfigSnapshot(
            captured_at="2026-09-01T00:00:00Z", nodes={}, version=None
        ),
    )
    workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子")],
        data_date=dt.date(2026, 9, 1),
        batch_id="batch-1",
    )

    assert calls and calls[0]["data_date"] == dt.date(2026, 9, 1)
    assert calls[0]["batch_id"] == "batch-1"
    assert any(node == "reorder" for node, _context in contexts)


def test_main_injects_production_workflow_factory(monkeypatch):
    import main as main_module

    captured = {}
    monkeypatch.setattr(main_module, "get_env", lambda: "test")
    monkeypatch.setattr(main_module, "get_release_info", lambda: {
        "releaseSha": "test-release",
        "artifactManifestSha256": "test-config",
    })

    def fake_start_scheduler(**kwargs):
        captured.update(kwargs)
        kwargs["on_started"]()
        return True

    monkeypatch.setattr(main_module, "start_scheduler", fake_start_scheduler)
    main_module.main()

    assert callable(captured["query_governance_workflow_factory"])


def test_scheduler_mainline_runs_dynamic_selection_and_workflow(monkeypatch):
    from src.query_governance.models import QueryRunResult
    from src.query_governance.scheduler import query_governance_mainline_job

    calls = []

    class SelectionService:
        def select_and_start(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                batch_id="query-governance:atlas:2026-09-01",
                selected=(SimpleNamespace(query="裙子"),),
                candidates=(SimpleNamespace(query="裙子"),),
            ), {
                "q1": QueryRunResult(
                    spec=QuerySpec("q1", "atlas", "裙子"),
                    final_status="completed",
                ),
            }

    class ExperimentService:
        def get_active_experiment_for_brand(self, brand, *, execution_date):
            return None

    class Workflow:
        pass

    monkeypatch.setattr("src.query_governance.scheduler.BRAND_CONFIGS", {"atlas": {"enabled": True}})
    monkeypatch.setattr("src.query_governance.scheduler.is_brand_enabled", lambda _brand: True)
    monkeypatch.setattr(
        "src.query_governance.scheduler.get_brand_business_config",
        lambda _brand: {"top_query_governance_enabled": True},
    )

    result = query_governance_mainline_job(
        target_date=dt.date(2026, 9, 1),
        workflow_factory=lambda brand: Workflow(),
        experiment_service_factory=lambda: ExperimentService(),
        selection_service_factory=lambda brand: SelectionService(),
    )

    assert result["status"] == "success"
    assert result["brands"]["atlas"]["status"] == "completed"
    assert calls and calls[0]["brand"] == "atlas"
    assert calls[0]["data_date"] == dt.date(2026, 9, 1)


def test_scheduler_remediation_job_processes_kg_then_retag_and_verification(monkeypatch):
    from src.query_governance.scheduler import query_governance_remediation_job

    calls = []

    class Worker:
        def process_pending(self, *, limit):
            calls.append(("kg", limit))
            return [object()]

    class Retag:
        def trigger_if_ready(self, brand, data_date):
            calls.append(("retag", brand, data_date))
            return {"status": "verified"}

        def verify_if_due(self, brand, data_date):
            calls.append(("verify", brand, data_date))
            return {"status": "waiting"}

    class Workflow:
        kg_worker = Worker()
        retag_verification = Retag()

    monkeypatch.setattr("src.query_governance.scheduler.BRAND_CONFIGS", {"atlas": {"enabled": True}})

    monkeypatch.setattr("src.query_governance.scheduler.is_brand_enabled", lambda _brand: True)
    monkeypatch.setattr(
        "src.query_governance.scheduler.get_brand_business_config",
        lambda _brand: {"top_query_governance_enabled": True},
    )

    result = query_governance_remediation_job(
        target_date=dt.date(2026, 9, 1),
        workflow_factory=lambda _brand: Workflow(),
    )

    assert result["status"] == "success"
    assert result["brands"]["atlas"]["kgTaskCount"] == 1
    assert calls == [
        ("kg", 100),
        ("retag", "atlas", dt.date(2026, 9, 1)),
        ("verify", "atlas", dt.date(2026, 9, 1)),
    ]


def test_scheduler_materializes_overview_summary_after_completed_or_skipped_batch(monkeypatch):
    from src.query_governance.models import QueryRunResult
    from src.query_governance.scheduler import query_governance_mainline_job

    materialized = []

    class SelectionService:
        def select_and_start(self, **kwargs):
            selected = () if kwargs["brand"] == "harbor" else (SimpleNamespace(query="裙子"),)
            runs = {} if not selected else {
                "q1": QueryRunResult(
                    spec=QuerySpec("q1", kwargs["brand"], "裙子"),
                    final_status="completed",
                ),
            }
            return SimpleNamespace(
                batch_id=kwargs["batch_id"],
                selected=selected,
                candidates=selected,
            ), runs

    monkeypatch.setattr("src.query_governance.scheduler.reload_config", lambda: None)
    monkeypatch.setattr("src.query_governance.scheduler.BRAND_CONFIGS", {"atlas": {}, "harbor": {}})
    monkeypatch.setattr("src.query_governance.scheduler.is_brand_enabled", lambda _brand: True)
    monkeypatch.setattr(
        "src.query_governance.scheduler.get_brand_business_config",
        lambda _brand: {"top_query_governance_enabled": True},
    )

    result = query_governance_mainline_job(
        target_date=dt.date(2026, 9, 1),
        workflow_factory=lambda _brand: object(),
        experiment_service_factory=lambda: SimpleNamespace(
            get_active_experiment_for_brand=lambda *args, **kwargs: None
        ),
        selection_service_factory=lambda _brand: SelectionService(),
        overview_summary_materializer=lambda brand, data_date: materialized.append(
            (brand, data_date)
        ),
    )

    assert result["brands"]["atlas"]["status"] == "completed"
    assert result["brands"]["harbor"]["status"] == "skipped"
    assert materialized == [
        ("atlas", dt.date(2026, 9, 1)),
        ("harbor", dt.date(2026, 9, 1)),
    ]


def test_scheduler_materializes_overview_summary_for_error_batch(monkeypatch):
    from src.query_governance.models import QueryRunResult
    from src.query_governance.scheduler import query_governance_mainline_job

    materialized = []

    class SelectionService:
        def select_and_start(self, **kwargs):
            return SimpleNamespace(
                batch_id=kwargs["batch_id"],
                selected=(SimpleNamespace(query="裙子"),),
                candidates=(SimpleNamespace(query="裙子"),),
            ), {
                "q1": QueryRunResult(
                    spec=QuerySpec("q1", kwargs["brand"], "裙子"),
                    final_status="error",
                ),
            }

    monkeypatch.setattr("src.query_governance.scheduler.reload_config", lambda: None)
    monkeypatch.setattr("src.query_governance.scheduler.BRAND_CONFIGS", {"atlas": {}})
    monkeypatch.setattr("src.query_governance.scheduler.is_brand_enabled", lambda _brand: True)
    monkeypatch.setattr(
        "src.query_governance.scheduler.get_brand_business_config",
        lambda _brand: {"top_query_governance_enabled": True},
    )

    result = query_governance_mainline_job(
        target_date=dt.date(2026, 9, 1),
        workflow_factory=lambda _brand: object(),
        experiment_service_factory=lambda: SimpleNamespace(
            get_active_experiment_for_brand=lambda *args, **kwargs: None
        ),
        selection_service_factory=lambda _brand: SelectionService(),
        overview_summary_materializer=lambda brand, data_date: materialized.append(
            (brand, data_date)
        ),
    )

    assert result["brands"]["atlas"]["status"] == "error"
    assert materialized == [("atlas", dt.date(2026, 9, 1))]


def test_scheduler_summary_materialization_failure_does_not_fail_governance(monkeypatch, caplog):
    from src.query_governance.models import QueryRunResult
    from src.query_governance.scheduler import query_governance_mainline_job

    class SelectionService:
        def select_and_start(self, **kwargs):
            return SimpleNamespace(
                batch_id=kwargs["batch_id"],
                selected=(SimpleNamespace(query="裙子"),),
                candidates=(SimpleNamespace(query="裙子"),),
            ), {
                "q1": QueryRunResult(
                    spec=QuerySpec("q1", kwargs["brand"], "裙子"),
                    final_status="completed",
                ),
            }

    def fail_materialization(_brand, _data_date):
        raise RuntimeError("summary unavailable")

    monkeypatch.setattr("src.query_governance.scheduler.reload_config", lambda: None)
    monkeypatch.setattr("src.query_governance.scheduler.BRAND_CONFIGS", {"atlas": {}})
    monkeypatch.setattr("src.query_governance.scheduler.is_brand_enabled", lambda _brand: True)
    monkeypatch.setattr(
        "src.query_governance.scheduler.get_brand_business_config",
        lambda _brand: {"top_query_governance_enabled": True},
    )

    result = query_governance_mainline_job(
        target_date=dt.date(2026, 9, 1),
        workflow_factory=lambda _brand: object(),
        experiment_service_factory=lambda: SimpleNamespace(
            get_active_experiment_for_brand=lambda *args, **kwargs: None
        ),
        selection_service_factory=lambda _brand: SelectionService(),
        overview_summary_materializer=fail_materialization,
    )

    assert result["status"] == "success"
    assert result["brands"]["atlas"]["status"] == "completed"
    assert "overview summary materialization failed" in caplog.text


def test_scheduler_materializes_metrics_with_injected_provider(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.persistence.models import Base
    from src.query_governance.models import QueryGovernanceCandidate, QueryGovernanceSelectionBatch, QueryMetricSnapshot
    from src.query_governance.scheduler import query_governance_mainline_job
    from src.query_governance.models import QueryRunResult

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        batch = QueryGovernanceSelectionBatch(batch_id="query-governance:atlas:2026-09-01", brand="atlas", data_date=dt.date(2026, 9, 1), config_json='{"history_days": 1}')
        session.add(batch); session.flush()
        session.add(QueryGovernanceCandidate(selection_batch_id=batch.id, batch_id=batch.batch_id, brand="atlas", data_date=batch.data_date, query_id="q", query_key="q", query_text="裙子", matched_rules_json="[]", metrics_json="{}", source_type="test", selection_rank=1, selected=True))
        session.commit()

    class SelectionService:
        def select_and_start(self, **kwargs):
            return SimpleNamespace(batch_id=kwargs["batch_id"], selected=(SimpleNamespace(query="裙子"),), candidates=()), {"q": QueryRunResult(spec=QuerySpec("q", "atlas", "裙子"))}

    class ExperimentService:
        def get_active_experiment_for_brand(self, brand, *, execution_date):
            return None

    monkeypatch.setattr("src.query_governance.scheduler.BRAND_CONFIGS", {"atlas": {"enabled": True}})
    monkeypatch.setattr("src.query_governance.scheduler.is_brand_enabled", lambda _brand: True)
    monkeypatch.setattr("src.query_governance.scheduler.get_brand_business_config", lambda _brand: {"top_query_governance_enabled": True})
    provider = lambda _brand, _query, metric_date: {"search_pv": 10, "search_uv": 5} if metric_date != dt.date(2026, 8, 31) else {"status": "missing"}
    result = query_governance_mainline_job(target_date=dt.date(2026, 9, 1), workflow_factory=lambda _brand: object(), session_factory=Session, experiment_service_factory=ExperimentService, selection_service_factory=lambda _brand: SelectionService(), metric_provider_factory=lambda _brand: provider)
    assert result["brands"]["atlas"]["materializedMetricCount"] == 1
    with Session() as session:
        assert [row.data_date for row in session.query(QueryMetricSnapshot).all()] == [dt.date(2026, 9, 1)]


@pytest.mark.parametrize(
    ("statuses", "expected_brand_status", "expected_batch_status", "expected_errors"),
    [
        (("error", "error"), "error", "error", 2),
        (("completed", "error"), "error", "error", 1),
        (("completed", "completed"), "completed", "success", 0),
    ],
)
def test_scheduler_aggregates_query_failures_into_brand_and_batch_status(
    monkeypatch,
    statuses,
    expected_brand_status,
    expected_batch_status,
    expected_errors,
):
    from src.query_governance.models import QueryRunResult
    from src.query_governance.scheduler import query_governance_mainline_job

    class SelectionService:
        def select_and_start(self, **kwargs):
            runs = {
                f"q{index}": QueryRunResult(
                    spec=QuerySpec(f"q{index}", "atlas", f"query-{index}"),
                    final_status=status,
                )
                for index, status in enumerate(statuses, start=1)
            }
            return SimpleNamespace(
                batch_id="query-governance:atlas:2026-09-01",
                selected=tuple(SimpleNamespace(query=f"query-{i}") for i in range(1, len(statuses) + 1)),
                candidates=tuple(SimpleNamespace(query=f"query-{i}") for i in range(1, len(statuses) + 1)),
            ), runs

    class ExperimentService:
        def get_active_experiment_for_brand(self, brand, *, execution_date):
            return None

    monkeypatch.setattr("src.query_governance.scheduler.BRAND_CONFIGS", {"atlas": {"enabled": True}})
    monkeypatch.setattr("src.query_governance.scheduler.is_brand_enabled", lambda _brand: True)
    monkeypatch.setattr(
        "src.query_governance.scheduler.get_brand_business_config",
        lambda _brand: {"top_query_governance_enabled": True},
    )

    result = query_governance_mainline_job(
        target_date=dt.date(2026, 9, 1),
        workflow_factory=lambda _brand: object(),
        experiment_service_factory=lambda: ExperimentService(),
        selection_service_factory=lambda _brand: SelectionService(),
    )

    assert result["status"] == expected_batch_status
    assert result["brands"]["atlas"]["status"] == expected_brand_status
    assert result["brands"]["atlas"]["errorCount"] == expected_errors


def test_scheduler_empty_selection_is_not_failure_and_other_brands_continue(monkeypatch):
    from src.query_governance.scheduler import query_governance_mainline_job

    class SelectionService:
        def select_and_start(self, **kwargs):
            if kwargs["brand"] == "harbor":
                raise TimeoutError("source timeout")
            return SimpleNamespace(batch_id=kwargs["batch_id"], selected=(), candidates=()), {}

    monkeypatch.setattr("src.query_governance.scheduler.reload_config", lambda: None)
    monkeypatch.setattr("src.query_governance.scheduler.BRAND_CONFIGS", {"harbor": {}, "atlas": {}})
    monkeypatch.setattr("src.query_governance.scheduler.is_brand_enabled", lambda _: True)
    monkeypatch.setattr("src.query_governance.scheduler.get_brand_business_config", lambda _: {})
    result = query_governance_mainline_job(
        target_date=dt.date(2026, 9, 7), workflow_factory=lambda _: object(),
        experiment_service_factory=lambda: SimpleNamespace(get_active_experiment_for_brand=lambda *args, **kwargs: None),
        selection_service_factory=lambda _: SelectionService(),
    )
    assert result["status"] == "error"
    assert result["brands"]["harbor"]["errorMessage"] == "source timeout"
    assert result["brands"]["atlas"]["status"] == "skipped"
    assert result["brands"]["atlas"]["reason"] == "no_candidates"


def test_checkpoint_adopts_selection_tasks_instead_of_inserting_duplicates():
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session, sessionmaker

    from src.persistence.models import Base
    from src.query_governance.checkpoint import SqlAlchemyCheckpointStore
    from src.query_governance.models import (
        QueryGovernanceSelectionBatch,
        QueryGovernanceTask,
        QuerySpec,
    )

    db_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(db_engine)
    factory = sessionmaker(bind=db_engine)
    with factory() as session:
        session.add(
            QueryGovernanceSelectionBatch(
                batch_id="batch-selection",
                brand="atlas",
                data_date=dt.date(2026, 9, 1),
                config_json="{}",
            )
        )
        session.flush()
        session.add(
            QueryGovernanceTask(
                batch_id="batch-selection",
                brand="atlas",
                data_date=dt.date(2026, 9, 1),
                query_text="裙子",
                query_rank=1,
                status="pending",
            )
        )
        session.commit()

    store = SqlAlchemyCheckpointStore(factory)
    checkpoint = store.start_batch(
        "batch-selection",
        [QuerySpec("q1", "atlas", "裙子")],
        data_date=dt.date(2026, 9, 1),
    )

    with Session(db_engine) as session:
        tasks = session.scalars(
            select(QueryGovernanceTask).where(
                QueryGovernanceTask.batch_id == "batch-selection"
            )
        ).all()

    assert checkpoint.status == "running"
    assert len(tasks) == 1
    assert tasks[0].query_text == "裙子"


@pytest.mark.parametrize('agent_mode', ['auto', 'human_approval', 'disabled'])
@pytest.mark.parametrize('tool_mode', ['auto', 'human_approval', 'disabled'])
def test_relevance_action_modes_take_strictest_configuration(monkeypatch, agent_mode, tool_mode):
    import src.query_governance.composition as composition

    monkeypatch.setattr(composition, 'get_brand_business_config', lambda brand: {
        'top_query_check_modes': {'relevance': agent_mode},
        'top_query_tool_modes': {'product_blacklist': tool_mode, 'kg_tag': tool_mode, 'kg_relation': tool_mode},
    })
    workflow = composition.build_query_governance_workflow(
        'atlas', session_factory=lambda: None,
        operational_service=_Operational(),
        model_config_snapshot=SimpleNamespace(version=17, nodes={}),
        model_runtime_factory=_runtime_factory,
        behavior_engine=create_engine('sqlite:///:memory:'),
    )
    expected = max((agent_mode, tool_mode), key=['auto', 'human_approval', 'disabled'].index)
    assert workflow._relevance_blacklist_execution_mode == expected


def test_recall_rate_catalog_falls_back_to_rd_rds_credentials(monkeypatch):
    """AI 召回率检测的目录数据源未显式配置时，应复用取词的 RD_RDS_* 凭据构建 evaluator。"""
    from src.query_governance.composition import build_query_governance_workflow

    for key in ("PRODUCT_DB_HOST", "PRODUCT_DB_USER", "PRODUCT_DB_PASSWORD", "PRODUCT_DB_NAME", "PRODUCT_DB_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RD_RDS_HOST", "rds.example.com")
    monkeypatch.setenv("RD_RDS_USER", "readonly_user")
    monkeypatch.setenv("RD_RDS_PASS", "secret")
    monkeypatch.setenv("RD_RDS_DB", "search-ops-admin")
    monkeypatch.setenv("RD_RDS_PORT", "3307")

    workflow = build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=_Operational(),
        model_config_snapshot=SimpleNamespace(version=17, nodes={}),
        model_runtime_factory=_runtime_factory,
        behavior_engine=create_engine("sqlite:///:memory:"),
    )
    recall_runner = workflow.runners["recall"]
    evaluator = recall_runner.recall_rate_evaluator
    assert evaluator is not None
    product_source = evaluator.llm_recall.product_source
    assert product_source.host == "rds.example.com"
    assert product_source.user == "readonly_user"
    assert product_source.password == "secret"
    assert product_source.database == "search-ops-admin"
    assert product_source.port == 3307


def test_recall_rate_catalog_disabled_without_any_source_env(monkeypatch):
    from src.query_governance.composition import build_query_governance_workflow

    for prefix in ("PRODUCT_DB_", "RD_RDS_"):
        for suffix in ("HOST", "USER", "PASS", "DB", "PORT"):
            monkeypatch.delenv(prefix + suffix, raising=False)

    workflow = build_query_governance_workflow(
        "atlas",
        session_factory=lambda: None,
        operational_service=_Operational(),
        model_config_snapshot=SimpleNamespace(version=17, nodes={}),
        model_runtime_factory=_runtime_factory,
        behavior_engine=create_engine("sqlite:///:memory:"),
    )
    assert workflow.runners["recall"].recall_rate_evaluator is None
