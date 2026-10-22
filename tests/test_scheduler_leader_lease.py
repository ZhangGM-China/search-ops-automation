import datetime
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.persistence import database
from src.persistence import (
    Base,
    SchedulerLeaderFenceError,
    SchedulerLeaderLease,
    SchedulerLeaderLeaseUnavailable,
    acquire_scheduler_leader_lease,
    release_scheduler_leader_lease,
    renew_scheduler_leader_lease,
    require_scheduler_leader_fence,
)
from src.migrations.release_schema import ensure_release_schema, verify_release_schema


@pytest.fixture()
def engine():
    sqlite_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(sqlite_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=sqlite_engine)
    yield sqlite_engine
    sqlite_engine.dispose()


def _session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def _dialect_db(name):
    return SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name=name))
    )


def _sqlite_file_engine(uri):
    sqlite_engine = create_engine(uri, connect_args={"timeout": 5})

    @event.listens_for(sqlite_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return sqlite_engine


def test_scheduler_leader_lease_acquire_renew_release_and_fence_contract(engine):
    Session = _session_factory(engine)
    with Session() as session:
        first = acquire_scheduler_leader_lease(
            session, leader_name="scheduler-main", owner_id="worker-a", lease_seconds=30
        )
        first_token = first.fencing_token
        assert first.fencing_token == 1
        assert first.status == "active"
        assert first.acquired_at is not None
        assert first.heartbeat_at is not None
        assert first.lease_expires_at is not None
        assert first.acquired_at.tzinfo is None

        require_scheduler_leader_fence(
            session,
            leader_name="scheduler-main",
            owner_id="worker-a",
            fencing_token=first.fencing_token,
        )
        renewed = renew_scheduler_leader_lease(
            session,
            leader_name="scheduler-main",
            owner_id="worker-a",
            fencing_token=first.fencing_token,
            lease_seconds=30,
        )
        assert renewed.fencing_token == first.fencing_token
        released = release_scheduler_leader_lease(
            session,
            leader_name="scheduler-main",
            owner_id="worker-a",
            fencing_token=first.fencing_token,
        )
        assert released.status == "released"
        assert released.released_at is not None
        with pytest.raises(SchedulerLeaderFenceError):
            require_scheduler_leader_fence(
                session,
                leader_name="scheduler-main",
                owner_id="worker-a",
                fencing_token=first.fencing_token,
            )

        second = acquire_scheduler_leader_lease(
            session, leader_name="scheduler-main", owner_id="worker-b", lease_seconds=30
        )
        assert second.fencing_token == 2
        with pytest.raises(SchedulerLeaderFenceError):
            require_scheduler_leader_fence(
                session,
                leader_name="scheduler-main",
                owner_id="worker-a",
                fencing_token=first.fencing_token,
            )


def test_scheduler_leader_lease_expiry_takeover_rejects_old_fence(engine):
    Session = _session_factory(engine)
    with Session() as session:
        first = acquire_scheduler_leader_lease(
            session, leader_name="scheduler-main", owner_id="worker-a", lease_seconds=30
        )
        first_token = first.fencing_token
        session.execute(text(
            "UPDATE scheduler_leader_lease SET lease_expires_at = :expired "
            "WHERE leader_name = :leader_name"
        ), {
            "expired": datetime.datetime(2000, 1, 1),
            "leader_name": "scheduler-main",
        })
        session.commit()

        second = acquire_scheduler_leader_lease(
            session, leader_name="scheduler-main", owner_id="worker-b", lease_seconds=30
        )
        assert second.fencing_token == first_token + 1
        with pytest.raises(SchedulerLeaderFenceError):
            renew_scheduler_leader_lease(
                session,
                leader_name="scheduler-main",
                owner_id="worker-a",
                fencing_token=first_token,
                lease_seconds=30,
            )
        with pytest.raises(SchedulerLeaderFenceError):
            require_scheduler_leader_fence(
                session,
                leader_name="scheduler-main",
                owner_id="worker-a",
                fencing_token=first_token,
            )


def test_scheduler_leader_lease_concurrent_acquire_has_one_winner(tmp_path):
    engine = _sqlite_file_engine(f"sqlite:///{tmp_path / 'leader-lease.db'}")
    Base.metadata.create_all(bind=engine)
    Session = _session_factory(engine)

    def acquire(owner_id):
        with Session() as session:
            try:
                lease = acquire_scheduler_leader_lease(
                    session,
                    leader_name="scheduler-main",
                    owner_id=owner_id,
                    lease_seconds=30,
                )
                return ("won", lease.owner_id, lease.fencing_token)
            except SchedulerLeaderLeaseUnavailable:
                return ("lost", owner_id, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(acquire, ("worker-a", "worker-b")))
    assert [result[0] for result in results] == ["lost", "won"]
    assert results[1][2] == 1
    engine.dispose()


def test_scheduler_leader_lease_rejects_active_owner_and_stale_release(engine):
    Session = _session_factory(engine)
    with Session() as session:
        first = acquire_scheduler_leader_lease(
            session, leader_name="scheduler-main", owner_id="worker-a", lease_seconds=30
        )
        with pytest.raises(SchedulerLeaderLeaseUnavailable):
            acquire_scheduler_leader_lease(
                session, leader_name="scheduler-main", owner_id="worker-b", lease_seconds=30
            )
        with pytest.raises(SchedulerLeaderFenceError):
            release_scheduler_leader_lease(
                session,
                leader_name="scheduler-main",
                owner_id="worker-b",
                fencing_token=first.fencing_token,
            )


def test_scheduler_leader_lease_uses_database_expiry_at_acquire(
    engine,
    monkeypatch,
):
    Session = _session_factory(engine)
    stale_process_now = datetime.datetime(2000, 1, 1)
    with Session() as session:
        monkeypatch.setattr(
            database,
            "_scheduler_leader_db_now",
            lambda _db: stale_process_now,
            raising=False,
        )
        acquired = acquire_scheduler_leader_lease(
            session,
            leader_name="scheduler-main",
            owner_id="worker-a",
            lease_seconds=1,
        )
        assert acquired.lease_expires_at > datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        )
        require_scheduler_leader_fence(
            session,
            leader_name="scheduler-main",
            owner_id="worker-a",
            fencing_token=acquired.fencing_token,
        )



