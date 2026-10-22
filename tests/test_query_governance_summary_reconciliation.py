from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.models import (
    QueryGovernanceBatchSummary,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
)
from src.query_governance.scheduler import query_governance_summary_reconciliation_job
import src.query_governance.scheduler as scheduler_module


def _database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _seed_completed_batch(session_factory, *, brand="atlas", batch_id="batch-1"):
    with session_factory() as session:
        session.add(QueryGovernanceWorkflowRun(
            batch_id=batch_id,
            status="completed",
            finished_at=datetime.now() - timedelta(minutes=5),
        ))
        session.add(QueryGovernanceTask(
            batch_id=batch_id,
            brand=brand,
            data_date=date(2026, 9, 28),
            query_text="外套",
            query_rank=1,
            status="completed",
        ))
        session.commit()


def test_reconciliation_materializes_completed_batch_with_missing_summary_idempotently():
    engine, session_factory = _database()
    try:
        _seed_completed_batch(session_factory)

        first = query_governance_summary_reconciliation_job(
            session_factory=session_factory,
        )
        assert first["status"] == "success"
        assert first["repairedCount"] == 1
        assert first["failedCount"] == 0

        with session_factory() as session:
            summary = session.execute(select(QueryGovernanceBatchSummary)).scalar_one()
            assert summary.batch_id == "batch-1"
            assert summary.brand == "atlas"
            assert summary.data_date == date(2026, 9, 28)

        second = query_governance_summary_reconciliation_job(
            session_factory=session_factory,
        )
        assert second["status"] == "success"
        assert second["candidateCount"] == 0
        assert second["repairedCount"] == 0
    finally:
        engine.dispose()


def test_reconciliation_materializes_each_completed_batch_on_the_same_date():
    engine, session_factory = _database()
    try:
        _seed_completed_batch(session_factory, batch_id="batch-1")
        _seed_completed_batch(session_factory, batch_id="batch-2")

        first = query_governance_summary_reconciliation_job(
            session_factory=session_factory,
        )
        assert first["candidateCount"] == 2
        assert first["repairedCount"] == 2

        with session_factory() as session:
            summaries = session.execute(select(QueryGovernanceBatchSummary)).scalars().all()
            assert {summary.batch_id for summary in summaries} == {"batch-1", "batch-2"}

        second = query_governance_summary_reconciliation_job(
            session_factory=session_factory,
        )
        assert second["candidateCount"] == 0
    finally:
        engine.dispose()


def test_reconciliation_rebuilds_legacy_summary_without_filter_index():
    engine, session_factory = _database()
    try:
        _seed_completed_batch(session_factory)
        with session_factory() as session:
            session.add(QueryGovernanceBatchSummary(
                batch_id="batch-1",
                brand="atlas",
                data_date=date(2026, 9, 28),
                payload_json='{"queryCount": 1}',
            ))
            session.commit()

        first = query_governance_summary_reconciliation_job(
            session_factory=session_factory,
        )
        assert first["candidateCount"] == 1
        assert first["repairedCount"] == 1

        with session_factory() as session:
            summary = session.execute(select(QueryGovernanceBatchSummary)).scalar_one()
            assert '"filterTaskIds"' in summary.payload_json

        second = query_governance_summary_reconciliation_job(
            session_factory=session_factory,
        )
        assert second["candidateCount"] == 0
    finally:
        engine.dispose()


def test_reconciliation_reports_each_materialization_failure_without_aborting_batch():
    engine, session_factory = _database()
    try:
        _seed_completed_batch(session_factory)
        calls = []

        def materialize(brand, data_date):
            calls.append((brand, data_date))
            raise RuntimeError("summary backend unavailable")

        result = query_governance_summary_reconciliation_job(
            session_factory=session_factory,
            summary_materializer=materialize,
        )

        assert calls == [("atlas", date(2026, 9, 28))]
        assert result["status"] == "partial_failed"
        assert result["candidateCount"] == 1
        assert result["repairedCount"] == 0
        assert result["failedCount"] == 1
        assert result["failures"][0]["brand"] == "atlas"
    finally:
        engine.dispose()


