from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.governance.action_gateway import ActionGateway
from src.persistence.models import Base
from src.query_governance.models import (
    QueryGovernanceActionPlan,
    QueryGovernanceAsyncTask,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
)
from src.query_governance.retag_verification import RelevanceRetagVerificationService


DATA_DATE = date(2026, 9, 20)
NOW = datetime(2026, 9, 20, 12, 0, 0)


class FakeOperational:
    def __init__(self, *, retag_result: object = True, search_results=None) -> None:
        self.retag_result = retag_result
        self.search_results = search_results or {}
        self.retag_calls: list[tuple[str, str | None]] = []
        self.search_calls: list[tuple[str, str, int]] = []

    def retag_products(self, brand: str, external_request_id: str | None = None) -> bool:
        self.retag_calls.append((brand, external_request_id))
        if isinstance(self.retag_result, BaseException):
            raise self.retag_result
        return bool(self.retag_result)

    def search_products_with_total(
        self, brand: str, query: str, page_size: int = 100, page_num: int = 1
    ):
        assert page_num == 1
        self.search_calls.append((brand, query, page_size))
        products = list(self.search_results.get(query, []))
        return {
            "success": True,
            "products": products,
            "total_count": len(products),
            "result_complete": True,
            "error_reason": None,
        }


def _factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _seed_query(
    factory,
    *,
    query: str = "连衣裙",
    kg_status: str = "verified",
    suffix: str = "1",
) -> int:
    with factory() as session:
        task = QueryGovernanceTask(
            batch_id=f"batch-{suffix}",
            brand="atlas",
            data_date=DATA_DATE,
            query_text=query,
            status="completed",
        )
        session.add(task)
        session.flush()
        session.add(
            QueryGovernanceStepStatus(
                task_id=task.id,
                node_key="relevance",
                status="executed",
                result_json=json.dumps(
                    {
                        "detected_product_ids": [f"bad-{suffix}", f"good-{suffix}"],
                        "relevant_product_ids": [f"good-{suffix}"],
                        "blacklist_candidates": [
                            {"product_id": f"bad-{suffix}", "reason": "不相关"}
                        ],
                    }
                ),
            )
        )
        session.add(
            QueryGovernanceActionPlan(
                task_id=task.id,
                batch_id=task.batch_id,
                brand=task.brand,
                query_text=task.query_text,
                node_key="query_governance.kg_agent",
                action_type="kg_plan",
                idempotency_key=f"kg-plan-{suffix}",
                status=kg_status,
                executor_type="ai_auto" if kg_status not in {"pending", "executing"} else None,
            )
        )
        session.commit()
        return int(task.id)


def _service(factory, operational: FakeOperational, *, now: datetime = NOW):
    return RelevanceRetagVerificationService(
        session_factory=factory,
        operational_service=operational,
        fact_gateway=ActionGateway(
            session_factory=factory,
            authorizer=lambda _intent: True,
            clock=lambda: now,
        ),
        clock=lambda: now,
    )


def test_retag_waits_until_all_daily_kg_plans_are_resolved():
    engine, factory = _factory()
    try:
        _seed_query(factory, kg_status="verified", suffix="1")
        _seed_query(factory, query="风衣", kg_status="pending", suffix="2")
        operational = FakeOperational()

        result = _service(factory, operational).trigger_if_ready("atlas", DATA_DATE)

        assert result == {
            "status": "waiting",
            "reason": "kg_actions_not_terminal",
            "pendingActionPlanIds": [2],
        }
        assert operational.retag_calls == []
    finally:
        engine.dispose()


def test_retag_waits_for_unprocessed_relevance_root_cause_task():
    engine, factory = _factory()
    try:
        _seed_query(factory, kg_status="verified", suffix="1")
        with factory() as session:
            task = session.query(QueryGovernanceTask).filter_by(batch_id="batch-1").one()
            session.add(
                QueryGovernanceAsyncTask(
                    task_id=task.id,
                    batch_id=task.batch_id,
                    brand=task.brand,
                    query_text=task.query_text,
                    trigger_source="relevance_root_cause",
                    idempotency_key="root-cause-pending-1",
                    status="pending",
                    evidence_json="{}",
                )
            )
            session.commit()
        result = _service(factory, FakeOperational()).trigger_if_ready("atlas", DATA_DATE)
        assert result["status"] == "waiting"
        assert result["reason"] == "root_cause_tasks_not_terminal"
    finally:
        engine.dispose()


def test_retag_requires_at_least_one_verified_kg_change():
    engine, factory = _factory()
    try:
        _seed_query(factory, kg_status="failed")
        operational = FakeOperational()

        result = _service(factory, operational).trigger_if_ready("atlas", DATA_DATE)

        assert result["status"] == "skipped"
        assert result["reason"] == "no_verified_kg_change"
        assert operational.retag_calls == []
    finally:
        engine.dispose()