def test_scheduler_leader_lease_uses_database_expiry_at_renew(engine, monkeypatch):
    Session = _session_factory(engine)
    stale_process_now = datetime.datetime(2000, 1, 1)
    with Session() as session:
        acquired = acquire_scheduler_leader_lease(
            session,
            leader_name="scheduler-main",
            owner_id="worker-a",
            lease_seconds=30,
        )
        monkeypatch.setattr(
            database,
            "_scheduler_leader_db_now",
            lambda _db: stale_process_now,
            raising=False,
        )
        renewed = renew_scheduler_leader_lease(
            session,
            leader_name="scheduler-main",
            owner_id="worker-a",
            fencing_token=acquired.fencing_token,
            lease_seconds=1,
        )
        assert renewed.lease_expires_at > datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        )
        require_scheduler_leader_fence(
            session,
            leader_name="scheduler-main",
            owner_id="worker-a",
            fencing_token=renewed.fencing_token,
        )


def test_scheduler_leader_lease_database_expression_is_utc_or_fails_closed():
    for dialect_name in ("mysql", "mariadb"):
        expression = database._scheduler_leader_db_expiry_expression(
            _dialect_db(dialect_name),
            1,
        )
        assert "date_add(utc_timestamp()" in str(expression).lower()

    with pytest.raises(database.SchedulerLeaderLeaseError, match="unsupported"):
        database._scheduler_leader_db_expiry_expression(_dialect_db("postgresql"), 1)


def test_scheduler_leader_lease_migrates_persists_and_verifies(tmp_path):
    uri = f"sqlite:///{tmp_path / 'leader-lease-migration.db'}"
    first_engine = _sqlite_file_engine(uri)
    ensure_release_schema(first_engine)
    Session = _session_factory(first_engine)
    with Session() as session:
        first = acquire_scheduler_leader_lease(
            session, leader_name="scheduler-main", owner_id="worker-a", lease_seconds=30
        )
    first_engine.dispose()

    second_engine = _sqlite_file_engine(uri)
    second = ensure_release_schema(second_engine)
    with _session_factory(second_engine)() as session:
        persisted = session.get(SchedulerLeaderLease, first.id)
        assert persisted is not None
        assert persisted.fencing_token == 1
    assert second["addedColumns"] == []
    assert verify_release_schema(second_engine)["ok"] is True
    second_engine.dispose()


def test_scheduler_leader_lease_partial_table_is_upgraded(tmp_path):
    engine = _sqlite_file_engine(f"sqlite:///{tmp_path / 'leader-lease-partial.db'}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE scheduler_leader_lease (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                leader_name VARCHAR(120) NOT NULL
            )
        """))

    result = ensure_release_schema(engine)
    columns = {
        column["name"] for column in inspect(engine).get_columns("scheduler_leader_lease")
    }
    assert {
        "owner_id",
        "fencing_token",
        "status",
        "created_at",
        "acquired_at",
        "heartbeat_at",
        "lease_expires_at",
        "released_at",
    } <= columns
    assert result["addedColumns"]
    assert verify_release_schema(engine)["ok"] is True
    engine.dispose()
