import hashlib
import json
import queue
import threading
from datetime import datetime, timedelta

import httpx
import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from src.governance.action_gateway import (
    ActionAuthorizationError,
    ActionConflictError,
    ActionExecutionFailedError,
    ActionGateway,
    ActionGatewayError,
    ActionInProgressError,
    ActionIntent,
    ExecutionContext,
    canonical_payload,
)
from src.persistence import (
    ActionAttempt,
    ActionFenceEvidence,
    ActionIntent as ActionIntentModel,
    ActionReadback,
    ActionTerminalFact,
    Base,
    InventoryRecoveryTask,
    SessionLocal,
)


@pytest.fixture(autouse=True)
def clean_action_facts():
    db = SessionLocal()
    try:
        db.query(ActionTerminalFact).delete()
        db.query(ActionReadback).delete()
        db.query(ActionFenceEvidence).delete()
        db.query(ActionAttempt).delete()
        db.query(ActionIntentModel).delete()
        db.commit()
    finally:
        db.close()


def _intent(*, key="intent-1", payload=None):
    return ActionIntent(
        action_type="inventory.switch",
        idempotency_key=key,
        payload=payload or {"product_id": "sku-1", "enabled": False},
        operator="operator-1",
        brand="atlas",
        release="release-test",
        permission="execute_governance_actions",
    )


def _allow(_intent):
    return True


class _MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 5, 12, 0, 0)

    def __call__(self):
        return self.value

    def advance(self, *, seconds):
        self.value += timedelta(seconds=seconds)


class _SimulatedProcessCrash(BaseException):
    pass


def test_audit_persistence_failure_fails_closed_without_executor(monkeypatch):
    calls = []

    class FailingSession:
        def add(self, _value):
            pass

        def commit(self):
            raise RuntimeError("audit unavailable")

        def rollback(self):
            pass

        def close(self):
            pass

        def query(self, _model):
            class Query:
                def filter_by(self, **_filters):
                    return self

                def first(self):
                    return None

            return Query()

    gateway = ActionGateway(session_factory=FailingSession, authorizer=_allow)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        gateway.execute(_intent(key="audit-failure"), lambda _context: calls.append("external"))

    assert calls == []


def test_same_idempotency_key_and_payload_hash_reuses_terminal_result():
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    first = gateway.execute(
        _intent(key="reuse"),
        lambda _context: calls.append("external") or {"status": "success", "request_id": "r-1"},
    )
    second = gateway.execute(
        _intent(key="reuse"),
        lambda _context: calls.append("unexpected") or {"status": "success"},
    )

    assert first == second
    assert calls == ["external"]


def test_successful_local_transaction_requires_authoritative_readback_before_terminal():
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    result = gateway.execute(
        _intent(key="local-readback-success"),
        lambda context: calls.append(("executor", context.transaction is not None))
        or {"status": "success", "business": "committed"},
        readback=lambda context: calls.append(("readback", context.transaction is not None))
        or {"status": "success", "observed": "committed"},
        atomic_local_transaction=True,
        authoritative_readback_on_success=True,
    )

    assert result == {"status": "success", "business": "committed"}
    assert calls == [("executor", True), ("readback", True)]
    db = SessionLocal()
    try:
        readback = db.query(ActionReadback).one()
        assert readback.status == "success"
        assert readback.authoritative == 1
        assert db.query(ActionTerminalFact).one().status == "success"
    finally:
        db.close()


def test_successful_local_transaction_without_readback_fails_closed():
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    result = gateway.execute(
        _intent(key="local-readback-unavailable"),
        lambda _context: {"status": "success", "business": "must-rollback"},
        atomic_local_transaction=True,
        authoritative_readback_on_success=True,
    )

    assert result["status"] == "inconclusive"
    db = SessionLocal()
    try:
        readback = db.query(ActionReadback).one()
        assert readback.status == "unavailable"
        assert readback.authoritative == 0
        terminal = db.query(ActionTerminalFact).one()
        assert terminal.status == "inconclusive"
        assert terminal.terminal_code == "readback_unavailable"
        assert db.query(ActionAttempt).one().status == "inconclusive"
    finally:
        db.close()


def test_large_result_first_response_and_replay_are_strictly_equal():
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    executor_result = {
        "status": "success",
        "request_id": "large-result",
        "items": ["多字节结果🙂\\\"" * 12000],
    }

    first = gateway.execute(
        _intent(key="large-result-replay"),
        lambda _context: calls.append("executor") or executor_result,
    )
    replay = gateway.execute(
        _intent(key="large-result-replay"),
        lambda _context: calls.append("unexpected") or {"status": "failed"},
    )

    assert first == executor_result
    assert replay == first
    assert calls == ["executor"]
    db = SessionLocal()
    try:
        terminal = db.query(ActionTerminalFact).one()
        assert json.loads(terminal.replay_result_json) == first
        assert len(terminal.replay_result_json.encode("utf-8")) > 65535
        assert len(terminal.result_json) <= 4096
        assert len(terminal.result_json.encode("utf-8")) <= 4096
    finally:
        db.close()


def test_replay_result_uses_mysql_mediumtext_capacity():
    ddl = str(
        CreateTable(ActionTerminalFact.__table__).compile(dialect=mysql.dialect())
    )

    assert "replay_result_json MEDIUMTEXT NOT NULL" in ddl


def test_sensitive_result_first_response_and_replay_are_equal_without_secrets():
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    credential_url = (
        "https://" + "response-user" + ":" + "response-password" + "@example.com/result"
    )
    executor_result = {
        "status": "success",
        "token": "response-token-secret",
        "Authorization": "Bearer response-authorization-secret",
        "nested": {"password": "response-password-secret"},
        "url": credential_url,
    }

    first = gateway.execute(
        _intent(key="sensitive-result-replay"),
        lambda _context: executor_result,
    )
    replay = gateway.execute(
        _intent(key="sensitive-result-replay"),
        lambda _context: pytest.fail("terminal result must be replayed"),
    )

    assert replay == first
    assert first["token"] == "[REDACTED]"
    assert first["Authorization"] == "[REDACTED]"
    assert first["nested"]["password"] == "[REDACTED]"
    assert first["url"] == "https://[REDACTED]@example.com/result"
    encoded_response = json.dumps(first, ensure_ascii=False)
    for secret in (
        "response-user",
        "response-password",
        "response-token-secret",
        "response-authorization-secret",
        "response-password-secret",
    ):
        assert secret not in encoded_response


def test_same_idempotency_key_different_payload_hash_conflicts():
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    gateway.execute(_intent(key="conflict"), lambda _context: {"status": "success"})

    with pytest.raises(ActionConflictError, match="payload hash"):
        gateway.execute(
            _intent(key="conflict", payload={"product_id": "sku-2", "enabled": False}),
            lambda _context: pytest.fail("conflicting intent must not execute"),
        )


@pytest.mark.parametrize("field", ["brand", "operator", "action_type", "permission", "release"])
def test_same_key_payload_different_scope_facts_conflicts_without_replay(field):
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    gateway.execute(
        _intent(key="scope-conflict"),
        lambda _context: calls.append("first") or {"status": "success"},
    )

    values = {
        "brand": "meadow",
        "operator": "operator-2",
        "action_type": "prompt.publish",
        "permission": "manage_brand_config",
        "release": "release-other",
    }
    conflicting = ActionIntent(
        **{**_intent(key="scope-conflict").__dict__, field: values[field]},
    )
    with pytest.raises(ActionConflictError, match="scope"):
        gateway.execute(
            conflicting,
            lambda _context: calls.append("replayed") or {"status": "success"},
            readback=lambda _context: calls.append("readback") or {"status": "success"},
        )

    assert calls == ["first"]


def test_attempt_is_persisted_before_executor_call():
    observed = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    def executor(_context):
        db = SessionLocal()
        try:
            observed.append(db.query(ActionAttempt).count())
        finally:
            db.close()
        return {"status": "success"}

    gateway.execute(_intent(key="attempt"), executor)

    assert observed == [1]


