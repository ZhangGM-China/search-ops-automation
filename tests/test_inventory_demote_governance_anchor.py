"""断码降桶锚点契约：仅存在于治理主链的 Query 也能获得权威降桶锚点。

旧 Top Query 管线停写后，`inventory_monitor` 发现的断码 Query 不再有
`top_query_task` 行，权威降桶链路原先会直接拒绝执行。这里固定新行为：
有匹配 `query_governance_task` 时补建完成态锚点任务，动作仍走原有
审计/回读链路；两边都没有任务时继续拒绝。
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base, TopQueryActionPlan, TopQueryTask
from src.query_governance.models import QueryGovernanceTask
from src.top_query.service import ActionValidationError, TopQueryGovernanceService


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'demote-anchor.db'}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _service():
    return TopQueryGovernanceService(
        extractor=MagicMock(),
        workflow=MagicMock(),
        operational_service=MagicMock(),
        kg_client=MagicMock(),
        world_knowledge_service=MagicMock(),
    )


def _patched_session(factory):
    return (
        patch("src.top_query.service.SessionLocal", factory),
        patch(
            "src.top_query.service.assert_query_governance_allowed",
            lambda *_args, **_kwargs: None,
        ),
    )


def test_execute_inventory_monitor_demotion_anchors_on_governance_task(tmp_path):
    engine, factory = _factory(tmp_path)
    with factory() as db:
        db.add(
            QueryGovernanceTask(
                batch_id="query-governance:atlas:2026-10-12",
                brand="atlas",
                data_date=datetime.date(2026, 10, 12),
                query_text="断码裙 女士",
                status="completed",
            )
        )
        db.commit()

    service = _service()
    session_patch, policy_patch = _patched_session(factory)
    with session_patch, policy_patch, patch.object(
        service, "execute_approved_action", return_value={"status": "executed"}
    ):
        result = service.execute_inventory_monitor_demotion(
            "atlas",
            "断码裙 女士",
            [{"product_id": "P1", "product_name": "商品1", "broken_rate_percent": 80}],
        )

    assert result["action_id"] > 0
    with factory() as db:
        anchor = db.query(TopQueryTask).filter(TopQueryTask.brand == "atlas").one()
        assert anchor.status == "completed"
        assert anchor.data_date == datetime.date(2026, 10, 12)
        action = (
            db.query(TopQueryActionPlan)
            .filter(TopQueryActionPlan.id == result["action_id"])
            .one()
        )
        assert action.action_type == "query_demote"
        assert int(action.task_id) == int(anchor.id)
        # 请求 Query 经过治理规范化（去掉人群后缀）后落库
        assert action.query_text == "断码裙"
        assert anchor.query_text == "断码裙"
        assert action.check_key == "inventory"
    engine.dispose()


def test_execute_inventory_monitor_demotion_without_any_task_still_rejected(tmp_path):
    engine, factory = _factory(tmp_path)
    service = _service()
    session_patch, policy_patch = _patched_session(factory)
    with session_patch, policy_patch:
        with pytest.raises(ActionValidationError, match="visible top query task"):
            service.execute_inventory_monitor_demotion(
                "atlas",
                "无任务查询 女士",
                [{"product_id": "P1", "product_name": "商品1", "broken_rate_percent": 80}],
            )
    engine.dispose()


def test_execute_inventory_monitor_demotion_prefers_existing_top_query_task(tmp_path):
    engine, factory = _factory(tmp_path)
    with factory() as db:
        db.add(
            TopQueryTask(
                brand="atlas",
                data_date=datetime.date(2026, 9, 17),
                query_text="断码裙 女士",
                status="completed",
                display_status="normal",
            )
        )
        db.add(
            QueryGovernanceTask(
                batch_id="query-governance:atlas:2026-10-12",
                brand="atlas",
                data_date=datetime.date(2026, 10, 12),
                query_text="断码裙 女士",
                status="completed",
            )
        )
        db.commit()

    service = _service()
    session_patch, policy_patch = _patched_session(factory)
    with session_patch, policy_patch, patch.object(
        service, "execute_approved_action", return_value={"status": "executed"}
    ):
        result = service.execute_inventory_monitor_demotion(
            "atlas",
            "断码裙 女士",
            [{"product_id": "P1", "product_name": "商品1", "broken_rate_percent": 80}],
        )

    with factory() as db:
        assert db.query(TopQueryTask).filter(TopQueryTask.brand == "atlas").count() == 1
        action = (
            db.query(TopQueryActionPlan)
            .filter(TopQueryActionPlan.id == result["action_id"])
            .one()
        )
        task = db.get(TopQueryTask, action.task_id)
        assert task.data_date == datetime.date(2026, 9, 17)
    engine.dispose()


def test_anchor_inventory_demotion_task_targeted_sql_match(tmp_path):
    engine, factory = _factory(tmp_path)
    with factory() as db:
        db.add(
            QueryGovernanceTask(
                batch_id="query-governance:atlas:2026-10-15",
                brand="atlas",
                data_date=datetime.date(2026, 10, 15),
                query_text="断码长裤 男士",
                status="completed",
            )
        )
        db.commit()

    service = _service()
    with factory() as db:
        # query_key is "断码长裤", query_text is "断码长裤 男士"
        anchor = service._anchor_inventory_demotion_task(
            db,
            brand="atlas",
            query_key="断码长裤",
            query_text="断码长裤 男士",
        )
        assert anchor is not None
        assert anchor.data_date == datetime.date(2026, 10, 15)
        assert anchor.brand == "atlas"
        assert anchor.query_text == "断码长裤 男士"
    engine.dispose()


def test_anchor_inventory_demotion_task_fallback_scan_match(tmp_path):
    engine, factory = _factory(tmp_path)
    with factory() as db:
        # Non-standard spacing that won't match candidate_queries exactly,
        # but normalize_governance_query_key strips and matches.
        db.add(
            QueryGovernanceTask(
                batch_id="query-governance:atlas:2026-10-14",
                brand="atlas",
                data_date=datetime.date(2026, 10, 14),
                query_text="断码夹克   女士",
                status="completed",
            )
        )
        db.commit()

    service = _service()
    with factory() as db:
        anchor = service._anchor_inventory_demotion_task(
            db,
            brand="atlas",
            query_key="断码夹克",
            query_text="断码夹克",
        )
        assert anchor is not None
        assert anchor.data_date == datetime.date(2026, 10, 14)
        assert anchor.query_text == "断码夹克"
    engine.dispose()


def test_record_inventory_scan_events_lightweight_query(tmp_path):
    import json
    from src.query_governance.inventory_monitor import InventoryMonitorService
    from src.query_governance.models import QueryGovernanceAgentEvent

    engine, factory = _factory(tmp_path)
    with factory() as db:
        db.add(
            QueryGovernanceTask(
                id=101,
                batch_id="batch-scan-1",
                brand="atlas",
                data_date=datetime.date(2026, 10, 15),
                query_text="断码风衣 女士",
                status="completed",
            )
        )
        db.commit()

    monitor = InventoryMonitorService(session_factory=factory)
    query_stats = {
        "断码风衣 女士": {"broken": 2, "normal": 8, "recovered": 1},
    }
    now = datetime.datetime(2026, 10, 15, 14, 30)
    monitor._record_inventory_scan_events("batch-scan-1", "atlas", query_stats, now)

    with factory() as db:
        events = db.query(QueryGovernanceAgentEvent).filter(
            QueryGovernanceAgentEvent.task_id == 101
        ).all()
        assert len(events) == 1
        event = events[0]
        assert event.task_id == 101
        assert event.brand == "atlas"
        assert event.query_text == "断码风衣 女士"
        assert event.agent_name == "inventory_monitor"
        assert event.node_key == "inventory"
        assert event.event_type == "inventory_scan"
        payload = json.loads(event.output_json)
        assert payload["brokenProductCount"] == 2
        assert payload["normalProductCount"] == 8
        assert payload["recoveredProductCount"] == 1
    engine.dispose()