def test_reconciliation_distinct_order_columns_are_selected_for_mysql():
    class InspectSession:
        def execute(self, query):
            sql = str(query.compile(dialect=mysql.dialect()))
            selected = sql.split(" FROM ", 1)[0]
            assert "query_governance_workflow_run.finished_at" in selected
            assert "query_governance_workflow_run.id" in selected
            return self

        def all(self):
            return []

    @contextmanager
    def session_factory():
        yield InspectSession()

    result = query_governance_summary_reconciliation_job(
        session_factory=session_factory,
        summary_materializer=lambda _brand, _date: None,
    )
    assert result["candidateCount"] == 0


def test_recovery_resumes_stale_workflow_run_with_original_scope(monkeypatch):
    engine, session_factory = _database()
    try:
        now = datetime(2026, 10, 1, 12, 0, 0)
        with session_factory() as session:
            session.add(QueryGovernanceWorkflowRun(
                batch_id="query-governance:summit:2026-09-30",
                status="running",
                heartbeat_at=now - timedelta(minutes=31),
            ))
            session.add(QueryGovernanceTask(
                batch_id="query-governance:summit:2026-09-30",
                brand="summit",
                data_date=date(2026, 9, 30),
                query_text="徒步帽子",
                query_rank=27,
                status="running",
            ))
            session.commit()

        calls = []
        def resume(**kwargs):
            calls.append(kwargs)
            with session_factory() as session:
                run = session.scalars(select(QueryGovernanceWorkflowRun)).one()
                run.status = "completed"
                session.commit()
            return {"status": "success", "brands": {"summit": {
                "status": "completed", "batchId": "query-governance:summit:2026-09-30",
            }}}
        monkeypatch.setattr(
            scheduler_module,
            "query_governance_mainline_job",
            resume,
        )

        result = scheduler_module.query_governance_recovery_job(
            workflow_factory="workflow-factory",
            session_factory=session_factory,
            now=now,
            stale_after_minutes=30,
        )

        assert result == {
            "status": "success",
            "candidateCount": 1,
            "resumedCount": 1,
            "failedCount": 0,
            "failures": [],
            "skippedCount": 0,
            "skips": [],
            "noProgressCount": 0,
            "noProgress": [],
        }
        assert calls == [{
            "brand_filter": "summit",
            "target_date": date(2026, 9, 30),
            "workflow_factory": "workflow-factory",
            "session_factory": session_factory,
            "brand_lock_wait_seconds": 0,
        }]
    finally:
        engine.dispose()


def test_recovery_ignores_recent_workflow_heartbeat(monkeypatch):
    engine, session_factory = _database()
    try:
        now = datetime(2026, 10, 1, 12, 0, 0)
        with session_factory() as session:
            session.add(QueryGovernanceWorkflowRun(
                batch_id="query-governance:atlas:2026-09-30",
                status="running",
                heartbeat_at=now - timedelta(minutes=1),
            ))
            session.add(QueryGovernanceTask(
                batch_id="query-governance:atlas:2026-09-30",
                brand="atlas",
                data_date=date(2026, 9, 30),
                query_text="连衣裙",
                query_rank=1,
                status="running",
            ))
            session.commit()

        mainline = monkeypatch.setattr(
            scheduler_module,
            "query_governance_mainline_job",
            lambda **_kwargs: pytest.fail("recent workflow must not be resumed"),
        )
        del mainline

        result = scheduler_module.query_governance_recovery_job(
            workflow_factory="workflow-factory",
            session_factory=session_factory,
            now=now,
            stale_after_minutes=30,
        )

        assert result["candidateCount"] == 0
        assert result["resumedCount"] == 0
    finally:
        engine.dispose()
