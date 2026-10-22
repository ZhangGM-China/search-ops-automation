from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.models import (
    NodeResult,
    NodeStatus,
    QueryGovernanceExperiment,
    QueryGovernanceExperimentDailyMetric,
    QueryRunResult,
    QuerySpec,
)
from src.query_governance.ports import ActionGateway, SearchGateway, StateStore
from src.query_governance.experiments import (
    QueryGovernanceExperimentCoordinator,
    QueryGovernanceExperimentScheduler,
    QueryGovernanceExperimentService,
    QueryExtractorExperimentMetricsProvider,
    SqlAlchemyExperimentMetricsObserver,
)
from src.query_governance.selection import (
    FixedExperimentSample,
    InMemoryQuerySelectionStore,
    QuerySelectionConfig,
    QuerySelectionService,
)
from src.query_governance.workflow import QueryGovernanceWorkflow


@dataclass
class Search(SearchGateway):
    calls: list[tuple[str, str]] = field(default_factory=list)

    def mixed(self, brand: str, query: str):
        self.calls.append((brand, query))
        return [{"product_id": f"{query}-1", "rank": 1}]


@dataclass
class Store(StateStore):
    snapshots: dict[tuple[str, str], dict] = field(default_factory=dict)
    writes: list[tuple[str, str, str, NodeResult]] = field(default_factory=list)

    def get_s0(self, brand: str, query: str):
        return self.snapshots.get((brand, query))

    def save_s0_if_absent(self, brand: str, query: str, snapshot):
        return self.snapshots.setdefault((brand, query), snapshot)

    def save_s0_failure(self, brand: str, query: str, error_code: str, error_message: str):
        raise AssertionError(error_code, error_message)

    def save_step(self, brand: str, query: str, node: str, result: NodeResult):
        self.writes.append((brand, query, node, result))


class FailingRunner:
    def __init__(self, node: str):
        self.node = node
        self.calls = 0

    def run(self, spec, context):
        self.calls += 1
        raise AssertionError(f"control group called {self.node}")


class Action(ActionGateway):
    def __init__(self):
        self.calls = 0

    def execute_and_verify(self, spec, action, context):
        self.calls += 1
        return NodeResult.executed({"action": action})


class Observer:
    def __init__(self):
        self.calls = []

    def record(self, spec, run, *, data_date, batch_id):
        self.calls.append((spec, run, data_date))

    def record_observation(
        self, spec, products, *, data_date, batch_id, error=None
    ):
        self.calls.append(
            (spec, {"products": products, "error": error}, data_date)
        )


def build_workflow(observer=None):
    runners = {node: FailingRunner(node) for node in ("recall", "reorder", "relevance")}
    search = Search()
    action = Action()
    workflow = QueryGovernanceWorkflow(
        search=search,
        store=Store(),
        runners=runners,
        action_gateway=action,
        experiment_observer=observer,
    )
    return workflow, runners, search, action


def test_control_group_reads_s0_but_skips_all_governance_nodes_and_actions():
    workflow, runners, search, action = build_workflow()
    result = workflow.run_batch([
        QuerySpec("control-1", "atlas", "裙子", experiment_id=7, group_type="control")
    ])["control-1"]

    assert len(search.calls) == 1
    assert result.nodes["s0"].status is NodeStatus.EXECUTED
    assert all(result.nodes[node].status is NodeStatus.SKIPPED for node in runners)
    assert all(runner.calls == 0 for runner in runners.values())
    assert action.calls == 0


def test_experiment_observer_receives_treatment_and_control_runs():
    observer = Observer()
    workflow, _, _, _ = build_workflow(observer)
    workflow.run_batch([
        QuerySpec("treatment-1", "atlas", "裙子", experiment_id=7, group_type="treatment"),
        QuerySpec("control-1", "atlas", "外套", experiment_id=7, group_type="control"),
    ], data_date=date(2026, 8, 29))

    assert sorted(call[0].group_type for call in observer.calls) == [
        "control",
        "treatment",
    ]
    assert all(call[2] == date(2026, 8, 29) for call in observer.calls)


