import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from src.persistence import Base, get_engine


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


@pytest.fixture()
def runtime_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv(
        "AUTO_DB_URI_TEST",
        f"sqlite:///{tmp_path / 'runtime-action-facts.db'}",
    )
    sqlite_engine = get_engine()
    Base.metadata.create_all(bind=sqlite_engine)
    yield sqlite_engine
    sqlite_engine.dispose()


def _insert_intent(conn, **overrides):
    values = {
        "action_type": "inventory.switch",
        "idempotency_key": "request-1",
        "payload_hash": "a" * 64,
        "payload_json": "{}",
        "operator": "operator-1",
        "brand": "atlas",
        "release": "release-test",
        "permission": "execute_governance_actions",
        "status": "pending",
        "version": 0,
    }
    values.update(overrides)
    return conn.execute(text("""
        INSERT INTO action_intent (
            created_at, updated_at, action_type, idempotency_key, payload_hash,
            payload_json, operator, brand, release, permission, status, version
        ) VALUES (
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :action_type, :idempotency_key,
            :payload_hash, :payload_json, :operator, :brand, :release,
            :permission, :status, :version
        )
    """), values).lastrowid


def _insert_attempt(conn, intent_id, **overrides):
    values = {
        "intent_id": intent_id,
        "attempt_no": 1,
        "status": "persisted",
        "version": 0,
        "execution_generation": 0,
        "claim_token": None,
        "lease_expires_at": None,
        "finished_at": None,
        "external_request_id": "request-1",
        "response_summary": None,
    }
    values.update(overrides)
    return conn.execute(text("""
        INSERT INTO action_attempt (
            intent_id, attempt_no, created_at, started_at, finished_at, status,
            version, execution_generation, claim_token, lease_expires_at,
            external_request_id, response_summary
        ) VALUES (
            :intent_id, :attempt_no, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
            :finished_at, :status, :version, :execution_generation,
            :claim_token, :lease_expires_at, :external_request_id,
            :response_summary
        )
    """), values).lastrowid


def test_runtime_sqlite_engine_enables_foreign_keys(runtime_engine):
    with runtime_engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_runtime_sqlite_engine_rejects_dangling_intent_id(runtime_engine):
    with pytest.raises(IntegrityError), runtime_engine.begin() as conn:
        _insert_attempt(conn, intent_id=999)