def test_intent_and_attempt_prewrite_roll_back_together_on_attempt_add_failure():
    calls = []

    class FailingAttemptAddSession:
        def __init__(self):
            self._session = SessionLocal()

        def add(self, value):
            if isinstance(value, ActionAttempt):
                raise RuntimeError("attempt store unavailable")
            return self._session.add(value)

        def __getattr__(self, name):
            return getattr(self._session, name)

    gateway = ActionGateway(session_factory=FailingAttemptAddSession, authorizer=_allow)

    with pytest.raises(RuntimeError, match="attempt store unavailable"):
        gateway.execute(
            _intent(key="atomic-prewrite"),
            lambda _context: calls.append("executor"),
        )

    assert calls == []
    db = SessionLocal()
    try:
        assert db.query(ActionIntentModel).count() == 0
        assert db.query(ActionAttempt).count() == 0
    finally:
        db.close()


def test_authorizer_is_required_and_fail_closed():
    gateway = ActionGateway(session_factory=SessionLocal)

    with pytest.raises(ActionAuthorizationError, match="authorization denied"):
        gateway.execute(_intent(key="no-authorizer"), lambda _context: pytest.fail("must not execute"))


def test_timeout_uses_one_authoritative_readback_without_retry():
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    def executor(_context):
        calls.append("executor")
        raise TimeoutError("upstream timeout")

    def readback(_context):
        calls.append("readback")
        return {"status": "success", "request_id": "r-unknown"}

    result = gateway.execute(_intent(key="timeout"), executor, readback=readback)

    assert result["status"] == "success"
    assert calls == ["executor", "readback"]
    db = SessionLocal()
    try:
        row = db.query(ActionReadback).one()
        terminal = db.query(ActionTerminalFact).one()
        assert row.authoritative == 1
        assert row.status == "success"
        assert terminal.status == "success"
    finally:
        db.close()


def test_connection_failure_without_readback_is_inconclusive_and_not_retried():
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    def executor(_context):
        calls.append("executor")
        raise ConnectionError("connection lost")

    def readback(_context):
        calls.append("readback")
        raise ConnectionError("readback unavailable")

    result = gateway.execute(_intent(key="connection"), executor, readback=readback)

    assert result["status"] == "inconclusive"
    assert calls == ["executor", "readback"]
    db = SessionLocal()
    try:
        row = db.query(ActionReadback).one()
        assert row.authoritative == 0
        assert row.status == "failed"
        assert "readback unavailable" not in row.observed_payload
        assert "readback_failed" in row.observed_payload
    finally:
        db.close()


def test_missing_authoritative_readback_is_not_marked_authoritative():
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    result = gateway.execute(
        _intent(key="missing-readback"),
        lambda _context: (_ for _ in ()).throw(TimeoutError("timeout")),
    )

    assert result["status"] == "inconclusive"
    db = SessionLocal()
    try:
        row = db.query(ActionReadback).one()
        assert row.authoritative == 0
        assert row.status == "unavailable"
        assert "readback_unavailable" in row.observed_payload
    finally:
        db.close()


def test_external_success_terminal_persistence_failure_reconciles_without_replay(monkeypatch):
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    original_persist = ActionGateway._persist_terminal
    fail_once = {"value": True}

    def flaky_persist(db, intent_row, attempt, **kwargs):
        if fail_once["value"]:
            fail_once["value"] = False
            raise RuntimeError("terminal store unavailable")
        return original_persist(db, intent_row, attempt, **kwargs)

    monkeypatch.setattr(ActionGateway, "_persist_terminal", staticmethod(flaky_persist))

    result = gateway.execute(
        _intent(key="terminal-write-failure"),
        lambda _context: calls.append("executor") or {"status": "success", "request_id": "r-2"},
        readback=lambda _context: calls.append("readback") or {"status": "success", "request_id": "r-2"},
    )

    assert result["status"] == "success"
    assert calls == ["executor", "readback"]
    db = SessionLocal()
    try:
        assert db.query(ActionIntentModel).one().status == "success"
        assert db.query(ActionAttempt).one().status == "success"
        assert db.query(ActionReadback).one().authoritative == 1
        assert db.query(ActionTerminalFact).one().status == "success"
    finally:
        db.close()


def test_failed_readback_terminal_arbitration_rolls_back_and_recovers_without_replay(
    monkeypatch,
):
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    original_persist = ActionGateway._persist_terminal
    fail_once = {"value": True}

    def flaky_persist(db, intent_row, attempt, **kwargs):
        if fail_once["value"]:
            fail_once["value"] = False
            raise RuntimeError("terminal arbitration unavailable")
        return original_persist(db, intent_row, attempt, **kwargs)

    monkeypatch.setattr(ActionGateway, "_persist_terminal", staticmethod(flaky_persist))

    with pytest.raises(RuntimeError, match="terminal arbitration unavailable"):
        gateway.execute(
            _intent(key="atomic-arbitration"),
            lambda _context: calls.append("executor")
            or (_ for _ in ()).throw(TimeoutError("transport outcome unknown")),
            readback=lambda _context: calls.append("readback-1") or {"status": "success"},
        )

    db = SessionLocal()
    try:
        assert db.query(ActionIntentModel).one().status == "pending"
        assert db.query(ActionAttempt).one().status == "execution_unknown"
        assert db.query(ActionReadback).count() == 0
        assert db.query(ActionTerminalFact).count() == 0
    finally:
        db.close()


    result = gateway.execute(
        _intent(key="atomic-arbitration"),
        lambda _context: calls.append("executor-replay") or {"status": "success"},
        readback=lambda _context: calls.append("readback-2")
        or {"status": "success", "request_id": "recovered"},
    )

    assert result == {"status": "success", "request_id": "recovered"}
    assert calls == ["executor", "readback-1", "readback-2"]
    db = SessionLocal()
    try:
        assert db.query(ActionIntentModel).one().status == "success"
        assert db.query(ActionAttempt).one().status == "success"
        assert db.query(ActionReadback).count() == 1
        assert db.query(ActionTerminalFact).one().status == "success"
    finally:
        db.close()


def test_post_side_effect_crash_recovers_by_stale_readback_without_executor_replay(
    monkeypatch,
):
    calls = []
    fenced_request_ids = []
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=SessionLocal,
        authorizer=_allow,
        clock=clock,
        execution_lease_seconds=10,
        readback_lease_seconds=10,
    )
    original_persist = ActionGateway._persist_terminal
    crash_once = {"value": True}

    def crashing_persist(db, intent_row, attempt, **kwargs):
        if crash_once["value"]:
            crash_once["value"] = False
            raise _SimulatedProcessCrash("crash after external side effect")
        return original_persist(db, intent_row, attempt, **kwargs)

    monkeypatch.setattr(ActionGateway, "_persist_terminal", staticmethod(crashing_persist))

    with pytest.raises(_SimulatedProcessCrash):
        gateway.execute(
            _intent(key="post-side-effect-crash"),
            lambda _context: calls.append("executor")
            or {"status": "success", "request_id": "side-effect-1"},
            readback=lambda _context: calls.append("unexpected-readback"),
        )

    db = SessionLocal()
    try:
        attempt = db.query(ActionAttempt).one()
        assert attempt.status == "executing"
        assert attempt.lease_expires_at == clock.value + timedelta(seconds=10)
    finally:
        db.close()

    with pytest.raises(ActionInProgressError):
        gateway.execute(
            _intent(key="post-side-effect-crash"),
            lambda _context: calls.append("executor-replay"),
            readback=lambda _context: calls.append("early-readback") or {"status": "success"},
        )

    clock.advance(seconds=11)
    recovered = gateway.execute(
        _intent(key="post-side-effect-crash"),
        lambda _context: calls.append("executor-replay"),
        readback=lambda _context: calls.append("readback")
        or {"status": "success", "request_id": "side-effect-1"},
        stale_execution_fence=lambda context: fenced_request_ids.append(
            context.external_request_id
        )
        or {"status": "fenced", "method": "test_fence"},
    )

    assert recovered == {"status": "success", "request_id": "side-effect-1"}
    assert calls == ["executor", "readback"]
    assert fenced_request_ids == ["post-side-effect-crash"]


