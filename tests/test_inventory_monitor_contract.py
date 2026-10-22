"""RED contract tests for the independent broken-rate monitor (AIOPS-198).

These tests describe the new runtime boundary.  They intentionally do not
exercise the legacy Agent Loop entrypoints or permit QueryExtractor to be
used at monitor runtime.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.persistence.models import Base
from src.query_governance.models import (
    QueryGovernanceAgentEvent,
    QueryGovernanceCandidate,
    QueryGovernanceSelectionBatch,
    QueryGovernanceTask,
)
from src.query_governance.selection import QuerySelectionConfig


def _inventory_monitor_module():
    """Import the new domain module inside the test so RED remains executable."""

    import importlib

    return importlib.import_module("src.query_governance.inventory_monitor")


def _config_json() -> str:
    import json

    return json.dumps(QuerySelectionConfig().as_mapping(), ensure_ascii=False)


def _batch(batch_id: str, data_date: date):
    return SimpleNamespace(batch_id=batch_id, data_date=data_date, specs=())


def test_selection_reader_returns_latest_persisted_batch_queries(tmp_path):
    """The monitor must consume the newest persisted batch, not re-extract Top Queries."""

    engine = create_engine(f"sqlite:///{tmp_path / 'selection.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as db:
        old = QueryGovernanceSelectionBatch(
            batch_id="batch-old",
            brand="atlas",
            data_date=date(2026, 8, 27),
            config_json=_config_json(),
        )
        new = QueryGovernanceSelectionBatch(
            batch_id="batch-new",
            brand="atlas",
            data_date=date(2026, 8, 28),
            config_json=_config_json(),
        )
        db.add_all([old, new])
        db.flush()
        db.add_all([
            QueryGovernanceCandidate(
                selection_batch_id=old.id,
                batch_id=old.batch_id,
                brand="atlas",
                data_date=old.data_date,
                query_id="q-old",
                query_key="旧词",
                query_text="旧词",
                matched_rules_json="[]",
                metrics_json="{}",
                source_type="daily",
                selection_rank=1,
                selected=True,
            ),
            QueryGovernanceCandidate(
                selection_batch_id=new.id,
                batch_id=new.batch_id,
                brand="atlas",
                data_date=new.data_date,
                query_id="q-new",
                query_key="新词",
                query_text="新词",
                matched_rules_json="[]",
                metrics_json="{}",
                source_type="daily",
                selection_rank=1,
                selected=True,
            ),
        ])
        db.commit()

    inventory_monitor = _inventory_monitor_module()
    reader = inventory_monitor.SqlAlchemyLatestQueryBatchReader(factory)
    result = reader.get_latest("atlas", before=datetime(2026, 8, 29, 1, 0))

    assert result.batch_id == "batch-new"
    assert [spec.query for spec in result.specs] == ["新词"]
    engine.dispose()


def test_selection_batch_before_0030_is_previous_persisted_batch():
    """Between midnight and 00:30, latest persisted data is still the prior batch."""

    reader = MagicMock()
    reader.get_latest.return_value = _batch("batch-previous", date(2026, 8, 27))

    inventory_monitor = _inventory_monitor_module()
    result = inventory_monitor.resolve_latest_persisted_queries(
        reader,
        brand="atlas",
        now=datetime(2026, 8, 29, 0, 15),
    )

    assert result.batch_id == "batch-previous"
    reader.get_latest.assert_called_once_with(
        "atlas", before=datetime(2026, 8, 29, 0, 15)
    )


def test_broken_rate_monitor_does_not_call_query_extractor_at_runtime():
    """Cycle execution must use the persisted selection batch as its only Query source."""

    inventory_monitor = _inventory_monitor_module()
    with patch("src.scheduler.runner.QueryExtractor") as extractor:
        inventory_monitor.run_inventory_monitor_cycle("atlas")

    extractor.assert_not_called()


def test_inventory_monitor_has_one_independent_scheduler_entrypoint():
    """Agent Loop recovery and compatibility monitor entrypoints are retired."""

    inventory_monitor = _inventory_monitor_module()
    assert hasattr(inventory_monitor, "InventoryMonitorService")
    assert hasattr(inventory_monitor, "run_inventory_monitor_cycle")


def test_inventory_monitor_defaults_to_authoritative_governance_action_service():
    """The scheduled monitor must use the audited versioned demotion path."""
    import importlib

    module = importlib.import_module("src.query_governance.inventory_monitor")
    authoritative = MagicMock()
    with patch("src.top_query.service.TopQueryGovernanceService", return_value=authoritative):
        service = module.InventoryMonitorService(
            reader=MagicMock(),
            operational_service=MagicMock(),
            task_reader=lambda: [],
            task_saver=MagicMock(),
        )
        service._wire_defaults()

    assert service.governance is authoritative


def test_scheduler_registers_only_the_independent_inventory_monitor_entrypoint():
    """Scheduler wiring must call the new thin wrapper, not legacy monitor jobs."""

    import importlib
    import inspect

    runner = importlib.import_module("src.scheduler.runner")
    assert callable(getattr(runner, "inventory_monitor_job", None))

    scheduler_source = inspect.getsource(runner.start_scheduler)
    assert "inventory_monitor_job" in scheduler_source
    assert "broken_rate_monitor_job" not in scheduler_source
    assert "agent_loop_inventory_recovery_job" not in scheduler_source


def test_scheduler_removes_previous_broken_rate_job_ids():
    """A scheduler refresh removes both historical inventory job prefixes."""

    import importlib

    runner = importlib.import_module("src.scheduler.runner")
    scheduler = MagicMock()
    stale_job = SimpleNamespace(id="broken_rate_monitor_job:atlas")
    scheduler.get_jobs.return_value = [stale_job]
    with patch.object(runner, "reload_config"), \
         patch("src.config.BRAND_CONFIGS", {"atlas": {"enabled": False}}), \
         patch("src.config.is_brand_enabled", return_value=False):
        runner.sync_inventory_monitor_jobs(scheduler)

    scheduler.remove_job.assert_called_once_with("broken_rate_monitor_job:atlas")


def _service_batch(*queries):
    specs = tuple(SimpleNamespace(query=query, query_id=f"id-{index}") for index, query in enumerate(queries))
    return SimpleNamespace(batch_id="batch-1", specs=specs)


def _service(**overrides):
    import importlib

    module = importlib.import_module("src.query_governance.inventory_monitor")
    defaults = {
        "reader": MagicMock(),
        "operational_service": MagicMock(),
        "governance_service": MagicMock(),
        "task_reader": lambda: [],
        "task_saver": MagicMock(),
        "task_stock_updater": MagicMock(),
        "task_status_updater": MagicMock(),
        "task_recoverer": MagicMock(),
        "action_logger": MagicMock(),
        "config_getter": lambda _brand: {
            "top_query_inventory_broken_rate_threshold": 50,
            "top_query_check_product_limit": 16,
            "top_query_inventory_max_cycles": 3,
        },
        "enabled_checker": lambda _brand: True,
        "supported_checker": lambda _brand: True,
    }
    defaults.update(overrides)
    return module.InventoryMonitorService(**defaults), defaults


def test_monitor_checks_bucket_before_querying_inventory():
    service, deps = _service()
    deps["reader"].get_latest.return_value = _service_batch("裙子")
    op = deps["operational_service"]
    op.search_products.return_value = [
        {"spuId": "protected", "ext": {"bucket": "productIdsByNoStock"}},
        {"spuId": "eligible", "productName": "Eligible", "ext": {"bucket": "productIdsByTagsAll"}},
    ]
    op.get_product_broken_rate.return_value = 0.8
    deps["governance_service"].execute_inventory_monitor_demotion.return_value = {
        "status": "verifying"
    }

    result = service.run("atlas")

    op.get_product_broken_rate.assert_called_once_with("atlas", op.search_products.return_value[1])
    deps["governance_service"].execute_inventory_monitor_demotion.assert_called_once()
    deps["action_logger"].assert_called_once_with({
        "brand": "atlas", "query": "裙子", "spu": "eligible", "name": "Eligible",
        "type": 2, "reason": "断码率自动降桶: 80%",
    })
    assert result["added_count"] == 1
    assert result["brands"]["atlas"]["protected_bucket_skip_count"] == 1


def test_disabled_inventory_stops_new_scans_but_recovers_existing_task():
    active = SimpleNamespace(
        id=12, brand="atlas", product_id="p-old", original_query="裙子",
        product_name="旧商品",
    )
    service, deps = _service(
        task_reader=lambda: [active],
        config_getter=lambda _brand: {
            "top_query_inventory_broken_rate_threshold": 50,
            "top_query_check_modes": {"inventory": "disabled"},
        },
    )
    deps["reader"].get_latest.return_value = _service_batch("裙子")
    operational = deps["operational_service"]
    operational.get_product_status_map.return_value = {
        "p-old": {"exists": True, "is_online": True},
    }
    operational.find_product_broken_rate.return_value = 0.1
    deps["governance_service"].execute_inventory_monitor_demotion.return_value = {
        "status": "verifying",
    }

    result = service.run("atlas")

    operational.search_products.assert_not_called()
    deps["task_saver"].assert_not_called()
    deps["task_recoverer"].assert_called_once_with(12, 10)
    assert result["recovered_count"] == 1


def test_monitor_exposes_action_failure_in_cycle_summary_and_keeps_no_task():
    service, deps = _service()
    deps["reader"].get_latest.return_value = _service_batch("衬衫 女士")
    deps["operational_service"].search_products.return_value = [
        {"spuId": "failed", "productName": "失败商品", "ext": {"bucket": "productIdsByTagsAll"}},
    ]
    deps["operational_service"].get_product_broken_rate.return_value = 0.8
    deps["governance_service"].execute_inventory_monitor_demotion.return_value = {
        "status": "error",
        "error_code": "INVENTORY_ACTION_FAILED",
        "error_message": "生产降桶写入被安全门禁阻断",
    }

    result = service.run("atlas")

    assert result["status"] == "error"
    assert result["brands"]["atlas"]["status"] == "error"
    assert result["brands"]["atlas"]["action_failures"] == [{
        "query": "衬衫 女士",
        "product_count": 1,
        "error_code": "INVENTORY_ACTION_FAILED",
        "error_message": "生产降桶写入被安全门禁阻断",
    }]
    deps["task_saver"].assert_not_called()


def test_monitor_batches_status_lookup_and_stops_missing_or_offline_tasks():
    task_online = SimpleNamespace(id=1, brand="atlas", product_id="online", original_query="q", product_name="Online")
    task_offline = SimpleNamespace(id=2, brand="atlas", product_id="offline", original_query="q", product_name="Offline")
    service, deps = _service(task_reader=lambda: [task_online, task_offline])
    deps["reader"].get_latest.return_value = _service_batch()
    op = deps["operational_service"]
    op.get_product_status_map.return_value = {
        "online": {"exists": True, "is_online": True},
        "offline": {"exists": True, "is_online": False},
    }

    service.run("atlas")

    op.get_product_status_map.assert_called_once_with("atlas", ["online", "offline"])
    deps["task_status_updater"].assert_called_once_with(2, "auto_stopped")


def test_monitor_keeps_tasks_when_batch_status_lookup_fails():
    """A failed batch status read must not mark products as stopped."""

    task = SimpleNamespace(id=3, brand="atlas", product_id="p1", original_query="q", product_name="P1")
    service, deps = _service(task_reader=lambda: [task])
    deps["reader"].get_latest.return_value = _service_batch()
    op = deps["operational_service"]
    op.get_product_status_map.return_value = None
    op.find_product_broken_rate.return_value = None

    result = service.run("atlas")

    assert result["status"] == "success"
    assert result["brands"]["atlas"]["warnings"] == [
        {
            "code": "PRODUCT_STATUS_LOOKUP_FAILED",
            "message": "商品状态批量查询失败，监测任务保持原状态",
        }
    ]
    deps["task_status_updater"].assert_not_called()


def test_monitor_recovers_task_without_a_current_query_batch():
    """Existing recovery tasks continue even when no persisted Top Query batch exists."""

    task = SimpleNamespace(id=8, brand="atlas", product_id="p1", original_query="q", product_name="P1")
    service, deps = _service(task_reader=lambda: [task])
    deps["reader"].get_latest.return_value = None
    op = deps["operational_service"]
    op.get_product_status_map.return_value = {"p1": {"exists": True, "is_online": True}}
    op.find_product_broken_rate.return_value = 0.2
    deps["governance_service"].execute_inventory_monitor_demotion.return_value = {
        "status": "verifying"
    }

    service.run("atlas")

    deps["task_recoverer"].assert_called_once_with(8, 20)


def test_monitor_skips_control_group_queries_without_searching():
    """A/B control queries are observed only and never receive inventory actions."""

    service, deps = _service(control_query_reader=lambda _brand: {"对照词"})
    deps["reader"].get_latest.return_value = _service_batch("对照词")

    service.run("atlas")

    deps["operational_service"].search_products.assert_not_called()
    deps["governance_service"].execute_inventory_monitor_demotion.assert_not_called()


def test_monitor_recovers_online_task_when_broken_rate_falls_below_threshold():
    task = SimpleNamespace(id=7, brand="atlas", product_id="p1", original_query="q", product_name="P1")
    service, deps = _service(task_reader=lambda: [task])
    deps["reader"].get_latest.return_value = _service_batch("q")
    op = deps["operational_service"]
    op.get_product_status_map.return_value = {"p1": {"exists": True, "is_online": True}}
    op.search_products.return_value = [{"spuId": "p1", "productName": "P1", "ext": {"bucket": "productIdsByTagsAll"}}]
    op.find_product_broken_rate.return_value = 0.2
    deps["governance_service"].execute_inventory_monitor_demotion.return_value = {
        "status": "verifying"
    }

    result = service.run("atlas")

    deps["governance_service"].execute_inventory_monitor_demotion.assert_called_once()
    deps["task_recoverer"].assert_called_once_with(7, 20)
    assert result["recovered_count"] == 1


def test_monitor_rechecks_only_new_products_after_successful_demotion():
    service, deps = _service()
    deps["reader"].get_latest.return_value = _service_batch("q")
    op = deps["operational_service"]
    op.search_products.side_effect = [
        [{"spuId": "p1", "productName": "P1", "ext": {"bucket": "productIdsByTagsAll"}}],
        [
            {"spuId": "p1", "productName": "P1", "ext": {"bucket": "productIdsByTagsAll"}},
            {"spuId": "p2", "productName": "P2", "ext": {"bucket": "productIdsByTagsAll"}},
        ],
        [],
    ]
    op.get_product_broken_rate.side_effect = [0.8, 0.7]
    deps["governance_service"].execute_inventory_monitor_demotion.return_value = {
        "status": "verifying"
    }

    result = service.run("atlas")

    assert op.get_product_broken_rate.call_count == 2
    assert deps["governance_service"].execute_inventory_monitor_demotion.call_count == 2
    assert result["added_count"] == 2


def test_monitor_honors_configured_maximum_replacement_cycles():
    service, deps = _service(
        config_getter=lambda _brand: {
            "top_query_inventory_broken_rate_threshold": 50,
            "top_query_check_product_limit": 16,
            "top_query_inventory_max_cycles": 1,
        }
    )
    deps["reader"].get_latest.return_value = _service_batch("q")
    deps["operational_service"].search_products.return_value = [
        {"spuId": "p1", "productName": "P1", "ext": {"bucket": "productIdsByTagsAll"}},
    ]
    deps["operational_service"].get_product_broken_rate.return_value = 0.8
    deps["governance_service"].execute_inventory_monitor_demotion.return_value = {
        "status": "verifying"
    }

    result = service.run("atlas")

    assert result["added_count"] == 1
    deps["operational_service"].search_products.assert_called_once_with(
        "atlas", "q", page_size=16
    )


def _governance_task(batch_id: str, brand: str, query: str):
    return QueryGovernanceTask(
        batch_id=batch_id, brand=brand, data_date=date.today(),
        query_text=query, query_rank=1, status="completed",
    )


def test_monitor_records_inventory_scan_events_with_model_fields(tmp_path):
    """每轮巡检事件必须真实落库，字段对齐 QueryGovernanceAgentEvent 模型。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as db:
        task = _governance_task("batch-1", "atlas", "裙子")
        db.add(task)
        db.commit()
        task_id = task.id

    service, deps = _service(session_factory=factory)
    deps["reader"].get_latest.return_value = _service_batch("裙子")
    op = deps["operational_service"]
    op.search_products.return_value = [
        {"spuId": "p1", "productName": "P1", "ext": {"bucket": "productIdsByTagsAll"}},
        {"spuId": "p2", "productName": "P2", "ext": {"bucket": "productIdsByTagsAll"}},
    ]
    op.get_product_broken_rate.side_effect = [0.8, 0.2]
    deps["governance_service"].execute_inventory_monitor_demotion.return_value = {
        "status": "verifying",
    }

    result = service.run("atlas")

    assert result["status"] == "success"
    with Session(engine) as db:
        events = db.query(QueryGovernanceAgentEvent).filter(
            QueryGovernanceAgentEvent.event_type == "inventory_scan"
        ).all()
        assert len(events) == 1
        ev = events[0]
        assert ev.task_id == task_id
        assert ev.brand == "atlas"
        assert ev.query_text == "裙子"
        assert ev.agent_name == "inventory_monitor"
        assert ev.node_key == "inventory"
        payload = json.loads(ev.output_json)
        assert payload == {
            "checkedAt": ev.created_at.strftime("%Y-%m-%d %H:%M"),
            "brokenProductCount": 1,
            "normalProductCount": 1,
            "recoveredProductCount": 0,
        }
    engine.dispose()


