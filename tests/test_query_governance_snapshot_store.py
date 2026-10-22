import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.models import (
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
)
from src.query_governance.snapshot_store import SqlAlchemyWorkflowStateStore


def _session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            QueryGovernanceTask.__table__,
            QueryGovernanceStepStatus.__table__,
        ],
    )
    return engine, sessionmaker(bind=engine)


def _create_task(factory):
    with factory() as session:
        session.add(
            QueryGovernanceTask(
                batch_id="batch-1",
                brand="atlas",
                data_date=datetime.date(2026, 8, 28),
                query_text="裙子",
                status="running",
            )
        )
        session.commit()


def test_sqlalchemy_store_persists_s0_once_and_reads_authoritative_value():
    engine, factory = _session_factory()
    _create_task(factory)
    store = SqlAlchemyWorkflowStateStore(factory, batch_id="batch-1")
    original = {
        "captured_at": "2026-08-28T10:00:00+00:00",
        "products": [{"product_id": "P-1", "rank": 1}],
        "result_count": 1,
        "result_complete": True,
        "request_status": "success",
    }
    replacement = {**original, "products": [{"product_id": "P-2", "rank": 1}]}

    assert store.save_s0_if_absent("atlas", "裙子", original) == original
    assert store.save_s0_if_absent("atlas", "裙子", replacement) == original
    assert store.get_s0("atlas", "裙子") == original

    with factory() as session:
        task = session.query(QueryGovernanceTask).one()
        assert task.s0_snapshot is not None
        assert task.error_code is None
        assert task.error_message is None
    engine.dispose()


def test_sqlalchemy_store_keeps_failed_s0_retryable():
    engine, factory = _session_factory()
    _create_task(factory)
    store = SqlAlchemyWorkflowStateStore(factory, batch_id="batch-1")

    store.save_s0_failure("atlas", "裙子", "MIXED_FAILED", "temporary")

    assert store.get_s0("atlas", "裙子") is None
    with factory() as session:
        task = session.query(QueryGovernanceTask).one()
        assert task.error_code == "MIXED_FAILED"
        assert task.error_message == "temporary"
    engine.dispose()