def test_selection_batch_runs_treatment_and_control_in_one_workflow_batch():
    class RecordingBatchWorkflow:
        def __init__(self):
            self.specs = None
            self.kwargs = None

        def run_batch(self, specs, **kwargs):
            self.specs = list(specs)
            self.kwargs = kwargs
            return {}

    workflow = RecordingBatchWorkflow()
    service = QuerySelectionService(
        source=type("Source", (), {"fetch": lambda *_: None})(),
        store=InMemoryQuerySelectionStore(),
    )
    batch, _ = service.select_and_start(
        batch_id="experiment-batch",
        brand="atlas",
        data_date=date(2026, 8, 29),
        config=QuerySelectionConfig(),
        workflow=workflow,
        fixed_samples=(
            FixedExperimentSample("exp-1", "连衣裙", "treatment", 1),
            FixedExperimentSample("exp-1", "外套", "control", 2),
        ),
    )

    assert [spec.group_type for spec in workflow.specs] == [
        "treatment",
        "control",
    ]
    assert workflow.kwargs == {
        "batch_id": "experiment-batch",
        "data_date": date(2026, 8, 29),
    }
    assert len(batch.control_specs) == 1


def test_experiment_coordinator_uses_one_batch_and_never_runs_controls_separately():
    class RecordingWorkflow:
        def __init__(self):
            self.calls = []

        def run_batch(self, specs, **kwargs):
            self.calls.append((list(specs), kwargs))
            return {spec.query_id: object() for spec in specs}

    workflow = RecordingWorkflow()
    coordinator = QueryGovernanceExperimentCoordinator(workflow, object())
    specs = coordinator.run(
        [QuerySpec("t", "atlas", "裙子", experiment_id=7, group_type="treatment")],
        [QuerySpec("c", "atlas", "外套", experiment_id=7, group_type="control")],
        batch_id="batch-1",
        data_date=date(2026, 8, 29),
    )

    assert len(workflow.calls) == 1
    assert [spec.group_type for spec in workflow.calls[0][0]] == [
        "treatment",
        "control",
    ]
    assert workflow.calls[0][1] == {
        "batch_id": "batch-1",
        "data_date": date(2026, 8, 29),
    }
    assert set(specs) == {"t", "c"}


def test_daily_metrics_observer_persists_groups_idempotently_and_blocks_control_actions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    from src.query_governance.models import (
        QueryGovernanceExperiment,
        QueryGovernanceExperimentDailyMetric,
        QueryGovernanceTask,
    )

    with factory() as session:
        session.add(QueryGovernanceExperiment(id=7, brand="atlas", experiment_name="exp"))
        session.add(
            QueryGovernanceTask(
                id=11,
                batch_id="batch-1",
                brand="atlas",
                data_date=date(2026, 8, 29),
                query_text="裙子",
                status="completed",
            )
        )
        session.commit()

    observer = SqlAlchemyExperimentMetricsObserver(factory)
    treatment = QuerySpec("t", "atlas", "裙子", experiment_id=7, group_type="treatment")
    control = QuerySpec("c", "atlas", "外套", experiment_id=7, group_type="control")
    treatment_run = QueryRunResult(
        spec=treatment,
        final_status="completed",
        nodes={"relevance": NodeResult.executed(action={"type": "blacklist"})},
    )
    observer.record(treatment, treatment_run, data_date=date(2026, 8, 29), batch_id="batch-1")
    observer.record(treatment, treatment_run, data_date=date(2026, 8, 29), batch_id="batch-1")
    observer.record_observation(
        control,
        [{"product_id": "p1"}],
        data_date=date(2026, 8, 29),
        batch_id="batch-1",
    )

    with factory() as session:
        rows = session.query(QueryGovernanceExperimentDailyMetric).order_by(
            QueryGovernanceExperimentDailyMetric.query_text
        ).all()

    assert len(rows) == 2
    assert [(row.query_text, row.group_type, row.action_count) for row in rows] == [
        ("外套", "control", 0),
        ("裙子", "treatment", 1),
    ]
    assert rows[1].governance_task_id == 11


def test_experiment_service_uses_rewrite_tables_for_lifecycle_and_active_samples():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    service = QueryGovernanceExperimentService(factory)

    draft = service.create_or_replace_draft(
        brand="atlas",
        created_by="operator",
        experiment_name="ranking test",
        treatment_queries=["裙子"],
        control_queries=["外套"],
        today=date(2026, 8, 29),
    )
    assert draft["status"] == "draft"
    scheduled = service.schedule_experiment(draft["id"])
    assert scheduled["status"] == "scheduled"
    active = service.get_active_experiment_for_brand("atlas", execution_date=date(2026, 8, 29))
    assert active["id"] == draft["id"]
    assert [(item["query"], item["groupType"]) for item in active["samples"]] == [
        ("裙子", "treatment"),
        ("外套", "control"),
    ]