def test_monitor_isolates_per_query_demotion_exception_and_still_records_events(tmp_path):
    """单 Query 降桶动作抛异常时，其余 Query 继续处理且巡检事件照常写入。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'isolation.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as db:
        bad = _governance_task("batch-1", "atlas", "坏词")
        good = _governance_task("batch-1", "atlas", "好词")
        db.add_all([bad, good])
        db.commit()
        bad_id, good_id = bad.id, good.id

    service, deps = _service(session_factory=factory)
    deps["reader"].get_latest.return_value = _service_batch("坏词", "好词")
    op = deps["operational_service"]
    op.search_products.side_effect = lambda _brand, query, page_size=16: [
        {"spuId": f"{query}-p1", "productName": f"{query}商品", "ext": {"bucket": "productIdsByTagsAll"}},
    ]
    op.get_product_broken_rate.return_value = 0.8

    def demote(_brand, query, _candidates, recovery_candidates=None):
        if query == "坏词":
            raise RuntimeError(
                "query_demote authoritative config unavailable: authoritative_config_incomplete"
            )
        return {"status": "verifying"}

    gov = deps["governance_service"]
    gov.execute_inventory_monitor_demotion.side_effect = demote

    result = service.run("atlas")

    assert result["status"] == "error"
    assert result["added_count"] == 1
    assert result["error_code"] == "INVENTORY_ACTION_FAILED"
    assert "authoritative_config_incomplete" in result["error"]
    failures = result["brands"]["atlas"]["action_failures"]
    assert len(failures) == 1
    assert failures[0]["query"] == "坏词"
    assert "authoritative_config_incomplete" in failures[0]["error_message"]
    deps["task_saver"].assert_called_once()
    assert deps["task_saver"].call_args[0][0]["query"] == "好词"

    with Session(engine) as db:
        events = db.query(QueryGovernanceAgentEvent).filter(
            QueryGovernanceAgentEvent.event_type == "inventory_scan"
        ).all()
        assert {ev.task_id for ev in events} == {bad_id, good_id}
    engine.dispose()