def test_stale_executor_owner_returns_winning_terminal_result_after_cas_loss():
    calls = []
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=SessionLocal,
        authorizer=_allow,
        clock=clock,
        execution_lease_seconds=10,
        readback_lease_seconds=10,
    )
    executor_started = threading.Event()
    release_executor = threading.Event()
    outcome = queue.Queue()

    def executor(_context):
        calls.append("executor")
        executor_started.set()
        assert release_executor.wait(timeout=5)
        return {"status": "success", "request_id": "old-owner"}

    def owner():
        try:
            outcome.put(
                (
                    "result",
                    gateway.execute(
                        _intent(key="stale-owner-cas"),
                        executor,
                        readback=lambda _context: calls.append("old-readback"),
                    ),
                )
            )
        except Exception as exc:
            outcome.put(("error", exc))

    owner_thread = threading.Thread(target=owner)
    owner_thread.start()
    try:
        assert executor_started.wait(timeout=5)
        clock.advance(seconds=11)
        winning_result = gateway.execute(
            _intent(key="stale-owner-cas"),
            lambda _context: calls.append("unexpected-executor"),
            readback=lambda _context: calls.append("winning-readback")
            or {"status": "success", "request_id": "winner"},
            stale_execution_fence=lambda _context: {
                "status": "fenced",
                "method": "test_fence",
            },
        )
        assert winning_result == {"status": "success", "request_id": "winner"}
        release_executor.set()
        owner_thread.join(timeout=5)
        assert not owner_thread.is_alive()
        owner_kind, owner_result = outcome.get(timeout=5)
        assert owner_kind == "result"
        assert owner_result == winning_result
        assert calls == ["executor", "winning-readback"]
    finally:
        release_executor.set()
        owner_thread.join(timeout=5)


def test_expired_lease_does_not_terminalize_while_slow_executor_can_still_write():
    calls = []
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=SessionLocal,
        authorizer=_allow,
        clock=clock,
        execution_lease_seconds=10,
        readback_lease_seconds=10,
    )
    executor_started = threading.Event()
    release_executor = threading.Event()
    outcome = queue.Queue()

    def executor(_context):
        calls.append("executor")
        executor_started.set()
        assert release_executor.wait(timeout=5)
        calls.append("external-write")
        return {"status": "success", "request_id": "slow-owner"}

    def owner():
        try:
            outcome.put(
                (
                    "result",
                    gateway.execute(
                        _intent(key="slow-executor-no-fence"),
                        executor,
                        readback=lambda _context: calls.append("owner-readback"),
                    ),
                )
            )
        except Exception as exc:
            outcome.put(("error", exc))

    owner_thread = threading.Thread(target=owner)
    owner_thread.start()
    try:
        assert executor_started.wait(timeout=5)
        clock.advance(seconds=11)

        with pytest.raises(ActionInProgressError, match="external fence"):
            gateway.execute(
                _intent(key="slow-executor-no-fence"),
                lambda _context: calls.append("unexpected-executor"),
                readback=lambda _context: calls.append("unsafe-readback")
                or {"status": "failed", "reason": "not_found"},
            )

        assert calls == ["executor"]
        release_executor.set()
        owner_thread.join(timeout=5)
        assert not owner_thread.is_alive()
        owner_kind, owner_result = outcome.get(timeout=5)
        assert owner_kind == "result"
        assert owner_result == {"status": "success", "request_id": "slow-owner"}
    finally:
        release_executor.set()
        owner_thread.join(timeout=5)


def test_stale_executor_deterministic_failure_returns_winning_terminal_result():
    calls = []
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=SessionLocal,
        authorizer=_allow,
        clock=clock,
        execution_lease_seconds=10,
        readback_lease_seconds=10,
    )
    executor_started = threading.Event()
    release_executor = threading.Event()
    outcome = queue.Queue()

    def executor(_context):
        calls.append("executor")
        executor_started.set()
        assert release_executor.wait(timeout=5)
        raise ValueError("old owner deterministic failure")

    def owner():
        try:
            outcome.put(
                (
                    "result",
                    gateway.execute(
                        _intent(key="stale-owner-deterministic-failure"),
                        executor,
                    ),
                )
            )
        except Exception as exc:
            outcome.put(("error", exc))

    owner_thread = threading.Thread(target=owner)
    owner_thread.start()
    try:
        assert executor_started.wait(timeout=5)
        clock.advance(seconds=11)
        winning_result = gateway.execute(
            _intent(key="stale-owner-deterministic-failure"),
            lambda _context: calls.append("unexpected-executor"),
            readback=lambda _context: calls.append("winning-readback")
            or {"status": "success", "request_id": "winner"},
            stale_execution_fence=lambda _context: {
                "status": "fenced",
                "method": "test_fence",
            },
        )
        assert winning_result == {"status": "success", "request_id": "winner"}

        release_executor.set()
        owner_thread.join(timeout=5)
        assert not owner_thread.is_alive()
        owner_kind, owner_result = outcome.get(timeout=5)
        assert owner_kind == "result"
        assert owner_result == winning_result
        assert calls == ["executor", "winning-readback"]
    finally:
        release_executor.set()
        owner_thread.join(timeout=5)


def test_unknown_marker_failure_recovers_after_execution_lease_without_replay(
    monkeypatch,
):
    calls = []
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=SessionLocal,
        authorizer=_allow,
        clock=clock,
        execution_lease_seconds=10,
        readback_lease_seconds=10,
    )
    original_mark_unknown = ActionGateway._mark_execution_unknown
    fail_once = {"value": True}

    def failing_mark_unknown(db, attempt, *, claim_token, expected_version):
        if fail_once["value"]:
            fail_once["value"] = False
            raise RuntimeError("execution unknown marker unavailable")
        return original_mark_unknown(
            db,
            attempt,
            claim_token=claim_token,
            expected_version=expected_version,
        )

    monkeypatch.setattr(
        ActionGateway,
        "_mark_execution_unknown",
        staticmethod(failing_mark_unknown),
    )

    with pytest.raises(RuntimeError, match="unknown marker unavailable"):
        gateway.execute(
            _intent(key="unknown-marker-failure"),
            lambda _context: calls.append("executor")
            or (_ for _ in ()).throw(TimeoutError("outcome unknown")),
            readback=lambda _context: calls.append("unexpected-readback"),
        )

    db = SessionLocal()
    try:
        assert db.query(ActionAttempt).one().status == "executing"
    finally:
        db.close()

    clock.advance(seconds=11)
    recovered = gateway.execute(
        _intent(key="unknown-marker-failure"),
        lambda _context: calls.append("executor-replay"),
        readback=lambda _context: calls.append("readback")
        or {"status": "success", "request_id": "unknown-recovered"},
        stale_execution_fence=lambda _context: {
            "status": "fenced",
            "method": "test_fence",
        },
    )

    assert recovered == {"status": "success", "request_id": "unknown-recovered"}
    assert calls == ["executor", "readback"]


def test_persisted_payload_and_result_facts_are_redacted_and_bounded():
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    payload_credential_url = (
        "https://" + "payload-user" + ":" + "payload-url-secret" + "@example.com/hook"
    )
    result_credential_url = (
        "https://" + "result-user" + ":" + "result-url-secret" + "@example.com/action"
    )
    payload = {
        "token": "payload-token-secret",
        "Authorization": "Bearer payload-authorization-secret",
        "password": "payload-password-secret",
        "callback_url": payload_credential_url,
    }
    observed = {
        "status": "success",
        "token": "result-token-secret",
        "Authorization": "Bearer result-authorization-secret",
        "password": "result-password-secret",
        "endpoint": result_credential_url,
        "blob": "🙂" * 5000,
    }

    result = gateway.execute(
        _intent(key="redacted-result-facts", payload=payload),
        lambda _context: (_ for _ in ()).throw(TimeoutError("outcome unknown")),
        readback=lambda _context: observed,
    )

    assert result == {
        "status": "success",
        "token": "[REDACTED]",
        "Authorization": "[REDACTED]",
        "password": "[REDACTED]",
        "endpoint": "https://[REDACTED]@example.com/action",
        "blob": "🙂" * 5000,
    }
    db = SessionLocal()
    try:
        intent_row = db.query(ActionIntentModel).one()
        attempt = db.query(ActionAttempt).one()
        readback = db.query(ActionReadback).one()
        terminal = db.query(ActionTerminalFact).one()
        fact_payloads = [
            intent_row.payload_json,
            attempt.response_summary,
            readback.observed_payload,
            terminal.result_json,
        ]
        for fact_payload in fact_payloads:
            assert len(fact_payload) <= 4096
            assert len(fact_payload.encode("utf-8")) <= 4096
            json.loads(fact_payload)
            assert "[REDACTED]" in fact_payload
        for secret in (
            "payload-token-secret",
            "payload-authorization-secret",
            "payload-password-secret",
            "payload-user",
            "payload-url-secret",
            "result-token-secret",
            "result-authorization-secret",
            "result-password-secret",
            "result-user",
            "result-url-secret",
        ):
            assert all(secret not in fact_payload for fact_payload in fact_payloads)
        assert json.loads(readback.observed_payload)["_truncated"] is True
        assert json.loads(terminal.result_json)["_truncated"] is True
    finally:
        db.close()