def test_experiment_service_reads_new_daily_metrics_for_detail_and_report():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        experiment = QueryGovernanceExperimentService(factory).create_or_replace_draft(
            brand="atlas",
            created_by="operator",
            experiment_name="ranking test",
            treatment_queries=["裙子"],
            control_queries=["外套"],
            today=date(2026, 8, 29),
        )
        session.add(
            QueryGovernanceExperimentDailyMetric(
                experiment_id=experiment["id"],
                brand="atlas",
                query_text="裙子",
                group_type="treatment",
                data_date=date(2026, 8, 29),
                search_pv=100,
                ctr=0.2,
                cvr=0.03,
                governance_status="completed",
                action_count=1,
            )
        )
        session.commit()
    detail = QueryGovernanceExperimentService(factory).get_experiment_detail(experiment["id"])
    assert detail["dailyMetrics"][0]["queryText"] == "裙子"
    assert detail["dailyMetrics"][0]["searchPv"] == 100
    assert detail["dailyMetrics"][0]["actionCount"] == 1


def test_experiment_coordinator_builds_specs_from_rewrite_experiment_service():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    service = QueryGovernanceExperimentService(factory)
    draft = service.create_or_replace_draft(
        brand="atlas",
        created_by="operator",
        experiment_name="ranking test",
        treatment_queries=["裙子"],
        control_queries=["外套"],
        today=date(2026, 8, 29),
    )
    service.schedule_experiment(draft["id"])
    coordinator = QueryGovernanceExperimentCoordinator(
        workflow=type("Workflow", (), {"run_batch": lambda self, specs, **kwargs: specs})(),
        observer=object(),
    )
    result = coordinator.run_active(
        service,
        brand="atlas",
        experiment_id=draft["id"],
        batch_id="batch-1",
        data_date=date(2026, 8, 29),
    )
    assert [item.group_type for item in result] == ["treatment", "control"]


def test_experiment_scheduler_runs_active_samples_through_injected_workflow():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    service = QueryGovernanceExperimentService(factory)
    draft = service.create_or_replace_draft(
        brand="atlas",
        created_by="operator",
        experiment_name="ranking test",
        treatment_queries=["裙子"],
        control_queries=["外套"],
        today=date(2026, 8, 29),
    )
    service.schedule_experiment(draft["id"])
    observed = []

    class Workflow:
        def run_batch(self, specs, **kwargs):
            observed.append((list(specs), kwargs))
            return {
                spec.query_id: QueryRunResult(spec=spec, final_status="completed")
                for spec in specs
            }

    scheduler = QueryGovernanceExperimentScheduler(
        factory, lambda _brand: Workflow()
    )
    result = scheduler.run_brand(
        "atlas", data_date=date(2026, 8, 29), batch_id="batch-1"
    )
    assert result["status"] == "completed"
    assert result["treatmentCount"] == 1
    assert result["controlCount"] == 1
    assert observed[0][1] == {"batch_id": "batch-1", "data_date": date(2026, 8, 29)}


def test_scheduler_entrypoint_routes_enabled_brands_to_rewrite_experiment_scheduler(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    service = QueryGovernanceExperimentService(factory)
    draft = service.create_or_replace_draft(
        brand="atlas",
        created_by="operator",
        experiment_name="ranking test",
        treatment_queries=["裙子"],
        control_queries=["外套"],
        today=date(2026, 8, 29),
    )
    service.schedule_experiment(draft["id"])

    from src.config import BRAND_CONFIGS
    from src.scheduler import runner as scheduler_module

    class Workflow:
        def run_batch(self, specs, **kwargs):
            return {
                spec.query_id: QueryRunResult(spec=spec, final_status="completed")
                for spec in specs
            }

    monkeypatch.setattr(scheduler_module, "SessionLocal", factory)
    monkeypatch.setattr(scheduler_module, "reload_config", lambda: None)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {"atlas": {}})
    monkeypatch.setattr("src.config.is_brand_enabled", lambda _brand: True)
    result = scheduler_module.query_governance_experiment_job(
        target_date=date(2026, 8, 29), workflow_factory=lambda _brand: Workflow()
    )
    assert result["status"] == "success"
    assert result["brands"]["atlas"]["treatmentCount"] == 1
    assert result["brands"]["atlas"]["controlCount"] == 1


def test_query_extractor_metrics_provider_reads_metrics_for_the_run_date():
    calls = []

    class Extractor:
        def get_query_daily_metrics_strict(self, brand, data_date, queries):
            calls.append((brand, data_date, queries))
            return {
                "status": "complete",
                "items": [{"query": "裙子", "search_pv": 321, "ctr": 0.12, "cvr": 0.03}],
                "missingKeys": [],
            }

    provider = QueryExtractorExperimentMetricsProvider(lambda brand: Extractor())
    result = provider(
        QuerySpec("q1", "atlas", "裙子", experiment_id=7, group_type="treatment"),
        {},
        data_date=date(2026, 8, 29),
    )

    assert result == {"search_pv": 321, "ctr": 0.12, "cvr": 0.03}
    assert calls == [("atlas", date(2026, 8, 29), ["裙子"])]


