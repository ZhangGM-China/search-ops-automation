import json
import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.models import QueryGovernanceAsyncTask, QueryGovernanceTask
from src.query_governance.async_tasks import (
    AsyncTaskStatus,
    AsyncTaskSubmission,
    QueryGovernanceAsyncTaskRepository,
    QueryGovernanceAsyncTaskService,
)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[QueryGovernanceTask.__table__, QueryGovernanceAsyncTask.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        task = QueryGovernanceTask(
            batch_id="batch-1",
            brand="atlas",
            data_date=datetime.date(2026, 8, 28),
            query_text="裙子",
            status="pending",
        )
        session.add(task)
        session.commit()
    yield factory
    engine.dispose()


def _service(factory):
    return QueryGovernanceAsyncTaskService(
        QueryGovernanceAsyncTaskRepository(factory)
    )


def test_enqueue_persists_trigger_evidence_and_returns_task(session_factory):
    result = _service(session_factory).enqueue(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={"result_count": 0},
        idempotency_key="kg:batch-1:atlas:裙子:recall",
    )

    assert isinstance(result, AsyncTaskSubmission)
    assert result.created is True
    assert result.status is AsyncTaskStatus.PENDING
    assert result.task_id == 1

    with session_factory() as session:
        row = session.get(QueryGovernanceAsyncTask, result.task_id)
        assert row is not None
        assert json.loads(row.evidence_json) == {"result_count": 0}
        assert row.trigger_source == "recall_anomaly"


def test_enqueue_persists_langfuse_context_round_trip(session_factory):
    context = {
        "batch_trace_id": "batch-trace-1",
        "query_trace_id": "query-trace-1",
        "metadata": {
            "batch_id": "batch-1",
            "query_id": "q1",
            "node": "kg_agent",
        },
    }

    result = _service(session_factory).enqueue(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={"result_count": 0},
        idempotency_key="kg:batch-1:atlas:裙子:trace",
        langfuse_context=context,
    )

    record = _service(session_factory).get(result.task_id)
    assert record is not None
    assert record.langfuse_context == context
    with session_factory() as session:
        row = session.get(QueryGovernanceAsyncTask, result.task_id)
        assert json.loads(row.langfuse_context_json) == context


def test_repeated_idempotency_key_reuses_existing_task(session_factory):
    service = _service(session_factory)
    kwargs = dict(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={"result_count": 0},
        idempotency_key="kg:batch-1:atlas:裙子:recall",
    )

    first = service.enqueue(**kwargs)
    second = service.enqueue(**kwargs)

    assert first.task_id == second.task_id
    assert first.created is True
    assert second.created is False
    with session_factory() as session:
        assert session.query(QueryGovernanceAsyncTask).count() == 1


def test_same_idempotency_key_with_different_payload_is_rejected(session_factory):
    service = _service(session_factory)
    kwargs = dict(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={"result_count": 0},
        idempotency_key="kg:batch-1:atlas:裙子:recall",
    )
    service.enqueue(**kwargs)

    with pytest.raises(ValueError, match="idempotency key collision"):
        service.enqueue(**{**kwargs, "evidence": {"result_count": 1}})


def test_task_status_updates_persist_plan_and_error(session_factory):
    service = _service(session_factory)
    task = service.enqueue(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="relevance_blacklist_ratio",
        evidence={"blacklist_ratio": 0.75},
        idempotency_key="kg:batch-1:atlas:裙子:relevance",
    )

    processing = service.mark_processing(task.task_id)
    generated = service.record_plan(task.task_id, {"operation": "add_relation"})
    assert processing.status is AsyncTaskStatus.PROCESSING
    assert generated.status is AsyncTaskStatus.PLAN_GENERATED
    assert generated.plan == {"operation": "add_relation"}

    failed = service.enqueue(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={},
        idempotency_key="kg:batch-1:atlas:裙子:failed",
    )
    error = service.record_error(failed.task_id, "KG_AGENT_FAILED", "KG Agent 超时")
    assert error.status is AsyncTaskStatus.ERROR
    assert error.error_code == "KG_AGENT_FAILED"
    assert error.error_message == "KG Agent 超时"


def test_enqueue_database_failure_returns_error_without_raising():
    class BrokenRepository:
        def create_or_get(self, **_kwargs):
            raise RuntimeError("database unavailable")

    result = QueryGovernanceAsyncTaskService(BrokenRepository()).enqueue(
        task_id=1,
        batch_id="batch-1",
        brand="atlas",
        query_text="裙子",
        trigger_source="recall_anomaly",
        evidence={},
        idempotency_key="kg:batch-1:atlas:裙子:broken",
    )

    assert result.status is AsyncTaskStatus.ERROR
    assert result.created is False
    assert result.error_code == "ASYNC_TASK_PERSIST_FAILED"
    assert "database unavailable" in result.error_message
