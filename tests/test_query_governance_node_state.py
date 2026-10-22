from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import Column, Date, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from src.query_governance.models import NodeStatus
from src.query_governance.state import (
    DeterministicNodeStateService,
    InMemoryNodeStateStore,
    InvalidNodeTransition,
    NodeIdentity,
    NodeStatePersistenceError,
    SqlAlchemyNodeStateStore,
)


DbBase = declarative_base()


class DbTask(DbBase):
    __tablename__ = "query_governance_task"

    id = Column(Integer, primary_key=True)
    batch_id = Column(String(120), nullable=False)
    brand = Column(String(50), nullable=False)
    query_text = Column(String(255), nullable=False)
    data_date = Column(Date, nullable=False)


class DbStep(DbBase):
    __tablename__ = "query_governance_step_status"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, nullable=False)
    node_key = Column(String(60), nullable=False)
    status = Column(String(30), nullable=False)
    result_json = Column(Text)
    error_code = Column(String(80))
    error_message = Column(Text)
    duration_seconds = Column(Float)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))


def build_service():
    store = InMemoryNodeStateStore()
    return DeterministicNodeStateService(store), store


def build_database_service():
    engine = create_engine("sqlite:///:memory:")
    DbBase.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        db.add(
            DbTask(
                batch_id="batch-1",
                brand="atlas",
                query_text="裙子",
                data_date=date(2026, 8, 28),
            )
        )
        db.commit()
    store = SqlAlchemyNodeStateStore(
        session_factory,
        task_model=DbTask,
        step_model=DbStep,
    )
    return DeterministicNodeStateService(store), store, engine


def identity() -> NodeIdentity:
    return NodeIdentity(
        batch_id="batch-1",
        brand="atlas",
        query="裙子",
        node="relevance",
    )


def test_node_is_created_pending_and_can_be_read_back():
    service, store = build_service()

    created = service.ensure_pending(identity())
    loaded = store.get(identity())

    assert created.status is NodeStatus.PENDING
    assert loaded == created
    assert loaded.batch_id == "batch-1"
    assert loaded.brand == "atlas"
    assert loaded.query == "裙子"
    assert loaded.node == "relevance"


def test_repeated_pending_creation_is_idempotent():
    service, store = build_service()
    key = identity()

    first = service.ensure_pending(key)
    second = service.ensure_pending(key)

    assert first == second
    assert store.get(key) == first


def test_running_to_executed_persists_result_and_duration():
    service, store = build_service()
    key = identity()
    service.ensure_pending(key)

    service.start(key)
    completed = service.complete(key, result={"checked": 10})
    loaded = store.get(key)

    assert completed.status is NodeStatus.EXECUTED
    assert completed.result == {"checked": 10}
    assert completed.started_at is not None
    assert completed.completed_at is not None
    assert completed.duration_seconds is not None
    assert completed.duration_seconds >= 0
    assert loaded == completed


def test_pending_can_be_skipped_with_a_human_readable_reason():
    service, store = build_service()
    key = identity()
    service.ensure_pending(key)

    skipped = service.skip(key, "没有需要治理的商品")

    assert skipped.status is NodeStatus.SKIPPED
    assert skipped.result == {"reason": "没有需要治理的商品"}
    assert skipped.started_at is not None
    assert store.get(key).completed_at is not None
    assert skipped.duration_seconds == 0


def test_error_requires_code_and_message_and_is_terminal():
    service, store = build_service()
    key = identity()
    service.ensure_pending(key)
    service.start(key)

    failed = service.fail(key, "MODEL_TIMEOUT", "相关性模型超时")

    assert failed.status is NodeStatus.ERROR
    assert failed.error_code == "MODEL_TIMEOUT"
    assert failed.error_message == "相关性模型超时"
    assert store.get(key).completed_at is not None


def test_completed_node_cannot_be_started_again():
    service, _ = build_service()
    key = identity()
    service.ensure_pending(key)
    service.start(key)
    service.complete(key)

    with pytest.raises(InvalidNodeTransition):
        service.start(key)

    with pytest.raises(InvalidNodeTransition):
        service.complete(key, result={"duplicate": True})


def test_invalid_status_transition_does_not_overwrite_persisted_state():
    service, store = build_service()
    key = identity()
    service.ensure_pending(key)

    with pytest.raises(InvalidNodeTransition):
        service.complete(key)

    assert store.get(key).status is NodeStatus.PENDING


def test_error_rejects_missing_error_details():
    service, _ = build_service()
    key = identity()
    service.ensure_pending(key)

    with pytest.raises(ValueError):
        service.fail(key, "", "说明")
    with pytest.raises(ValueError):
        service.fail(key, "FAILED", "")


def test_sqlalchemy_store_persists_and_reads_back_node_state():
    service, store, engine = build_database_service()
    key = identity()

    try:
        service.ensure_pending(key)
        service.start(key)
        service.complete(key, result={"checked": 10})
        loaded = store.get(key)
    finally:
        engine.dispose()

    assert loaded is not None
    assert loaded.status is NodeStatus.EXECUTED
    assert loaded.result == {"checked": 10}
    assert loaded.batch_id == key.batch_id
    assert loaded.query == key.query
    assert loaded.started_at is not None
    assert loaded.completed_at is not None


def test_sqlalchemy_store_requires_an_existing_query_task():
    engine = create_engine("sqlite:///:memory:")
    DbBase.metadata.create_all(engine)
    store = SqlAlchemyNodeStateStore(
        sessionmaker(bind=engine), task_model=DbTask, step_model=DbStep
    )

    try:
        with pytest.raises(NodeStatePersistenceError):
            store.save(service_record_for(identity()))
    finally:
        engine.dispose()


def service_record_for(key: NodeIdentity):
    from src.query_governance.state import NodeStateRecord

    return NodeStateRecord(identity=key)