def test_retag_is_one_daily_idempotent_action_and_sets_30_minute_verification_gate():
    engine, factory = _factory()
    try:
        _seed_query(factory)
        operational = FakeOperational()
        service = _service(factory, operational)

        first = service.trigger_if_ready("atlas", DATA_DATE)
        second = service.trigger_if_ready("atlas", DATA_DATE)

        assert first["status"] == "verified"
        assert first["triggeredAt"] == "2026-09-20T12:00:00"
        assert first["earliestVerificationAt"] == "2026-09-20T12:30:00"
        assert second == first
        assert len(operational.retag_calls) == 1
        assert operational.retag_calls[0][0] == "atlas"
        assert operational.retag_calls[0][1] == "query-governance:retag:atlas:2026-09-20"
        with factory() as session:
            plans = session.scalars(
                select(QueryGovernanceActionPlan).where(
                    QueryGovernanceActionPlan.action_type == "product_retag"
                )
            ).all()
            assert len(plans) == 1
            assert plans[0].status == "verified"
    finally:
        engine.dispose()


def test_verification_waits_30_minutes_without_live_search():
    engine, factory = _factory()
    try:
        task_id = _seed_query(factory)
        operational = FakeOperational()
        _service(factory, operational).trigger_if_ready("atlas", DATA_DATE)

        waiting = _service(
            factory, operational, now=NOW + timedelta(minutes=29, seconds=59)
        ).verify_if_due("atlas", DATA_DATE)

        assert waiting["status"] == "waiting"
        assert waiting["reason"] == "verification_not_due"
        assert operational.search_calls == []
        assert _service(factory, operational).summary_for_task(task_id)["status"] == "waiting"
    finally:
        engine.dispose()


def test_due_verification_compares_wrong_and_correct_products_and_persists_summary():
    engine, factory = _factory()
    try:
        task_id = _seed_query(factory)
        operational = FakeOperational(
            search_results={"连衣裙": [{"productId": "good-1"}, {"productId": "new-1"}]}
        )
        _service(factory, operational).trigger_if_ready("atlas", DATA_DATE)

        summary = _service(
            factory, operational, now=NOW + timedelta(minutes=30)
        ).verify_if_due("atlas", DATA_DATE)

        assert summary["status"] == "verified"
        assert summary["conclusion"] == "passed"
        assert summary["errorProductsDisappeared"] is True
        assert summary["correctProductsHarmed"] is False
        assert summary["beforeRecallCount"] == 2
        assert summary["afterRecallCount"] == 2
        assert summary["queries"] == [
            {
                "query": "连衣裙",
                "beforeRecallCount": 2,
                "afterRecallCount": 2,
                "errorProductIds": ["bad-1"],
                "remainingErrorProductIds": [],
                "correctProductIds": ["good-1"],
                "harmedCorrectProductIds": [],
                "errorProductsDisappeared": True,
                "correctProductsHarmed": False,
            }
        ]
        assert _service(factory, operational).summary_for_task(task_id) == summary
    finally:
        engine.dispose()


def test_due_verification_records_failed_business_outcome_without_changing_retag_fact():
    engine, factory = _factory()
    try:
        _seed_query(factory)
        operational = FakeOperational(search_results={"连衣裙": [{"productId": "bad-1"}]})
        _service(factory, operational).trigger_if_ready("atlas", DATA_DATE)

        summary = _service(
            factory, operational, now=NOW + timedelta(minutes=30)
        ).verify_if_due("atlas", DATA_DATE)

        assert summary["status"] == "failed"
        assert summary["conclusion"] == "failed"
        assert summary["remainingErrorProductIds"] == ["bad-1"]
        assert summary["harmedCorrectProductIds"] == ["good-1"]
        with factory() as session:
            plan = session.scalar(
                select(QueryGovernanceActionPlan).where(
                    QueryGovernanceActionPlan.action_type == "product_retag"
                )
            )
            assert plan.status == "verified"
    finally:
        engine.dispose()


def test_retag_failure_and_transport_unknown_keep_existing_status_semantics():
    engine, factory = _factory()
    try:
        _seed_query(factory, suffix="failure")
        failed = _service(factory, FakeOperational(retag_result=False)).trigger_if_ready(
            "atlas", DATA_DATE
        )
        assert failed["status"] == "failed"
        assert failed["errorCode"] == "RETAG_EXECUTION_FAILED"
    finally:
        engine.dispose()

    engine, factory = _factory()
    try:
        _seed_query(factory, suffix="unknown")
        unknown = _service(
            factory, FakeOperational(retag_result=TimeoutError("timed out"))
        ).trigger_if_ready("atlas", DATA_DATE)
        assert unknown["status"] == "execution_unknown"
        assert unknown["errorCode"] == "RETAG_EXECUTION_UNKNOWN"
    finally:
        engine.dispose()