def test_runtime_sqlite_engine_rejects_dangling_attempt_id(runtime_engine):
    with runtime_engine.begin() as conn:
        intent_id = _insert_intent(conn)

    with pytest.raises(IntegrityError), runtime_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_readback (
                intent_id, attempt_id, created_at, status, authoritative,
                observed_payload, fact_hash
            ) VALUES (
                :intent_id, 999, CURRENT_TIMESTAMP, 'success', 1, '{}', :fact_hash
            )
        """), {"intent_id": intent_id, "fact_hash": "c" * 64})

    with runtime_engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action_type", ""),
        ("action_type", "\t\n\r"),
        ("idempotency_key", "   "),
        ("payload_hash", "a" * 63),
        ("payload_hash", "z" * 64),
        ("payload_json", ""),
        ("payload_json", "not-json"),
        ("operator", ""),
        ("brand", "   "),
        ("release", ""),
        ("permission", ""),
        ("status", "running"),
        ("version", -1),
    ],
)
def test_action_intent_rejects_invalid_core_facts(engine, field, value):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_intent(conn, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_no", 0),
        ("status", "unknown"),
        ("version", -1),
        ("execution_generation", -1),
        ("external_request_id", ""),
        ("external_request_id", "\t\n\r"),
        ("external_request_id", None),
    ],
)
def test_action_attempt_rejects_invalid_core_facts(engine, field, value):
    with engine.begin() as conn:
        intent_id = _insert_intent(conn)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_attempt(conn, intent_id, **{field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "persisted", "execution_generation": 1},
        {"status": "executing", "execution_generation": 1},
        {
            "status": "executing",
            "execution_generation": 1,
            "claim_token": "\t\n\r",
            "lease_expires_at": "2026-08-06 12:00:00",
        },
        {
            "status": "execution_unknown",
            "execution_generation": 1,
            "claim_token": "stale-claim",
        },
        {
            "status": "reconciling",
            "execution_generation": 1,
            "claim_token": "readback-claim",
        },
        {"status": "success", "execution_generation": 1},
    ],
)
def test_action_attempt_rejects_invalid_state_fact_combinations(
    engine,
    overrides,
):
    with engine.begin() as conn:
        intent_id = _insert_intent(conn)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_attempt(conn, intent_id, **overrides)


@pytest.mark.parametrize(
    "status",
    ["pending", "success", "failed", "execution_unknown", "inconclusive"],
)
def test_action_intent_accepts_each_legal_status(engine, status):
    with engine.begin() as conn:
        _insert_intent(conn, status=status)


@pytest.mark.parametrize(
    ("status", "facts"),
    [
        ("persisted", {"execution_generation": 0}),
        (
            "executing",
            {
                "execution_generation": 1,
                "claim_token": "claim-executing",
                "lease_expires_at": "2026-08-06 12:00:00",
            },
        ),
        (
            "fencing",
            {
                "execution_generation": 1,
                "claim_token": "claim-fencing",
                "lease_expires_at": "2026-08-06 12:00:00",
            },
        ),
        ("execution_unknown", {"execution_generation": 1}),
        (
            "reconciling",
            {
                "execution_generation": 1,
                "claim_token": "claim-readback",
                "lease_expires_at": "2026-08-06 12:00:00",
            },
        ),
        ("success", {"execution_generation": 1, "finished_at": "2026-08-06 12:00:00"}),
        ("failed", {"execution_generation": 1, "finished_at": "2026-08-06 12:00:00"}),
        (
            "inconclusive",
            {"execution_generation": 1, "finished_at": "2026-08-06 12:00:00"},
        ),
    ],
)
def test_action_attempt_accepts_each_legal_state_fact_combination(
    engine,
    status,
    facts,
):
    with engine.begin() as conn:
        intent_id = _insert_intent(conn)
        _insert_attempt(conn, intent_id, status=status, **facts)


@pytest.mark.parametrize(
    ("status", "authoritative"),
    [
        ("success", 1),
        ("failed", 1),
        ("failed", 0),
        ("unavailable", 0),
    ],
)
def test_action_readback_accepts_each_legal_authority_combination(
    engine,
    status,
    authoritative,
):
    with engine.begin() as conn:
        intent_id = _insert_intent(conn)
        attempt_id = _insert_attempt(conn, intent_id)
        conn.execute(text("""
            INSERT INTO action_readback (
                intent_id, attempt_id, created_at, status, authoritative,
                observed_payload, fact_hash
            ) VALUES (
                :intent_id, :attempt_id, CURRENT_TIMESTAMP, :status,
                :authoritative, '{}', :fact_hash
            )
        """), {
            "intent_id": intent_id,
            "attempt_id": attempt_id,
            "status": status,
            "authoritative": authoritative,
            "fact_hash": "c" * 64,
        })


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_request_id", ""),
        ("execution_generation", 0),
        ("fence_claim_token", ""),
        ("status", "failed"),
        ("evidence_payload", ""),
        ("evidence_payload", "not-json"),
        ("fact_hash", "a" * 63),
        ("fact_hash", "z" * 64),
    ],
)
def test_action_fence_rejects_invalid_core_facts(engine, field, value):
    with engine.begin() as conn:
        intent_id = _insert_intent(conn)
        attempt_id = _insert_attempt(conn, intent_id)
    values = {
        "intent_id": intent_id,
        "attempt_id": attempt_id,
        "external_request_id": "request-1",
        "execution_generation": 1,
        "fence_claim_token": "claim-1",
        "status": "fenced",
        "evidence_payload": "{}",
        "fact_hash": "b" * 64,
    }
    values[field] = value
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_fence_evidence (
                intent_id, attempt_id, created_at, external_request_id,
                execution_generation, fence_claim_token, status,
                evidence_payload, fact_hash
            ) VALUES (
                :intent_id, :attempt_id, CURRENT_TIMESTAMP, :external_request_id,
                :execution_generation, :fence_claim_token, :status,
                :evidence_payload, :fact_hash
            )
        """), values)