def test_persisted_executor_error_is_redacted_and_bounded():
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    error_credential_url = (
        "https://" + "error-user" + ":" + "error-url-secret" + "@example.com/action"
    )
    error_message = (
        "Authorization: Bearer error-authorization-secret "
        "token=error-token-secret password=error-password-secret "
        + error_credential_url
        + " "
        + "z" * 10000
    )

    with pytest.raises(RuntimeError):
        gateway.execute(
            _intent(key="redacted-error-fact"),
            lambda _context: (_ for _ in ()).throw(RuntimeError(error_message)),
        )

    db = SessionLocal()
    try:
        attempt = db.query(ActionAttempt).one()
        terminal = db.query(ActionTerminalFact).one()
        for fact_payload in (attempt.response_summary, terminal.result_json):
            assert len(fact_payload) <= 4096
            json.loads(fact_payload)
            assert "[REDACTED]" in fact_payload
            assert "error-authorization-secret" not in fact_payload
            assert "error-token-secret" not in fact_payload
            assert "error-password-secret" not in fact_payload
            assert "error-user" not in fact_payload
            assert "error-url-secret" not in fact_payload
            assert json.loads(fact_payload)["_truncated"] is True
    finally:
        db.close()


class _Http5xxError(RuntimeError):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _AmbiguousSdkError(RuntimeError):
    ambiguous_external = True


@pytest.mark.parametrize("error", [_Http5xxError(503), _AmbiguousSdkError()])
def test_explicit_ambiguous_external_failures_use_readback_without_retry(error):
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    def executor(_context):
        calls.append("executor")
        raise error

    result = gateway.execute(
        _intent(key=f"ambiguous-{type(error).__name__}"),
        executor,
        readback=lambda _context: calls.append("readback") or {"status": "failed", "reason": "not_found"},
    )

    assert result["status"] == "failed"
    assert calls == ["executor", "readback"]


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.Timeout("requests timeout"),
        requests.exceptions.ConnectionError("requests connection lost"),
        httpx.ReadTimeout(
            "httpx read timeout",
            request=httpx.Request("POST", "https://example.invalid/action"),
        ),
        httpx.ConnectError(
            "httpx connection lost",
            request=httpx.Request("POST", "https://example.invalid/action"),
        ),
    ],
)
def test_real_transport_failures_use_one_readback_without_executor_retry(error):
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    def executor(_context):
        calls.append("executor")
        raise error

    result = gateway.execute(
        _intent(key=f"real-transport-{type(error).__module__}-{type(error).__name__}"),
        executor,
        readback=lambda _context: calls.append("readback") or {"status": "success"},
    )

    assert result == {"status": "success"}
    assert calls == ["executor", "readback"]


def test_non_ambiguous_executor_failure_stays_failed_without_readback():
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    def executor(_context):
        calls.append("executor")
        raise ValueError("invalid deterministic request")

    with pytest.raises(ActionExecutionFailedError) as failure:
        gateway.execute(
            _intent(key="deterministic-failure"),
            executor,
            readback=lambda _context: calls.append("readback") or {"status": "success"},
        )

    assert failure.value.result == {
        "status": "failed",
        "error": "invalid deterministic request",
    }
    assert calls == ["executor"]
    db = SessionLocal()
    try:
        assert db.query(ActionIntentModel).one().status == "failed"
        assert db.query(ActionAttempt).one().status == "failed"
        assert db.query(ActionReadback).count() == 0
        assert db.query(ActionTerminalFact).one().terminal_code == "executor_failed"
    finally:
        db.close()


def test_deterministic_failure_first_and_replay_have_the_same_failure_contract():
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    with pytest.raises(ActionExecutionFailedError) as first_error:
        gateway.execute(
            _intent(key="deterministic-failure-replay"),
            lambda _context: calls.append("executor")
            or (_ for _ in ()).throw(ValueError("invalid deterministic request")),
        )

    with pytest.raises(ActionExecutionFailedError) as replay_error:
        gateway.execute(
            _intent(key="deterministic-failure-replay"),
            lambda _context: calls.append("executor-replay"),
        )

    assert str(replay_error.value) == str(first_error.value)
    assert replay_error.value.result == first_error.value.result
    assert calls == ["executor"]


def test_concurrent_identical_intents_execute_once_and_report_in_progress(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'action-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            ActionIntentModel.__table__,
            ActionAttempt.__table__,
            ActionReadback.__table__,
            ActionTerminalFact.__table__,
        ],
    )
    local_session = sessionmaker(bind=engine)
    initial_lookup_barrier = threading.Barrier(2)

    class RacingQuery:
        def __init__(self, query, owner):
            self._query = query
            self._owner = owner

        def filter_by(self, **filters):
            self._query = self._query.filter_by(**filters)
            return self

        def first(self):
            result = self._query.first()
            self._owner.initial_lookup_complete = True
            initial_lookup_barrier.wait(timeout=5)
            return result

    class RacingSession:
        def __init__(self):
            self._session = local_session()
            self.initial_lookup_complete = False

        def query(self, model):
            query = self._session.query(model)
            if model is ActionIntentModel and not self.initial_lookup_complete:
                return RacingQuery(query, self)
            return query

        def __getattr__(self, name):
            return getattr(self._session, name)

    calls = []
    executor_started = threading.Event()
    release_executor = threading.Event()
    outcomes = queue.Queue()
    gateway = ActionGateway(session_factory=RacingSession, authorizer=_allow)

    def executor(_context):
        calls.append("executor")
        executor_started.set()
        assert release_executor.wait(timeout=5)
        return {"status": "success", "request_id": "winner"}

    def worker():
        try:
            outcomes.put(("result", gateway.execute(_intent(key="race"), executor)))
        except Exception as exc:
            outcomes.put(("error", exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()

        assert executor_started.wait(timeout=5)
        first_kind, first_value = outcomes.get(timeout=5)
        assert first_kind == "error"
        assert isinstance(first_value, ActionInProgressError)
        assert "terminal fact" in str(first_value)

        release_executor.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()

        second_kind, second_value = outcomes.get(timeout=5)
        assert second_kind == "result"
        assert second_value == {"status": "success", "request_id": "winner"}
        assert calls == ["executor"]
    finally:
        release_executor.set()
        for thread in threads:
            thread.join(timeout=5)
        engine.dispose()


def _seed_unknown_action(tmp_path, *, key="unknown-race"):
    engine = create_engine(
        f"sqlite:///{tmp_path / f'{key}.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            ActionIntentModel.__table__,
            ActionAttempt.__table__,
            ActionReadback.__table__,
            ActionTerminalFact.__table__,
        ],
    )
    local_session = sessionmaker(bind=engine)
    intent = _intent(key=key)
    payload_json, payload_hash = canonical_payload(intent.payload)
    db = local_session()
    try:
        intent_row = ActionIntentModel(
            action_type=intent.action_type,
            idempotency_key=intent.idempotency_key,
            payload_hash=payload_hash,
            payload_json=payload_json,
            operator=intent.operator,
            brand=intent.brand,
            release=intent.release,
            permission=intent.permission,
            status="execution_unknown",
            version=1,
        )
        db.add(intent_row)
        db.flush()
        db.add(
            ActionAttempt(
                intent_id=intent_row.id,
                attempt_no=1,
                status="execution_unknown",
                version=1,
                execution_generation=1,
                started_at=datetime.now(),
                external_request_id=key,
            )
        )
        db.commit()
    finally:
        db.close()
    return engine, local_session


def _seed_cas_action(tmp_path, *, key, status="persisted"):
    engine = create_engine(
        f"sqlite:///{tmp_path / f'{key}.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            ActionIntentModel.__table__,
            ActionAttempt.__table__,
            ActionTerminalFact.__table__,
        ],
    )
    local_session = sessionmaker(bind=engine)
    intent = _intent(key=key)
    payload_json, payload_hash = canonical_payload(intent.payload)
    db = local_session()
    try:
        intent_row = ActionIntentModel(
            action_type=intent.action_type,
            idempotency_key=intent.idempotency_key,
            payload_hash=payload_hash,
            payload_json=payload_json,
            operator=intent.operator,
            brand=intent.brand,
            release=intent.release,
            permission=intent.permission,
            status="pending",
            version=0,
        )
        db.add(intent_row)
        db.flush()
        attempt_values = {
            "intent_id": intent_row.id,
            "attempt_no": 1,
            "status": status,
            "version": 0 if status == "persisted" else 1,
            "execution_generation": 0 if status == "persisted" else 1,
            "external_request_id": key,
        }
        if status == "executing":
            attempt_values.update({
                "claim_token": "claim-1",
                "lease_expires_at": datetime.now() + timedelta(minutes=1),
            })
        db.add(ActionAttempt(**attempt_values))
        db.commit()
    finally:
        db.close()
    return engine, local_session


