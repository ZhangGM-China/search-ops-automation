from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from io import BytesIO
import json
import threading
import time
from types import SimpleNamespace

from PIL import Image
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.models import (
    QueryGovernanceTask, QueryGovernanceWorkflowRun, QueryGovernanceStepStatus,
    QuerySpec, NodeResult,
)
from src.query_governance.relevance import PillowImageTransformer, RelevanceError
import src.query_governance.scheduler as jobs
from src.runtime.process_guard import acquire_process_lock
from src.scheduler.recovery import SchedulerLeaseHeartbeat


def test_jpeg_is_reduced_before_first_full_decode(monkeypatch):
    source = BytesIO()
    with Image.new("RGB", (5000, 5000), "gray") as image:
        image.save(source, format="JPEG")
    sizes = []
    original = Image.Image.load

    def observe(image, *args, **kwargs):
        sizes.append(image.size)
        return original(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", observe)
    output = PillowImageTransformer().transform(source.getvalue(), "image/jpeg")
    assert output.width == output.height == 768
    assert max(w * h for w, h in sizes) <= 1250 * 1250


def test_oversized_non_jpeg_is_rejected_before_decode(monkeypatch):
    class HeaderOnly:
        size = (8000, 8000)
        format = "PNG"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def load(self):
            pytest.fail("unsafe image must be rejected before full decode")

    monkeypatch.setattr(Image, "open", lambda _value: HeaderOnly())
    with pytest.raises(RelevanceError) as error:
        PillowImageTransformer().transform(b"header", "image/png")
    assert error.value.error_code == "IMAGE_PIXEL_LIMIT_EXCEEDED"


def test_decode_concurrency_is_shared_across_transformer_instances(monkeypatch):
    source = BytesIO()
    with Image.new("RGB", (100, 100), "gray") as image:
        image.save(source, format="PNG")
    payload = source.getvalue()
    original = Image.open
    lock = threading.Lock()
    active = peak = 0

    @contextmanager
    def observe(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            with original(*args, **kwargs) as image:
                yield image
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(Image, "open", observe)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(
            lambda _: PillowImageTransformer().transform(payload, "image/png"), range(8)
        ))
    assert len(outputs) == 8
    assert 1 <= peak <= 2


@pytest.fixture
def database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


def seed_running(factory, *, yielded=False):
    batch_id = "query-governance:summit:2026-10-01"
    with factory() as db:
        db.add(QueryGovernanceWorkflowRun(
            batch_id=batch_id, status="running",
            heartbeat_at=datetime(2026, 10, 7, 12) - timedelta(minutes=1 if yielded else 31),
            checkpoint_payload=json.dumps({"invocation_yielded": yielded}),
        ))
        task = QueryGovernanceTask(
            batch_id=batch_id, brand="summit", data_date=date(2026, 10, 1),
            query_text="徒步帽子", query_rank=1, status="running",
        )
        db.add(task)
        db.flush()
        db.add(QueryGovernanceStepStatus(task_id=task.id, node_key="relevance", status="pending"))
        db.commit()
    return batch_id


@pytest.mark.parametrize("outcome", ["disabled", "empty", "heartbeat_only", "wrong_batch"])
def test_recovery_does_not_report_skip_or_no_progress_as_resumed(monkeypatch, database, outcome):
    batch_id = seed_running(database)

    def mainline(**_kwargs):
        if outcome == "heartbeat_only":
            with database() as db:
                row = db.scalars(select(QueryGovernanceWorkflowRun)).one()
                row.heartbeat_at = datetime(2026, 10, 7, 12)
                row.checkpoint_version += 1
                db.commit()
        brand = {"batchId": "other" if outcome == "wrong_batch" else batch_id,
                 "status": "skipped" if outcome == "disabled" else "running",
                 "reason": "brand_disabled" if outcome == "disabled" else None}
        return {"status": "success", "brands": {} if outcome == "empty" else {"summit": brand}}

    monkeypatch.setattr(jobs, "query_governance_mainline_job", mainline)
    result = jobs.query_governance_recovery_job(
        workflow_factory=lambda _: None, session_factory=database,
        now=datetime(2026, 10, 7, 12),
    )
    assert result["resumedCount"] == 0
    if outcome == "disabled":
        assert result["skippedCount"] == 1
        assert result["skips"][0]["reason"] == "brand_disabled"
    else:
        assert result["noProgressCount"] + result["failedCount"] == 1


def test_recovery_accepts_real_node_progress_in_yielded_batch(monkeypatch, database):
    batch_id = seed_running(database, yielded=True)

    def mainline(**_kwargs):
        with database() as db:
            row = db.scalars(select(QueryGovernanceStepStatus)).one()
            row.status = "executed"
            db.commit()
        return {"status": "success", "brands": {"summit": {
            "batchId": batch_id, "status": "running",
        }}}

    monkeypatch.setattr(jobs, "query_governance_mainline_job", mainline)
    result = jobs.query_governance_recovery_job(
        workflow_factory=lambda _: None, session_factory=database,
        now=datetime(2026, 10, 7, 12),
    )
    assert result["candidateCount"] == result["resumedCount"] == 1


def test_mainline_returns_explicit_disabled_scope(monkeypatch, database):
    monkeypatch.setattr(jobs, "reload_config", lambda: None)
    monkeypatch.setattr(jobs, "is_brand_enabled", lambda _: False)
    result = jobs.query_governance_mainline_job(
        brand_filter="summit", target_date=date(2026, 10, 1),
        workflow_factory=lambda _: pytest.fail("disabled brand must not construct workflow"),
        session_factory=database,
    )
    assert result["brands"]["summit"] == {
        "brand": "summit", "status": "skipped", "reason": "brand_disabled",
        "batchId": "query-governance:summit:2026-10-01", "dataDate": "2026-10-01",
    }


def test_bounded_workflow_preserves_full_scope_and_stage_barriers():
    from test_query_governance_checkpoint import (
        build_store, Search, Runner, Actions, MODEL_CONFIG_SNAPSHOT,
    )
    from src.query_governance.workflow import QueryGovernanceWorkflow
    engine, store = build_store()
    events = []
    items = [QuerySpec(f"q{i}", "atlas", f"query-{i}") for i in range(5)]
    try:
        workflow = QueryGovernanceWorkflow(
            search=Search(), store=store, checkpoint=store,
            runners={n: Runner(n, events) for n in ("recall", "reorder", "relevance")},
            action_gateway=Actions(), model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
            max_node_starts=3,
        )
        workflow.run_batch(items, batch_id="bounded", data_date=date(2026, 10, 1))
        assert not events
        assert store.get_checkpoint("bounded").status == "running"
        with store._session_factory() as db:
            assert len(db.scalars(select(QueryGovernanceTask)).all()) == 5
            row = db.scalars(select(QueryGovernanceWorkflowRun)).one()
            payload = json.loads(row.checkpoint_payload)
            assert payload["invocation_yielded"] is True
            assert [q["query_id"] for q in payload["query_order"]] == [s.query_id for s in items]
        for _ in range(12):
            workflow.run_batch(items, batch_id="bounded", data_date=date(2026, 10, 1))
            if store.get_checkpoint("bounded").status == "completed":
                break
        assert store.get_checkpoint("bounded").status == "completed"
        assert len(events) == len(set(events))
    finally:
        engine.dispose()


def test_bounded_s0_failure_does_not_starve_remaining_queries():
    from test_query_governance_checkpoint import build_store, Runner, Actions, MODEL_CONFIG_SNAPSHOT
    from src.query_governance.workflow import QueryGovernanceWorkflow
    engine, store = build_store()
    attempts = []

    class BrokenSearch:
        def mixed(self, brand, query):
            attempts.append(query)
            raise TimeoutError("temporary search failure")

    items = [QuerySpec(f"q{i}", "atlas", f"query-{i}") for i in range(6)]
    try:
        workflow = QueryGovernanceWorkflow(
            search=BrokenSearch(), store=store, checkpoint=store,
            runners={n: Runner(n, []) for n in ("recall", "reorder", "relevance")},
            action_gateway=Actions(), model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
            max_node_starts=3,
        )
        for _ in range(15):
            workflow.run_batch(items, batch_id="broken-s0")
            if store.get_checkpoint("broken-s0").status == "error":
                break
        assert store.get_checkpoint("broken-s0").status == "error"
        assert len(attempts) == 6
        assert len(set(attempts)) == 6
    finally:
        engine.dispose()


def test_time_budget_yields_after_inflight_result_and_resumes(monkeypatch):
    from test_query_governance_checkpoint import build_store, Search, Runner, Actions, MODEL_CONFIG_SNAPSHOT
    from src.query_governance.workflow import QueryGovernanceWorkflow
    engine, store = build_store()
    clock = [0.0]
    monkeypatch.setattr("src.query_governance.workflow.time.monotonic", lambda: clock[0])
    calls = []

    class SlowSearch(Search):
        def mixed(self, brand, query):
            calls.append(query)
            result = super().mixed(brand, query)
            clock[0] += 11
            return result

    items = [QuerySpec(f"q{i}", "atlas", f"query-{i}") for i in range(2)]
    try:
        workflow = QueryGovernanceWorkflow(
            search=SlowSearch(), store=store, checkpoint=store,
            runners={n: Runner(n, []) for n in ("recall", "reorder", "relevance")},
            action_gateway=Actions(), model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
            max_execution_seconds=10, reorder_enabled=False, relevance_enabled=False,
        )
        workflow.run_batch(items, batch_id="time-bound")
        assert calls == ["query-0"]
        assert store.get_node("time-bound", items[0], "s0").terminal
        assert store.get_checkpoint("time-bound").status == "running"
        for _ in range(4):
            workflow.run_batch(items, batch_id="time-bound")
        assert store.get_checkpoint("time-bound").status == "completed"
        assert calls == ["query-0", "query-1"]
    finally:
        engine.dispose()


def test_yield_checkpoint_rejects_stale_writer_and_preserves_progress():
    from test_query_governance_checkpoint import build_store
    from src.query_governance.checkpoint import CheckpointConflictError
    engine, store = build_store()
    try:
        item = QuerySpec("q1", "atlas", "query-1")
        initial = store.start_batch("yield-cas", [item])
        current = store.begin_node("yield-cas", item, "s0", initial.version)
        with pytest.raises(CheckpointConflictError):
            store.yield_batch("yield-cas", initial.version)
        assert store.get_checkpoint("yield-cas").version == current.version
        yielded = store.yield_batch("yield-cas", current.version)
        assert store.start_batch("yield-cas", [item]).resumed_after_yield is True
        assert store.get_checkpoint("yield-cas").version > yielded.version
    finally:
        engine.dispose()


def test_process_lock_release_keeps_inode_for_waiting_holders(tmp_path):
    first = acquire_process_lock("scheduler", lock_dir=tmp_path)
    inode = first.path.stat().st_ino
    first.release()
    assert first.path.exists()
    second = acquire_process_lock("scheduler", lock_dir=tmp_path)
    try:
        assert second.path.stat().st_ino == inode
    finally:
        second.release()


def test_lease_loss_notifies_runtime_with_safe_error(monkeypatch, caplog):
    observed = []
    heartbeat = SchedulerLeaseHeartbeat(
        leader_name="scheduler-main", owner_id="pod:1", fencing_token=1,
        lease_seconds=30, interval_seconds=0.01,
        on_lost=lambda exc: observed.append(type(exc).__name__),
    )
    monkeypatch.setattr(heartbeat, "beat_once", lambda: (_ for _ in ()).throw(RuntimeError("secret")))
    heartbeat._run()
    assert observed == ["RuntimeError"]
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


def test_status_tracks_process_and_freshness(monkeypatch, tmp_path):
    import os
    from importlib import import_module
    scheduler = import_module("src.scheduler.runner")
    monkeypatch.setattr(scheduler, "SCHEDULER_STATUS_FILE", tmp_path / "status.json")
    scheduler.write_scheduler_status(ready=True, mode="active", state="ready")
    value = json.loads((tmp_path / "status.json").read_text())
    assert value["pid"] == os.getpid()
    assert abs(value["updatedAt"] - time.time()) < 1


def test_heartbeat_refreshes_health_after_successful_renewal(monkeypatch):
    notified = []
    heartbeat = SchedulerLeaseHeartbeat(
        leader_name="scheduler-main", owner_id="pod:1", fencing_token=1,
        lease_seconds=30, interval_seconds=0.01,
        on_renewed=lambda: notified.append(True),
    )
    monkeypatch.setattr(heartbeat, "beat_once", lambda: heartbeat._stop_event.set())
    heartbeat._run()
    assert notified == [True]


def test_lost_fence_blocks_external_action_after_model_returns():
    from test_query_governance_checkpoint import build_store, Search, Actions, MODEL_CONFIG_SNAPSHOT
    from src.query_governance.workflow import QueryGovernanceWorkflow
    from src.persistence import SchedulerLeaderFenceError
    engine, store = build_store()
    lost = False
    writes = []

    class Runner:
        def run(self, spec, context):
            nonlocal lost
            lost = True
            return NodeResult.executed({}, action={"type": "query_reorder"})

    class Gateway(Actions):
        def execute_and_verify(self, *args):
            writes.append(args)
            return NodeResult.executed({})

    def assert_fence():
        if lost:
            raise SchedulerLeaderFenceError("lost")

    try:
        workflow = QueryGovernanceWorkflow(
            search=Search(), store=store, checkpoint=store,
            runners={n: Runner() for n in ("recall", "reorder", "relevance")},
            action_gateway=Gateway(), model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
        )
        workflow.assert_scheduler_fence = assert_fence
        with pytest.raises(SchedulerLeaderFenceError):
            workflow.run_batch([QuerySpec("q1", "atlas", "裙子")], batch_id="fenced")
        assert writes == []
    finally:
        engine.dispose()


def test_startup_waits_for_existing_leader_without_force_claim(monkeypatch):
    from unittest.mock import MagicMock
    from importlib import import_module
    from src.persistence import SchedulerLeaderLeaseUnavailable
    scheduler = import_module("src.scheduler.runner")
    db = MagicMock()
    acquire = MagicMock(side_effect=[SchedulerLeaderLeaseUnavailable("held"), SimpleNamespace(fencing_token=2)])
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(scheduler, "BlockingScheduler", MagicMock())
    monkeypatch.setattr(scheduler, "SchedulerLeaseHeartbeat", MagicMock(return_value=MagicMock(spec=SchedulerLeaseHeartbeat)))
    monkeypatch.setattr(scheduler, "acquire_scheduler_leader_lease", acquire)
    monkeypatch.setattr(scheduler, "recover_stale_scheduler_job_runs", lambda *_a, **_kw: 0)
    monkeypatch.setattr(scheduler, "release_scheduler_leader_lease", MagicMock())
    monkeypatch.setattr(scheduler, "plan_and_persist_missed_scheduler_runs", MagicMock())
    monkeypatch.setattr(scheduler.time, "sleep", lambda _: None)
    monkeypatch.setattr("src.config.BRAND_CONFIGS", {})
    assert scheduler.start_scheduler(leader_wait_seconds=1) is True
    assert acquire.call_count == 2
    db.rollback.assert_called_once()


def test_500_queries_finish_across_bounded_invocations_without_duplicate_actions(tmp_path):
    from test_query_governance_checkpoint import Search, Actions, MODEL_CONFIG_SNAPSHOT
    from src.query_governance.workflow import QueryGovernanceWorkflow
    from src.query_governance.checkpoint import SqlAlchemyCheckpointStore
    engine = create_engine("sqlite:///" + str(tmp_path / "bounded.db"))
    Base.metadata.create_all(engine)
    store = SqlAlchemyCheckpointStore(sessionmaker(bind=engine))
    events, writes = [], []

    class ActionRunner:
        def __init__(self, node):
            self.node = node

        def run(self, spec, context):
            events.append((self.node, spec.query_id))
            return NodeResult.executed({"ok": True}, action={"type": self.node})

    class RecordingActions(Actions):
        def execute_and_verify(self, spec, action, context):
            writes.append((action["type"], spec.query_id))
            return NodeResult.executed({"ok": True})

    class BatchRelevance:
        def run(self, inputs, context=None):
            return {item.spec.query: NodeResult.executed({
                "detected_product_ids": [item.spec.query + "-1"],
                "blacklist_candidates": [], "product_count": 1,
            }) for item in inputs}

    items = [QuerySpec(f"q{i}", "atlas", f"query-{i}") for i in range(500)]
    try:
        workflow = QueryGovernanceWorkflow(
            search=Search(), store=store, checkpoint=store,
            runners={n: ActionRunner(n) for n in ("recall", "reorder", "relevance")},
            action_gateway=RecordingActions(), model_config_snapshot=MODEL_CONFIG_SNAPSHOT,
            max_node_starts=100, query_concurrency=10,
            relevance_batch_runner=BatchRelevance(),
        )
        invocations = 0
        for _ in range(30):
            workflow.run_batch(items, batch_id="bounded-500", data_date=date(2026, 10, 1))
            invocations += 1
            if store.get_checkpoint("bounded-500").status == "completed":
                break
        with store._session_factory() as db:
            steps = db.scalars(select(QueryGovernanceStepStatus)).all()
            from collections import Counter
            progress = Counter((s.node_key, s.status, s.error_code) for s in steps)
        assert store.get_checkpoint("bounded-500").status == "completed", progress
        assert invocations > 1
        assert len(writes) > 0 and len(writes) == len(set(writes))
        with store._session_factory() as db:
            assert len(db.scalars(select(QueryGovernanceTask)).all()) == 500
            assert all(s.status in {"executed", "skipped"}
                       for s in db.scalars(select(QueryGovernanceStepStatus)).all())
    finally:
        engine.dispose()