def test_experiment_scheduler_activates_scheduled_experiment_and_persists_real_metrics():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    service = QueryGovernanceExperimentService(factory)
    draft = service.create_or_replace_draft(
        brand="atlas",
        created_by="operator",
        experiment_name="ranking test",
        treatment_queries=["裙子"],
        control_queries=["外套"],
        today=date(2026, 8, 29),
    )
    service.schedule_experiment(draft["id"])

    class Extractor:
        def get_query_daily_metrics_strict(self, brand, data_date, queries):
            return {
                "status": "complete",
                "items": [{
                    "query": queries[0],
                    "search_pv": 321 if queries[0] == "裙子" else 123,
                    "ctr": 0.12,
                    "cvr": 0.03,
                }],
                "missingKeys": [],
            }

    class Workflow:
        def run_batch(self, specs, **kwargs):
            for spec in specs:
                if spec.is_control:
                    self.experiment_observer.record_observation(
                        spec, [{"product_id": "p1"}], **kwargs
                    )
                else:
                    self.experiment_observer.record(
                        spec,
                        QueryRunResult(spec=spec, final_status="completed"),
                        **kwargs,
                    )
            return {
                spec.query_id: QueryRunResult(spec=spec, final_status="completed")
                for spec in specs
            }

    scheduler = QueryGovernanceExperimentScheduler(
        factory,
        lambda _brand: Workflow(),
        metrics_provider_factory=lambda _brand: QueryExtractorExperimentMetricsProvider(
            lambda _brand: Extractor()
        ),
    )
    result = scheduler.run_brand(
        "atlas", data_date=date(2026, 8, 29), batch_id="batch-1"
    )

    assert result["status"] == "completed"
    with factory() as session:
        experiment = session.get(QueryGovernanceExperiment, draft["id"])
        rows = session.query(QueryGovernanceExperimentDailyMetric).order_by(
            QueryGovernanceExperimentDailyMetric.query_text
        ).all()
    assert experiment.status == "running"
    assert [(row.query_text, row.search_pv) for row in rows] == [("外套", 123), ("裙子", 321)]


def test_scheduler_registration_removes_unconfigured_job_and_registers_configured_job():
    from src.scheduler import runner as scheduler_module

    class FakeScheduler:
        def __init__(self):
            self.jobs = {}
            self.added = []
            self.removed = []

        def get_job(self, job_id):
            return self.jobs.get(job_id)

        def remove_job(self, job_id):
            self.removed.append(job_id)
            self.jobs.pop(job_id, None)

        def add_job(self, func, trigger, **kwargs):
            self.added.append((func, trigger, kwargs))
            self.jobs[kwargs["id"]] = kwargs

    scheduler = FakeScheduler()
    scheduler.jobs[scheduler_module.QUERY_GOVERNANCE_EXPERIMENT_JOB_ID] = object()
    scheduler_module.sync_query_governance_experiment_job(scheduler)
    assert scheduler.removed == [scheduler_module.QUERY_GOVERNANCE_EXPERIMENT_JOB_ID]

    factory = lambda _brand: object()
    scheduler_module.sync_query_governance_experiment_job(scheduler, factory)
    assert scheduler.added[0][1] == "cron"
    assert scheduler.added[0][2]["kwargs"]["workflow_factory"] is factory
    assert scheduler.added[0][2]["id"] == scheduler_module.QUERY_GOVERNANCE_EXPERIMENT_JOB_ID


def test_scheduler_registers_query_governance_remediation_interval_job():
    from src.scheduler import runner as scheduler_module

    class FakeScheduler:
        def __init__(self):
            self.jobs = {}
            self.added = []

        def get_job(self, job_id):
            return self.jobs.get(job_id)

        def remove_job(self, job_id):
            self.jobs.pop(job_id, None)

        def add_job(self, func, trigger, **kwargs):
            self.added.append((func, trigger, kwargs))
            self.jobs[kwargs["id"]] = kwargs

    scheduler = FakeScheduler()
    factory = lambda _brand: object()
    scheduler_module.sync_query_governance_remediation_job(scheduler, factory)

    assert scheduler.added[0][1] == "interval"
    assert scheduler.added[0][2]["minutes"] == 5
    assert scheduler.added[0][2]["kwargs"]["workflow_factory"] is factory
    assert scheduler.added[0][2]["id"] == scheduler_module.QUERY_GOVERNANCE_REMEDIATION_JOB_ID