@pytest.mark.parametrize(
    ("claim_method", "status", "version", "generation", "claim_token", "lease_expires_at", "expected_version"),
    [
        ("_claim_execution", "persisted", 0, 0, None, None, 1),
        ("_claim_fence", "executing", 1, 1, "old-execution-token", -1, 2),
        ("_claim_readback", "execution_unknown", 2, 1, None, None, 3),
    ],
)
def test_claim_context_reloads_database_fact_with_expire_on_commit_false(
    tmp_path,
    claim_method,
    status,
    version,
    generation,
    claim_token,
    lease_expires_at,
    expected_version,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / f'{claim_method[7:]}.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[ActionIntentModel.__table__, ActionAttempt.__table__],
    )
    local_session = sessionmaker(bind=engine, expire_on_commit=False)
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=local_session,
        authorizer=_allow,
        clock=clock,
        execution_lease_seconds=10,
        readback_lease_seconds=10,
        fence_lease_seconds=10,
    )
    seed = local_session()
    try:
        intent = _intent(key=f"claim-{claim_method[7:]}")
        payload_json, payload_hash = canonical_payload(intent.payload)
        intent_row = ActionIntentModel(
            action_type=intent.action_type,
            idempotency_key=intent.idempotency_key,
            payload_hash=payload_hash,
            payload_json=payload_json,
            operator=intent.operator,
            brand=intent.brand,
            release=intent.release,
            permission=intent.permission,
            status="pending",
            version=0,
        )
        seed.add(intent_row)
        seed.flush()
        seed.add(ActionAttempt(
            intent_id=intent_row.id,
            attempt_no=1,
            status=status,
            version=version,
            execution_generation=generation,
            claim_token=claim_token,
            lease_expires_at=(
                clock.value + timedelta(seconds=lease_expires_at)
                if lease_expires_at is not None
                else None
            ),
            started_at=clock.value,
            external_request_id=intent.idempotency_key,
        ))
        seed.commit()
    finally:
        seed.close()

    db = local_session()
    try:
        intent_row = db.query(ActionIntentModel).one()
        attempt = db.query(ActionAttempt).one()
        context = getattr(gateway, claim_method)(db, intent_row, attempt)
        assert context.attempt_version == expected_version
        assert context.attempt_id == attempt.id
        assert context.external_request_id == intent_row.idempotency_key
    finally:
        db.close()
        engine.dispose()


def test_sensitive_payload_values_conflict_without_persisting_secrets():
    one_json, one_hash = canonical_payload({"password": "one"})
    two_json, two_hash = canonical_payload({"password": "two"})

    assert one_hash != two_hash
    assert "one" not in one_json
    assert "two" not in two_json
    assert "[REDACTED]" in one_json

    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    gateway.execute(
        _intent(key="sensitive-idempotency", payload={"password": "one"}),
        lambda _context: calls.append("first") or {"status": "success"},
    )
    with pytest.raises(ActionConflictError):
        gateway.execute(
            _intent(key="sensitive-idempotency", payload={"password": "two"}),
            lambda _context: calls.append("second") or {"status": "success"},
        )
    assert calls == ["first"]
    db = SessionLocal()
    try:
        persisted = db.query(ActionIntentModel).one().payload_json
        assert "one" not in persisted
        assert "two" not in persisted
    finally:
        db.close()


def test_sensitive_payload_replays_across_jwt_secret_key_rotation(monkeypatch, caplog):
    sensitive_value = "same-test-value"
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    monkeypatch.setenv("SECRET_KEY", "rotation-key-a")
    first = gateway.execute(
        _intent(key="sensitive-key-rotation", payload={"password": sensitive_value}),
        lambda _context: calls.append("first") or {"status": "success"},
    )

    monkeypatch.setenv("SECRET_KEY", "rotation-key-b")
    replay = gateway.execute(
        _intent(key="sensitive-key-rotation", payload={"password": sensitive_value}),
        lambda _context: calls.append("unexpected") or {"status": "failed"},
    )

    assert replay == first
    assert calls == ["first"]
    db = SessionLocal()
    try:
        persisted = db.query(ActionIntentModel).one().payload_json
        assert sensitive_value not in persisted
        assert sensitive_value not in caplog.text
    finally:
        db.close()


def test_sensitive_payload_fact_uses_bounded_versioned_scrypt_envelope():
    sensitive_value = "low-entropy-value"
    collision_key = "__aiops_action_payload_fact_v1__"
    payload = {
        "password": sensitive_value,
        collision_key: {"password": "nested-sensitive-value"},
    }

    first_json, first_hash = canonical_payload(payload)
    second_json, second_hash = canonical_payload(payload)
    first = json.loads(first_json)
    envelope = first[collision_key]
    verifier = envelope["semantic_verifier"]

    assert set(first) == {collision_key}
    assert envelope["version"] == 1
    assert envelope["sanitized"]["password"] == "[REDACTED]"
    assert envelope["sanitized"][collision_key]["password"] == "[REDACTED]"
    assert verifier == {
        "algorithm": "scrypt",
        "dklen": 32,
        "digest": verifier["digest"],
        "n": 16384,
        "p": 1,
        "r": 8,
        "salt": verifier["salt"],
    }
    assert len(verifier["salt"]) == 32
    assert len(verifier["digest"]) == 64
    assert first_hash == hashlib.sha256(first_json.encode("utf-8")).hexdigest()
    assert first_hash != second_hash
    assert first_json != second_json
    assert len(first_json) <= 4096
    assert len(first_json.encode("utf-8")) <= 4096
    assert sensitive_value not in first_json
    assert "nested-sensitive-value" not in first_json


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "[REDACTED]"},
        {"password": ""},
        {"password": None},
        {"nested": {"password": "[REDACTED]"}},
        {"items": [{"token": "[REDACTED]"}]},
    ],
)
def test_sensitive_field_names_always_use_verifier_envelope(payload):
    payload_json, payload_hash = canonical_payload(payload)
    persisted = json.loads(payload_json)

    assert set(persisted) == {"__aiops_action_payload_fact_v1__"}
    assert payload_hash == hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "sensitive_text",
    [
        "https://" + "audit-user" + ":" + "audit-password" + "@example.com/path",
        "https://[REDACTED]@example.com/path",
        "Authorization: [REDACTED]",
        "token=[REDACTED]",
        "Bearer [REDACTED]",
        "Basic [REDACTED]",
    ],
)
def test_sensitive_text_semantics_use_verifier_even_when_already_redacted(
    sensitive_text,
):
    payload_json, _payload_hash = canonical_payload({"message": sensitive_text})

    assert set(json.loads(payload_json)) == {"__aiops_action_payload_fact_v1__"}