@pytest.mark.parametrize(
    ("status", "authoritative", "observed_payload", "fact_hash"),
    [
        ("unknown", 0, "{}", "c" * 64),
        ("unavailable", 2, "{}", "c" * 64),
        ("unavailable", 1, "{}", "c" * 64),
        ("success", 0, "{}", "c" * 64),
        ("failed", 0, "", "c" * 64),
        ("failed", 0, "not-json", "c" * 64),
        ("failed", 0, "{}", "c" * 63),
        ("failed", 0, "{}", "z" * 64),
    ],
)
def test_action_readback_rejects_invalid_authoritative_facts(
    engine,
    status,
    authoritative,
    observed_payload,
    fact_hash,
):
    with engine.begin() as conn:
        intent_id = _insert_intent(conn)
        attempt_id = _insert_attempt(conn, intent_id)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_readback (
                intent_id, attempt_id, created_at, status, authoritative,
                observed_payload, fact_hash
            ) VALUES (
                :intent_id, :attempt_id, CURRENT_TIMESTAMP, :status,
                :authoritative, :observed_payload, :fact_hash
            )
        """), {
            "intent_id": intent_id,
            "attempt_id": attempt_id,
            "status": status,
            "authoritative": authoritative,
            "observed_payload": observed_payload,
            "fact_hash": fact_hash,
        })


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "pending"),
        ("terminal_code", ""),
        ("terminal_code", "\t\n\r"),
        ("result_json", ""),
        ("result_json", "not-json"),
        ("replay_result_json", ""),
        ("replay_result_json", "not-json"),
        ("fact_hash", "d" * 63),
        ("fact_hash", "z" * 64),
    ],
)
def test_action_terminal_fact_rejects_invalid_core_facts(engine, field, value):
    with engine.begin() as conn:
        intent_id = _insert_intent(conn)
    values = {
        "intent_id": intent_id,
        "status": "success",
        "terminal_code": "executor_succeeded",
        "result_json": "{}",
        "replay_result_json": "{}",
        "fact_hash": "d" * 64,
    }
    values[field] = value
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_terminal_fact (
                intent_id, created_at, status, terminal_code, result_json,
                replay_result_json, fact_hash
            ) VALUES (
                :intent_id, CURRENT_TIMESTAMP, :status, :terminal_code,
                :result_json, :replay_result_json, :fact_hash
            )
        """), values)


@pytest.mark.parametrize(
    ("status", "terminal_code"),
    [
        ("success", "executor_failed"),
        ("failed", "executor_succeeded"),
        ("inconclusive", "authoritative_readback_success"),
    ],
)
def test_action_terminal_fact_rejects_status_code_mismatch(
    engine,
    status,
    terminal_code,
):
    with engine.begin() as conn:
        intent_id = _insert_intent(conn)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_terminal_fact (
                intent_id, created_at, status, terminal_code, result_json,
                replay_result_json, fact_hash
            ) VALUES (
                :intent_id, CURRENT_TIMESTAMP, :status, :terminal_code,
                '{}', '{}', :fact_hash
            )
        """), {
            "intent_id": intent_id,
            "status": status,
            "terminal_code": terminal_code,
            "fact_hash": "d" * 64,
        })


def test_action_readback_rejects_attempt_from_another_intent(engine):
    with engine.begin() as conn:
        intent_id = _insert_intent(conn)
        other_intent_id = _insert_intent(
            conn,
            idempotency_key="request-2",
        )
        attempt_id = _insert_attempt(conn, intent_id)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_readback (
                intent_id, attempt_id, created_at, status, authoritative,
                observed_payload, fact_hash
            ) VALUES (
                :intent_id, :attempt_id, CURRENT_TIMESTAMP, 'success', 1,
                '{}', :fact_hash
            )
        """), {
            "intent_id": other_intent_id,
            "attempt_id": attempt_id,
            "fact_hash": "c" * 64,
        })


@pytest.mark.parametrize(
    "overrides",
    [
        {"use_other_intent": True},
        {"external_request_id": "other-request"},
        {"execution_generation": 2},
    ],
)
def test_action_fence_rejects_attempt_identity_drift(engine, overrides):
    with engine.begin() as conn:
        intent_id = _insert_intent(conn)
        other_intent_id = _insert_intent(
            conn,
            idempotency_key="request-2",
        )
        attempt_id = _insert_attempt(
            conn,
            intent_id,
            status="executing",
            execution_generation=1,
            claim_token="claim-1",
            lease_expires_at="2026-08-06 12:00:00",
        )
    values = {
        "intent_id": other_intent_id if overrides.get("use_other_intent") else intent_id,
        "attempt_id": attempt_id,
        "external_request_id": overrides.get("external_request_id", "request-1"),
        "execution_generation": overrides.get("execution_generation", 1),
        "fence_claim_token": "fence-claim",
        "status": "fenced",
        "evidence_payload": "{}",
        "fact_hash": "b" * 64,
    }

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO action_fence_evidence (
                intent_id, attempt_id, created_at, external_request_id,
                execution_generation, fence_claim_token, status,
                evidence_payload, fact_hash
            ) VALUES (
                :intent_id, :attempt_id, CURRENT_TIMESTAMP,
                :external_request_id, :execution_generation,
                :fence_claim_token, :status, :evidence_payload, :fact_hash
            )
        """), values)