def test_context_free_redacted_audit_marker_remains_deterministic_non_sensitive():
    payload = {"message": "[REDACTED]"}

    first_json, first_hash = canonical_payload(payload)
    second_json, second_hash = canonical_payload(payload)

    assert "__aiops_action_payload_fact_v1__" not in json.loads(first_json)
    assert first_json == second_json
    assert first_hash == second_hash


def test_sensitive_detection_depth_limit_fails_closed():
    payload = {}
    cursor = payload
    for _index in range(66):
        cursor["nested"] = {}
        cursor = cursor["nested"]
    cursor["password"] = "deep-sensitive-value"

    with pytest.raises(ActionGatewayError, match="complexity limit"):
        canonical_payload(payload)


def test_oversized_sensitive_payload_fails_before_audit_or_executor():
    calls = []
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)

    with pytest.raises(ActionGatewayError, match="verifier limit") as error:
        gateway.execute(
            _intent(
                key="oversized-sensitive-payload",
                payload={"password": "x" * 65536},
            ),
            lambda _context: calls.append("executor"),
        )

    assert "x" * 100 not in str(error.value)
    assert calls == []
    db = SessionLocal()
    try:
        assert db.query(ActionIntentModel).count() == 0
        assert db.query(ActionAttempt).count() == 0
    finally:
        db.close()


def test_legacy_sensitive_payload_without_verifier_fails_closed():
    intent = _intent(key="legacy-sensitive", payload={"password": "legacy-value"})
    payload_json = json.dumps(
        {"password": "[REDACTED]"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    db = SessionLocal()
    try:
        db.add(ActionIntentModel(
            action_type=intent.action_type,
            idempotency_key=intent.idempotency_key,
            payload_hash=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            payload_json=payload_json,
            operator=intent.operator,
            brand=intent.brand,
            release=intent.release,
            permission=intent.permission,
            status="pending",
            version=0,
        ))
        db.commit()
    finally:
        db.close()

    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    with pytest.raises(ActionConflictError, match="payload") as error:
        gateway.execute(
            intent,
            lambda _context: pytest.fail("legacy sensitive fact must not execute"),
        )

    assert "legacy-value" not in str(error.value)


def test_concurrent_sensitive_intents_compare_winner_verifier_after_key_rotation(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sensitive-action-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            ActionIntentModel.__table__,
            ActionAttempt.__table__,
            ActionReadback.__table__,
            ActionTerminalFact.__table__,
        ],
    )
    local_session = sessionmaker(bind=engine)
    first_lookup = threading.Event()
    release_first_lookup = threading.Event()

    class RacingQuery:
        def __init__(self, query, owner):
            self._query = query
            self._owner = owner

        def filter_by(self, **filters):
            self._query = self._query.filter_by(**filters)
            return self

        def first(self):
            result = self._query.first()
            self._owner.initial_lookup_complete = True
            if self._owner.is_first:
                first_lookup.set()
                assert release_first_lookup.wait(timeout=5)
            else:
                release_first_lookup.set()
            return result

    session_count = {"value": 0}

    class RacingSession:
        def __init__(self):
            self._session = local_session()
            self.initial_lookup_complete = False
            session_count["value"] += 1
            self.is_first = session_count["value"] == 1

        def query(self, model):
            query = self._session.query(model)
            if model is ActionIntentModel and not self.initial_lookup_complete:
                return RacingQuery(query, self)
            return query

        def __getattr__(self, name):
            return getattr(self._session, name)

    calls = []
    executor_started = threading.Event()
    release_executor = threading.Event()
    outcomes = queue.Queue()
    gateway = ActionGateway(session_factory=RacingSession, authorizer=_allow)
    payload = {"password": "same-race-value"}

    def executor(_context):
        calls.append("executor")
        executor_started.set()
        assert release_executor.wait(timeout=5)
        return {"status": "success", "request_id": "winner"}

    def worker():
        try:
            outcomes.put(("result", gateway.execute(
                _intent(key="sensitive-race", payload=payload),
                executor,
            )))
        except Exception as exc:
            outcomes.put(("error", exc))

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    try:
        monkeypatch.setenv("SECRET_KEY", "rotation-key-a")
        first.start()
        assert first_lookup.wait(timeout=5)

        monkeypatch.setenv("SECRET_KEY", "rotation-key-b")
        second.start()
        assert executor_started.wait(timeout=5)

        first_kind, first_value = outcomes.get(timeout=5)
        assert first_kind == "error"
        assert isinstance(first_value, ActionInProgressError)

        release_executor.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive()
        assert not second.is_alive()

        second_kind, second_value = outcomes.get(timeout=5)
        assert second_kind == "result"
        assert second_value == {"status": "success", "request_id": "winner"}
        assert calls == ["executor"]
    finally:
        release_first_lookup.set()
        release_executor.set()
        first.join(timeout=5)
        second.join(timeout=5)
        engine.dispose()


def test_execution_claim_compares_stale_attempt_version_in_update_where(tmp_path):
    engine, local_session = _seed_cas_action(
        tmp_path,
        key="claim-version-cas",
    )
    stale_session = local_session()
    winner_session = local_session()
    try:
        intent_row = stale_session.query(ActionIntentModel).one()
        attempt = stale_session.query(ActionAttempt).one()
        winner_session.query(ActionAttempt).filter_by(id=attempt.id).update({
            ActionAttempt.version: 1,
        })
        winner_session.commit()

        gateway = ActionGateway(session_factory=local_session, authorizer=_allow)
        with pytest.raises(ActionInProgressError):
            gateway._claim_execution(stale_session, intent_row, attempt)

        stale_session.rollback()
        winner = winner_session.get(ActionAttempt, attempt.id)
        winner_session.refresh(winner)
        assert winner.status == "persisted"
        assert winner.version == 1
        assert winner.claim_token is None
    finally:
        stale_session.close()
        winner_session.close()
        engine.dispose()


def test_terminal_persist_compares_stale_intent_version_and_rolls_back(tmp_path):
    engine, local_session = _seed_cas_action(
        tmp_path,
        key="terminal-intent-version-cas",
        status="executing",
    )
    stale_session = local_session()
    winner_session = local_session()
    try:
        intent_row = stale_session.query(ActionIntentModel).one()
        attempt = stale_session.query(ActionAttempt).one()
        winner_session.query(ActionIntentModel).filter_by(id=intent_row.id).update({
            ActionIntentModel.version: 1,
        })
        winner_session.commit()

        with pytest.raises(ActionInProgressError):
            ActionGateway._persist_terminal(
                stale_session,
                intent_row,
                attempt,
                status="success",
                terminal_code="executor_succeeded",
                result={"status": "success"},
                expected_status="executing",
                claim_token="claim-1",
            )

        stale_session.rollback()
        winner_attempt = winner_session.get(ActionAttempt, attempt.id)
        winner_session.refresh(winner_attempt)
        assert winner_attempt.status == "executing"
        assert winner_attempt.version == 1
        assert winner_session.query(ActionTerminalFact).count() == 0
    finally:
        stale_session.close()
        winner_session.close()
        engine.dispose()


def test_concurrent_unknown_readback_has_one_database_claim_winner(tmp_path):
    engine, local_session = _seed_unknown_action(tmp_path, key="unknown-readback-race")
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=local_session,
        authorizer=_allow,
        clock=clock,
        readback_lease_seconds=10,
    )
    calls = []
    calls_lock = threading.Lock()
    readback_started = threading.Event()
    release_readback = threading.Event()
    outcomes = queue.Queue()

    def readback(_context):
        with calls_lock:
            calls.append("readback")
        readback_started.set()
        assert release_readback.wait(timeout=5)
        return {"status": "success", "request_id": "unknown-winner"}

    def worker():
        try:
            outcomes.put(
                (
                    "result",
                    gateway.execute(
                        _intent(key="unknown-readback-race"),
                        lambda _context: calls.append("executor") or {"status": "bad"},
                        readback=readback,
                    ),
                )
            )
        except Exception as exc:
            outcomes.put(("error", exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        assert readback_started.wait(timeout=5)
        first_kind, first_value = outcomes.get(timeout=2)
        assert first_kind == "error"
        assert isinstance(first_value, ActionInProgressError)
        release_readback.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        second_kind, second_value = outcomes.get(timeout=5)
        assert second_kind == "result"
        assert second_value == {"status": "success", "request_id": "unknown-winner"}
        assert calls == ["readback"]
    finally:
        release_readback.set()
        for thread in threads:
            thread.join(timeout=5)
        engine.dispose()


def test_stale_readback_owner_returns_winning_terminal_result_after_cas_loss(tmp_path):
    engine, local_session = _seed_unknown_action(tmp_path, key="stale-readback-cas")
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=local_session,
        authorizer=_allow,
        clock=clock,
        readback_lease_seconds=10,
    )
    calls = []
    readback_started = threading.Event()
    release_readback = threading.Event()
    outcome = queue.Queue()

    def stale_readback(_context):
        calls.append("stale-readback")
        readback_started.set()
        assert release_readback.wait(timeout=5)
        return {"status": "failed", "reason": "stale-observation"}

    def stale_owner():
        try:
            outcome.put(
                (
                    "result",
                    gateway.execute(
                        _intent(key="stale-readback-cas"),
                        lambda _context: calls.append("unexpected-executor"),
                        readback=stale_readback,
                    ),
                )
            )
        except Exception as exc:
            outcome.put(("error", exc))

    owner_thread = threading.Thread(target=stale_owner)
    owner_thread.start()
    try:
        assert readback_started.wait(timeout=5)
        clock.advance(seconds=11)
        winning_result = gateway.execute(
            _intent(key="stale-readback-cas"),
            lambda _context: calls.append("unexpected-executor"),
            readback=lambda _context: calls.append("winning-readback")
            or {"status": "success", "request_id": "winner"},
        )
        assert winning_result == {"status": "success", "request_id": "winner"}

        release_readback.set()
        owner_thread.join(timeout=5)
        assert not owner_thread.is_alive()
        owner_kind, owner_result = outcome.get(timeout=5)
        assert owner_kind == "result"
        assert owner_result == winning_result
        assert calls == ["stale-readback", "winning-readback"]
    finally:
        release_readback.set()
        owner_thread.join(timeout=5)
        engine.dispose()


def test_readback_claim_holder_crash_is_recovered_only_after_lease_expiry(tmp_path):
    engine, local_session = _seed_unknown_action(tmp_path, key="unknown-readback-crash")
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=local_session,
        authorizer=_allow,
        clock=clock,
        readback_lease_seconds=10,
    )
    calls = []

    with pytest.raises(_SimulatedProcessCrash):
        gateway.execute(
            _intent(key="unknown-readback-crash"),
            lambda _context: calls.append("executor"),
            readback=lambda _context: calls.append("readback-1")
            or (_ for _ in ()).throw(_SimulatedProcessCrash("readback owner crashed")),
        )

    db = local_session()
    try:
        attempt = db.query(ActionAttempt).one()
        assert attempt.status == "reconciling"
        assert attempt.lease_expires_at == clock.value + timedelta(seconds=10)
    finally:
        db.close()

    with pytest.raises(ActionInProgressError):
        gateway.execute(
            _intent(key="unknown-readback-crash"),
            lambda _context: calls.append("executor-replay"),
            readback=lambda _context: calls.append("early-recovery"),
        )

    clock.advance(seconds=11)
    recovered = gateway.execute(
        _intent(key="unknown-readback-crash"),
        lambda _context: calls.append("executor-replay"),
        readback=lambda _context: calls.append("readback-2")
        or {"status": "success", "request_id": "stale-recovered"},
    )

    assert recovered == {"status": "success", "request_id": "stale-recovered"}
    assert calls == ["readback-1", "readback-2"]
    engine.dispose()


def test_fact_hash_is_full_sanitized_canonical_not_audit_summary_hash():
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    observed = {
        "status": "success",
        "token": "fact-token-secret",
        "blob": "x" * 10000,
    }
    expected_full = {
        "status": "success",
        "token": "[REDACTED]",
        "blob": "x" * 10000,
    }
    expected_canonical = json.dumps(
        expected_full,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_hash = hashlib.sha256(expected_canonical.encode("utf-8")).hexdigest()

    gateway.execute(
        _intent(key="full-fact-hash"),
        lambda _context: (_ for _ in ()).throw(TimeoutError("unknown")),
        readback=lambda _context: observed,
    )

    db = SessionLocal()
    try:
        readback = db.query(ActionReadback).one()
        terminal = db.query(ActionTerminalFact).one()
        assert readback.fact_hash == expected_hash
        assert terminal.fact_hash == expected_hash
        assert json.loads(readback.observed_payload)["sha256"] == expected_hash
        assert json.loads(terminal.result_json)["sha256"] == expected_hash
        assert expected_hash != hashlib.sha256(
            terminal.result_json.encode("utf-8")
        ).hexdigest()
    finally:
        db.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "success", "token": "payload-secret"},
        {"status": "success", "token": "payload-secret", "blob": "x" * 10000},
    ],
)
def test_payload_hash_matches_persisted_sanitized_canonical_fact(payload):
    payload_json, payload_hash = canonical_payload(payload)
    persisted = json.loads(payload_json)

    if persisted.get("_truncated") is True:
        expected_hash = persisted["sha256"]
    else:
        expected_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    assert payload_hash == expected_hash
    assert "payload-secret" not in payload_json


def test_compensation_intent_keeps_parent_intent_link():
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    gateway.execute(_intent(key="original"), lambda _context: {"status": "success"})

    db = SessionLocal()
    try:
        parent_id = db.query(ActionIntentModel).filter_by(idempotency_key="original").one().id
    finally:
        db.close()

    compensation = _intent(
        key="compensation",
        payload={"product_id": "sku-1", "enabled": True},
    )
    compensation = ActionIntent(**{**compensation.__dict__, "parent_intent_id": parent_id})
    gateway.execute(compensation, lambda _context: {"status": "success"})

    db = SessionLocal()
    try:
        row = db.query(ActionIntentModel).filter_by(idempotency_key="compensation").one()
        assert row.parent_intent_id == parent_id
    finally:
        db.close()


def test_execution_context_is_shared_across_executor_fence_and_readback(monkeypatch):
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=SessionLocal,
        authorizer=_allow,
        clock=clock,
        execution_lease_seconds=10,
        readback_lease_seconds=10,
        fence_lease_seconds=10,
    )
    contexts = {}
    original_persist = ActionGateway._persist_terminal
    crash_once = {"value": True}

    def crashing_persist(db, intent_row, attempt, **kwargs):
        if crash_once["value"]:
            crash_once["value"] = False
            raise _SimulatedProcessCrash("crash after executor")
        return original_persist(db, intent_row, attempt, **kwargs)

    monkeypatch.setattr(ActionGateway, "_persist_terminal", staticmethod(crashing_persist))

    def executor(context: ExecutionContext):
        contexts["executor"] = context
        return {"status": "success"}

    with pytest.raises(_SimulatedProcessCrash):
        gateway.execute(_intent(key="shared-execution-context"), executor)

    clock.advance(seconds=11)

    def fence(context: ExecutionContext):
        contexts["fence"] = context
        return {
            "status": "fenced",
            "method": "test_atomic_fence",
            "external_request_id": context.external_request_id,
            "generation": context.generation,
        }

    def readback(context: ExecutionContext):
        contexts["readback"] = context
        return {"status": "success", "request_id": context.external_request_id}

    result = gateway.execute(
        _intent(key="shared-execution-context"),
        lambda _context: pytest.fail("executor must not replay"),
        readback=readback,
        stale_execution_fence=fence,
    )

    assert result == {"status": "success", "request_id": "shared-execution-context"}
    assert {context.external_request_id for context in contexts.values()} == {
        "shared-execution-context"
    }
    assert {context.generation for context in contexts.values()} == {1}
    assert contexts["executor"].claim_kind == "execution"
    assert contexts["fence"].claim_kind == "fence"
    assert contexts["readback"].claim_kind == "readback"
    assert len({context.claim_token for context in contexts.values()}) == 3

    db = SessionLocal()
    try:
        evidence = db.query(ActionFenceEvidence).one()
        assert evidence.external_request_id == "shared-execution-context"
        assert evidence.execution_generation == 1
        assert evidence.fence_claim_token == contexts["fence"].claim_token
        assert evidence.status == "fenced"
    finally:
        db.close()


def test_stale_fence_has_one_database_claim_winner_and_holds_no_transaction(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fence-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            ActionIntentModel.__table__,
            ActionAttempt.__table__,
            ActionFenceEvidence.__table__,
            ActionReadback.__table__,
            ActionTerminalFact.__table__,
        ],
    )
    local_session = sessionmaker(bind=engine)
    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=local_session,
        authorizer=_allow,
        clock=clock,
        execution_lease_seconds=10,
        readback_lease_seconds=10,
        fence_lease_seconds=10,
    )
    payload_json, payload_hash = canonical_payload(_intent(key="fence-race").payload)
    db = local_session()
    try:
        intent_row = ActionIntentModel(
            action_type="inventory.switch",
            idempotency_key="fence-race",
            payload_hash=payload_hash,
            payload_json=payload_json,
            operator="operator-1",
            brand="atlas",
            release="release-test",
            permission="execute_governance_actions",
            status="pending",
            version=0,
        )
        db.add(intent_row)
        db.flush()
        db.add(
            ActionAttempt(
                intent_id=intent_row.id,
                attempt_no=1,
                status="executing",
                version=1,
                execution_generation=1,
                claim_token="old-execution-token",
                lease_expires_at=clock.value - timedelta(seconds=1),
                external_request_id="fence-race",
                started_at=clock.value - timedelta(seconds=20),
            )
        )
        db.commit()
    finally:
        db.close()

    fence_started = threading.Event()
    release_fence = threading.Event()
    outcomes = queue.Queue()
    calls = []

    def fence(context: ExecutionContext):
        probe = local_session()
        try:
            calls.append(context.claim_token)
            assert context.transaction is None
            assert probe.in_transaction() is False
            fence_started.set()
            assert release_fence.wait(timeout=5)
            return {"status": "fenced", "method": "test_atomic_fence"}
        finally:
            probe.close()

    def worker():
        try:
            outcomes.put(
                (
                    "result",
                    gateway.execute(
                        _intent(key="fence-race"),
                        lambda _context: pytest.fail("executor must not replay"),
                        readback=lambda context: {
                            "status": "success",
                            "request_id": context.external_request_id,
                        },
                        stale_execution_fence=fence,
                    ),
                )
            )
        except Exception as exc:
            outcomes.put(("error", exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        assert fence_started.wait(timeout=5)
        first_kind, first_value = outcomes.get(timeout=2)
        assert first_kind == "error"
        assert isinstance(first_value, ActionInProgressError)
        release_fence.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        second_kind, second_value = outcomes.get(timeout=5)
        assert second_kind == "result"
        assert second_value == {"status": "success", "request_id": "fence-race"}
        assert len(calls) == 1
    finally:
        release_fence.set()
        for thread in threads:
            thread.join(timeout=5)
        engine.dispose()


def test_terminal_null_winner_is_distinct_from_missing_terminal(monkeypatch):
    gateway = ActionGateway(session_factory=SessionLocal, authorizer=_allow)
    original_persist = ActionGateway._persist_terminal
    arbitrate_once = {"value": True}

    def winner_persist(db, intent_row, attempt, **kwargs):
        if arbitrate_once["value"]:
            arbitrate_once["value"] = False
            original_persist(
                db,
                intent_row,
                attempt,
                status="success",
                terminal_code="executor_succeeded",
                result=None,
                expected_status=kwargs["expected_status"],
                claim_token=kwargs["claim_token"],
            )
            raise ActionInProgressError("simulated loser after null winner")
        return original_persist(db, intent_row, attempt, **kwargs)

    monkeypatch.setattr(ActionGateway, "_persist_terminal", staticmethod(winner_persist))

    result = gateway.execute(
        _intent(key="null-terminal-winner"),
        lambda _context: {"status": "stale-owner"},
    )

    assert result is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_lease_seconds", 0),
        ("execution_lease_seconds", -1),
        ("execution_lease_seconds", True),
        ("readback_lease_seconds", 0),
        ("fence_lease_seconds", 0),
    ],
)
def test_lease_configuration_requires_positive_integer(field, value):
    with pytest.raises(ValueError, match=field):
        ActionGateway(authorizer=_allow, **{field: value})


def test_atomic_local_side_effect_is_rolled_back_after_stale_fence(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'atomic-local-fence.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            InventoryRecoveryTask.__table__,
            ActionIntentModel.__table__,
            ActionAttempt.__table__,
            ActionFenceEvidence.__table__,
            ActionReadback.__table__,
            ActionTerminalFact.__table__,
        ],
    )
    local_session = sessionmaker(bind=engine)
    seed = local_session()
    try:
        task = InventoryRecoveryTask(
            brand="atlas",
            product_id="atomic-sku",
            product_name="Atomic",
            original_query="atomic-query",
            status="monitoring",
        )
        seed.add(task)
        seed.commit()
        task_id = int(task.id)
    finally:
        seed.close()

    clock = _MutableClock()
    gateway = ActionGateway(
        session_factory=local_session,
        authorizer=_allow,
        clock=clock,
        execution_lease_seconds=10,
        readback_lease_seconds=10,
        fence_lease_seconds=10,
    )
    executor_started = threading.Event()
    release_executor = threading.Event()
    outcome = queue.Queue()

    def executor(context: ExecutionContext):
        assert context.transaction is not None
        current = context.transaction.get(InventoryRecoveryTask, task_id)
        current.status = "manually_stopped"
        current.last_action_request_id = context.external_request_id
        current.last_action_generation = context.generation
        executor_started.set()
        assert release_executor.wait(timeout=5)
        return {"status": "success"}

    def owner():
        try:
            outcome.put(
                (
                    "result",
                    gateway.execute(
                        _intent(key="atomic-local-stale-owner"),
                        executor,
                        atomic_local_transaction=True,
                    ),
                )
            )
        except Exception as exc:
            outcome.put(("error", exc))

    owner_thread = threading.Thread(target=owner)
    owner_thread.start()
    try:
        assert executor_started.wait(timeout=5)
        clock.advance(seconds=11)

        def fence(context: ExecutionContext):
            return {
                "status": "fenced",
                "method": "shared_database_transaction_cas",
                "external_request_id": context.external_request_id,
                "generation": context.generation,
            }

        def readback(context: ExecutionContext):
            readback_db = local_session()
            try:
                current = readback_db.get(InventoryRecoveryTask, task_id)
                assert current.status == "monitoring"
                assert current.last_action_request_id is None
                return {
                    "status": "failed",
                    "reason": "request_marker_not_applied",
                    "external_request_id": context.external_request_id,
                }
            finally:
                readback_db.close()

        winning_result = gateway.execute(
            _intent(key="atomic-local-stale-owner"),
            lambda _context: pytest.fail("executor must not replay"),
            readback=readback,
            stale_execution_fence=fence,
            atomic_local_transaction=True,
        )
        assert winning_result["status"] == "failed"

        release_executor.set()
        owner_thread.join(timeout=5)
        assert not owner_thread.is_alive()
        owner_kind, owner_result = outcome.get(timeout=5)
        assert owner_kind == "result"
        assert owner_result == winning_result

        verify = local_session()
        try:
            current = verify.get(InventoryRecoveryTask, task_id)
            assert current.status == "monitoring"
            assert current.last_action_request_id is None
            assert verify.query(ActionFenceEvidence).count() == 1
            assert verify.query(ActionTerminalFact).count() == 1
        finally:
            verify.close()
    finally:
        release_executor.set()
        owner_thread.join(timeout=5)
        engine.dispose()
