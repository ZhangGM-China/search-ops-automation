"""Fail-closed execution boundary for governed write actions."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import requests
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is optional at runtime
    _HTTPX_TRANSPORT_ERRORS: tuple[type[Exception], ...] = ()
else:
    _HTTPX_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.TransportError)

from src.persistence import (
    ActionAttempt,
    ActionFenceEvidence,
    ActionIntent as ActionIntentModel,
    ActionReadback,
    ActionTerminalFact,
    SessionLocal,
)


class ActionGatewayError(RuntimeError):
    """Base error for action authorization and fact-chain failures."""


class ActionAuthorizationError(ActionGatewayError):
    pass


class ActionConflictError(ActionGatewayError):
    pass


class ActionInProgressError(ActionGatewayError):
    pass


class ActionExecutionFailedError(ActionGatewayError):
    """Stable failure contract returned for deterministic executor failures."""

    def __init__(self, result: Any) -> None:
        self.result = result
        super().__init__("action executor failed")


ACTION_FACT_MAX_CHARS = 4096
ACTION_FACT_MAX_BYTES = 4096
_REDACTED = "[REDACTED]"
_SENSITIVE_NAME_PATTERN = (
    r"(?:authorization|access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"secret|api[_-]?key|cookie)"
)
_SENSITIVE_FIELD = re.compile(
    _SENSITIVE_NAME_PATTERN,
    re.IGNORECASE,
)
_CREDENTIAL_URL = re.compile(
    r"\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/@\s]+)@",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rf"(\b{_SENSITIVE_NAME_PATTERN}\b\s*[:=]\s*)"
    r"(?:Bearer\s+|Basic\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;}&]+)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_MARKER = re.compile(
    rf"\b{_SENSITIVE_NAME_PATTERN}\b\s*[:=]",
    re.IGNORECASE,
)
_AUTHORIZATION_VALUE = re.compile(
    r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_AUTHORIZATION_MARKER = re.compile(
    r"\b(?:Bearer|Basic)\s+(?:\[REDACTED\]|[A-Za-z0-9._~+/=-]+)",
    re.IGNORECASE,
)
_REDACTED_CREDENTIAL_URL = re.compile(
    r"\b[a-z][a-z0-9+.-]*://\[REDACTED\]@",
    re.IGNORECASE,
)
_SENSITIVE_PAYLOAD_ENVELOPE_KEY = "__aiops_action_payload_fact_v1__"
_SENSITIVE_PAYLOAD_VERSION = 1
_SENSITIVE_PAYLOAD_MAX_RAW_BYTES = 65536
_SENSITIVE_PAYLOAD_MAX_DEPTH = 64
_SENSITIVE_PAYLOAD_MAX_NODES = 65536
_SENSITIVE_PAYLOAD_SALT_BYTES = 16
_SENSITIVE_PAYLOAD_SCRYPT_N = 1 << 14
_SENSITIVE_PAYLOAD_SCRYPT_R = 8
_SENSITIVE_PAYLOAD_SCRYPT_P = 1
_SENSITIVE_PAYLOAD_SCRYPT_DKLEN = 32


@dataclass(frozen=True)
class ActionIntent:
    action_type: str
    idempotency_key: str
    payload: dict[str, Any]
    operator: str
    brand: str
    release: str
    permission: str
    query_text: str | None = None
    target: str | None = None
    parent_intent_id: int | None = None
    # Optional stable identity payload used for idempotency comparison while
    # ``payload`` retains the complete auditable request facts.
    idempotency_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionContext:
    external_request_id: str
    intent_id: int
    attempt_id: int
    generation: int
    claim_token: str
    claim_kind: str
    attempt_version: int
    intent_version: int
    transaction: Any | None = field(default=None, repr=False, compare=False)


_NO_TERMINAL = object()


def _sanitize_fact_text(value: str) -> str:
    sanitized = _CREDENTIAL_URL.sub(r"\1[REDACTED]@", value)
    sanitized = _SENSITIVE_ASSIGNMENT.sub(r"\1[REDACTED]", sanitized)
    return _AUTHORIZATION_VALUE.sub(r"\1 [REDACTED]", sanitized)


def _fact_text_has_sensitive_semantics(value: str) -> bool:
    return any(
        pattern.search(value) is not None
        for pattern in (
            _CREDENTIAL_URL,
            _REDACTED_CREDENTIAL_URL,
            _SENSITIVE_ASSIGNMENT_MARKER,
            _AUTHORIZATION_MARKER,
        )
    )


def _canonical_raw_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return str(value)


def _sanitize_fact_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            normalized_key = str(key)
            sanitized[normalized_key] = (
                _REDACTED
                if _SENSITIVE_FIELD.search(normalized_key)
                else _sanitize_fact_value(item)
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize_fact_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_fact_text(value)
    return value


def _canonical_sanitized_json(value: Any) -> str:
    sanitized = _sanitize_fact_value(value)
    try:
        return json.dumps(
            sanitized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return json.dumps(
            _sanitize_fact_text(str(sanitized)),
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _bounded_fact_json(
    value: Any,
    *,
    max_chars: int = ACTION_FACT_MAX_CHARS,
    max_bytes: int = ACTION_FACT_MAX_BYTES,
) -> str:
    sanitized = _sanitize_fact_value(value)
    encoded = _canonical_sanitized_json(value)
    if len(encoded) <= max_chars and len(encoded.encode("utf-8")) <= max_bytes:
        return encoded

    full_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    status = sanitized.get("status") if isinstance(sanitized, dict) else None

    def render(preview_chars: int) -> str:
        summary: dict[str, Any] = {
            "_truncated": True,
            "preview": encoded[:preview_chars],
            "sha256": full_hash,
        }
        if isinstance(status, str) and len(status) <= 80:
            summary["status"] = status
        return json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    low = 0
    high = len(encoded)
    bounded = render(0)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = render(midpoint)
        if (
            len(candidate) <= max_chars
            and len(candidate.encode("utf-8")) <= max_bytes
        ):
            bounded = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return bounded


def _payload_requires_semantic_verifier(payload: dict[str, Any]) -> bool:
    stack: list[tuple[Any, int]] = [(payload, 0)]
    visited_nodes = 0
    sensitive = False
    while stack:
        value, depth = stack.pop()
        visited_nodes += 1
        if (
            visited_nodes > _SENSITIVE_PAYLOAD_MAX_NODES
            or depth > _SENSITIVE_PAYLOAD_MAX_DEPTH
        ):
            raise ActionGatewayError("action payload sensitivity complexity limit")
        if isinstance(value, dict):
            for key, item in value.items():
                if _SENSITIVE_FIELD.search(str(key)):
                    sensitive = True
                stack.append((item, depth + 1))
        elif isinstance(value, (list, tuple)):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str) and _fact_text_has_sensitive_semantics(value):
            sensitive = True
    return sensitive


def _sensitive_payload_input(payload: dict[str, Any]) -> bytes:
    encoded = _canonical_raw_json(payload).encode("utf-8")
    if len(encoded) > _SENSITIVE_PAYLOAD_MAX_RAW_BYTES:
        raise ActionGatewayError("sensitive action payload exceeds verifier limit")
    return encoded


def _derive_sensitive_payload_verifier(payload_bytes: bytes, salt: bytes) -> bytes:
    try:
        return hashlib.scrypt(
            payload_bytes,
            salt=salt,
            n=_SENSITIVE_PAYLOAD_SCRYPT_N,
            r=_SENSITIVE_PAYLOAD_SCRYPT_R,
            p=_SENSITIVE_PAYLOAD_SCRYPT_P,
            dklen=_SENSITIVE_PAYLOAD_SCRYPT_DKLEN,
        )
    except (TypeError, ValueError) as exc:
        raise ActionGatewayError("sensitive payload verifier unavailable") from exc


def _render_sensitive_payload_fact(
    sanitized_json: str,
    *,
    salt_hex: str,
    verifier_hex: str,
) -> str:
    verifier = {
        "algorithm": "scrypt",
        "dklen": _SENSITIVE_PAYLOAD_SCRYPT_DKLEN,
        "digest": verifier_hex,
        "n": _SENSITIVE_PAYLOAD_SCRYPT_N,
        "p": _SENSITIVE_PAYLOAD_SCRYPT_P,
        "r": _SENSITIVE_PAYLOAD_SCRYPT_R,
        "salt": salt_hex,
    }
    try:
        sanitized_value = json.loads(sanitized_json)
    except (TypeError, ValueError):
        sanitized_value = sanitized_json

    def render(audit_value: Any) -> str:
        return json.dumps(
            {
                _SENSITIVE_PAYLOAD_ENVELOPE_KEY: {
                    "sanitized": audit_value,
                    "semantic_verifier": verifier,
                    "version": _SENSITIVE_PAYLOAD_VERSION,
                }
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    encoded = render(sanitized_value)
    if (
        len(encoded) <= ACTION_FACT_MAX_CHARS
        and len(encoded.encode("utf-8")) <= ACTION_FACT_MAX_BYTES
    ):
        return encoded

    sanitized_hash = hashlib.sha256(sanitized_json.encode("utf-8")).hexdigest()

    def render_preview(preview_chars: int) -> str:
        return render({
            "_truncated": True,
            "preview": sanitized_json[:preview_chars],
            "sha256": sanitized_hash,
        })

    low = 0
    high = len(sanitized_json)
    bounded = render_preview(0)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = render_preview(midpoint)
        if (
            len(candidate) <= ACTION_FACT_MAX_CHARS
            and len(candidate.encode("utf-8")) <= ACTION_FACT_MAX_BYTES
        ):
            bounded = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return bounded


def _canonical_sensitive_payload(payload: dict[str, Any]) -> tuple[str, str]:
    payload_bytes = _sensitive_payload_input(payload)
    salt = secrets.token_bytes(_SENSITIVE_PAYLOAD_SALT_BYTES)
    verifier = _derive_sensitive_payload_verifier(payload_bytes, salt)
    payload_json = _render_sensitive_payload_fact(
        _canonical_sanitized_json(payload),
        salt_hex=salt.hex(),
        verifier_hex=verifier.hex(),
    )
    return payload_json, hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _verify_sensitive_payload_fact(payload_json: str, payload: dict[str, Any]) -> bool:
    try:
        fact = json.loads(payload_json)
        if not isinstance(fact, dict) or set(fact) != {_SENSITIVE_PAYLOAD_ENVELOPE_KEY}:
            return False
        envelope = fact[_SENSITIVE_PAYLOAD_ENVELOPE_KEY]
        if not isinstance(envelope, dict) or set(envelope) != {
            "sanitized",
            "semantic_verifier",
            "version",
        }:
            return False
        if envelope["version"] != _SENSITIVE_PAYLOAD_VERSION:
            return False
        verifier = envelope["semantic_verifier"]
        if not isinstance(verifier, dict) or verifier != {
            "algorithm": "scrypt",
            "dklen": _SENSITIVE_PAYLOAD_SCRYPT_DKLEN,
            "digest": verifier.get("digest"),
            "n": _SENSITIVE_PAYLOAD_SCRYPT_N,
            "p": _SENSITIVE_PAYLOAD_SCRYPT_P,
            "r": _SENSITIVE_PAYLOAD_SCRYPT_R,
            "salt": verifier.get("salt"),
        }:
            return False
        salt_hex = verifier["salt"]
        digest_hex = verifier["digest"]
        if not isinstance(salt_hex, str) or not isinstance(digest_hex, str):
            return False
        if len(salt_hex) != _SENSITIVE_PAYLOAD_SALT_BYTES * 2:
            return False
        if len(digest_hex) != _SENSITIVE_PAYLOAD_SCRYPT_DKLEN * 2:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    actual = _derive_sensitive_payload_verifier(
        _sensitive_payload_input(payload),
        salt,
    )
    return hmac.compare_digest(actual, expected)


def _payload_semantically_matches(
    payload_json: str,
    payload_hash: str,
    payload: dict[str, Any],
    idempotency_payload: dict[str, Any] | None = None,
) -> bool:
    comparable_payload = idempotency_payload or payload
    if idempotency_payload is not None:
        _incoming_json, incoming_hash = canonical_payload(comparable_payload)
        return payload_hash == incoming_hash
    if _payload_requires_semantic_verifier(comparable_payload):
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_hash:
            return False
        return _verify_sensitive_payload_fact(payload_json, comparable_payload)
    incoming_json, incoming_hash = canonical_payload(comparable_payload)
    return payload_json == incoming_json and payload_hash == incoming_hash


def canonical_payload(payload: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("action payload must be an object")
    if _payload_requires_semantic_verifier(payload):
        return _canonical_sensitive_payload(payload)
    encoded = _canonical_sanitized_json(payload)
    return (
        _bounded_fact_json(payload),
        hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


class ActionGateway:
    """Persist authorization facts before invoking an external executor.

    The gateway deliberately requires an explicit authorizer. Callers must supply
    the existing user/brand permission decision instead of silently widening scope.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = SessionLocal,
        authorizer: Callable[[ActionIntent], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        execution_lease_seconds: int = 120,
        readback_lease_seconds: int = 60,
        fence_lease_seconds: int = 60,
    ) -> None:
        lease_values = {
            "execution_lease_seconds": execution_lease_seconds,
            "readback_lease_seconds": readback_lease_seconds,
            "fence_lease_seconds": fence_lease_seconds,
        }
        for field_name, value in lease_values.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        self.session_factory = session_factory
        self.authorizer = authorizer
        self.clock = clock
        self.execution_lease_seconds = execution_lease_seconds
        self.readback_lease_seconds = readback_lease_seconds
        self.fence_lease_seconds = fence_lease_seconds

    def execute(
        self,
        intent: ActionIntent,
        executor: Callable[[ExecutionContext], Any],
        *,
        readback: Callable[[ExecutionContext], Any] | None = None,
        stale_execution_fence: Callable[[ExecutionContext], dict[str, Any]] | None = None,
        atomic_local_transaction: bool = False,
        authoritative_readback_on_success: bool = False,
    ) -> Any:
        self._validate_intent(intent)
        if self.authorizer is None or not self.authorizer(intent):
            raise ActionAuthorizationError("action authorization denied")

        db = self.session_factory()
        try:
            existing = (
                db.query(ActionIntentModel)
                .filter_by(idempotency_key=intent.idempotency_key)
                .first()
            )
            if existing is not None:
                return self._resolve_existing(
                    db,
                    existing,
                    intent,
                    executor=executor,
                    readback=readback,
                    stale_execution_fence=stale_execution_fence,
                    atomic_local_transaction=atomic_local_transaction,
                    authoritative_readback_on_success=authoritative_readback_on_success,
                    allow_reconcile=True,
                )

            payload_json, audit_hash = canonical_payload(intent.payload)
            if intent.idempotency_payload is None:
                payload_hash = audit_hash
            else:
                _identity_json, payload_hash = canonical_payload(intent.idempotency_payload)
            intent_row = ActionIntentModel(
                action_type=intent.action_type,
                idempotency_key=intent.idempotency_key,
                payload_hash=payload_hash,
                payload_json=payload_json,
                operator=intent.operator,
                brand=intent.brand,
                release=intent.release,
                permission=intent.permission,
                query_text=intent.query_text,
                target=intent.target,
                parent_intent_id=intent.parent_intent_id,
                status="pending",
                version=0,
            )
            attempt = ActionAttempt(
                intent=intent_row,
                attempt_no=1,
                status="persisted",
                external_request_id=intent.idempotency_key,
            )
            db.add(intent_row)
            db.add(attempt)
            db.commit()
            return self._execute_attempt(
                db,
                intent_row,
                attempt,
                executor=executor,
                readback=readback,
                atomic_local_transaction=atomic_local_transaction,
                authoritative_readback_on_success=authoritative_readback_on_success,
            )
        except IntegrityError as exc:
            db.rollback()
            existing = (
                db.query(ActionIntentModel)
                .filter_by(idempotency_key=intent.idempotency_key)
                .first()
            )
            if existing is None:
                raise ActionConflictError("action intent uniqueness conflict") from exc
            return self._resolve_existing(
                db,
                existing,
                intent,
                executor=None,
                readback=None,
                stale_execution_fence=None,
                atomic_local_transaction=False,
                authoritative_readback_on_success=False,
                allow_reconcile=False,
            )
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise
        finally:
            db.close()

    def _resolve_existing(
        self,
        db: Any,
        existing: ActionIntentModel,
        intent: ActionIntent,
        *,
        executor: Callable[[ExecutionContext], Any] | None,
        readback: Callable[[ExecutionContext], Any] | None,
        stale_execution_fence: Callable[[ExecutionContext], dict[str, Any]] | None,
        atomic_local_transaction: bool,
        authoritative_readback_on_success: bool,
        allow_reconcile: bool,
    ) -> Any:
        if not _payload_semantically_matches(
            existing.payload_json,
            existing.payload_hash,
            intent.payload,
            intent.idempotency_payload,
        ):
            raise ActionConflictError("idempotency key payload hash conflict")
        scope_fields = (
            "action_type",
            "operator",
            "brand",
            "release",
            "permission",
            "query_text",
            "target",
            "parent_intent_id",
        )
        scope_conflicts = [
            field
            for field in scope_fields
            if getattr(existing, field, None) != getattr(intent, field, None)
        ]
        if scope_conflicts:
            raise ActionConflictError(
                "idempotency key action scope conflict: " + ",".join(scope_conflicts)
            )
        terminal = (
            db.query(ActionTerminalFact)
            .filter_by(intent_id=existing.id)
            .first()
        )
        if terminal is not None:
            return self._terminal_result(terminal)

        attempt = (
            db.query(ActionAttempt)
            .filter_by(intent_id=existing.id)
            .order_by(ActionAttempt.attempt_no.desc())
            .first()
        )
        if attempt is None:
            raise ActionInProgressError("action intent has no attempt fact")
        if allow_reconcile and attempt.status == "persisted" and executor is not None:
            return self._execute_attempt(
                db,
                existing,
                attempt,
                executor=executor,
                readback=readback,
                atomic_local_transaction=atomic_local_transaction,
                authoritative_readback_on_success=authoritative_readback_on_success,
            )
        if allow_reconcile and attempt.status in {"executing", "fencing"}:
            lease_expires_at = attempt.lease_expires_at
            if lease_expires_at is None or lease_expires_at > self._now(db):
                raise ActionInProgressError("action execution claim is still active")
            if stale_execution_fence is None:
                raise ActionInProgressError(
                    "expired action execution requires external fence proof"
                )
            self._fence_stale_execution(
                db,
                existing,
                attempt,
                stale_execution_fence=stale_execution_fence,
            )
            return self._reconcile_unknown(
                db,
                existing,
                attempt,
                readback=readback,
            )
        if allow_reconcile and attempt.status in {"execution_unknown", "reconciling"}:
            return self._reconcile_unknown(
                db,
                existing,
                attempt,
                readback=readback,
            )
        raise ActionInProgressError("action intent has no terminal fact")

    def _execute_attempt(
        self,
        db: Any,
        intent_row: ActionIntentModel,
        attempt: ActionAttempt,
        *,
        executor: Callable[[ExecutionContext], Any],
        readback: Callable[[ExecutionContext], Any] | None,
        atomic_local_transaction: bool,
        authoritative_readback_on_success: bool,
    ) -> Any:
        execution_context = self._claim_execution(db, intent_row, attempt)
        if atomic_local_transaction:
            execution_context = ExecutionContext(
                **{
                    **execution_context.__dict__,
                    "transaction": db,
                }
            )
        try:
            result = executor(execution_context)
        except Exception as exc:
            db.rollback()
            if self._is_ambiguous_external_failure(exc):
                try:
                    self._mark_execution_unknown(
                        db,
                        attempt,
                        claim_token=execution_context.claim_token,
                        expected_version=execution_context.attempt_version,
                    )
                except ActionInProgressError:
                    replay = self._read_terminal_result(db, intent_row.id)
                    if replay is not _NO_TERMINAL:
                        return replay
                    raise
                return self._reconcile_unknown(
                    db,
                    intent_row,
                    attempt,
                    readback=readback,
                )
            try:
                terminal_result = self._persist_terminal(
                    db,
                    intent_row,
                    attempt,
                    status="failed",
                    terminal_code="executor_failed",
                    result={"status": "failed", "error": str(exc)},
                    expected_status="executing",
                    claim_token=execution_context.claim_token,
                    expected_attempt_version=execution_context.attempt_version,
                    expected_intent_version=execution_context.intent_version,
                )
            except ActionInProgressError:
                replay = self._read_terminal_result(db, intent_row.id)
                if replay is not _NO_TERMINAL:
                    return replay
                raise
            raise ActionExecutionFailedError(terminal_result) from None

        try:
            if authoritative_readback_on_success:
                return self._persist_success_with_readback(
                    db,
                    intent_row,
                    attempt,
                    execution_context,
                    result,
                    readback=readback,
                )
            return self._persist_terminal(
                db,
                intent_row,
                attempt,
                status="success",
                terminal_code="executor_succeeded",
                result=result,
                expected_status="executing",
                claim_token=execution_context.claim_token,
                expected_attempt_version=execution_context.attempt_version,
                expected_intent_version=execution_context.intent_version,
            )
        except Exception:
            db.rollback()
            try:
                self._mark_execution_unknown(
                    db,
                    attempt,
                    claim_token=execution_context.claim_token,
                    expected_version=execution_context.attempt_version,
                )
            except ActionInProgressError:
                replay = self._read_terminal_result(db, intent_row.id)
                if replay is not _NO_TERMINAL:
                    return replay
                raise
            return self._reconcile_unknown(
                db,
                intent_row,
                attempt,
                readback=readback,
            )

    def _persist_success_with_readback(
        self,
        db: Any,
        intent_row: ActionIntentModel,
        attempt: ActionAttempt,
        context: ExecutionContext,
        result: Any,
        *,
        readback: Callable[[ExecutionContext], Any] | None,
    ) -> Any:
        """Require one authoritative local readback before committing a success."""
        authoritative = 0
        readback_status = "unavailable"
        observed_payload: dict[str, Any]
        if readback is None:
            observed_payload = {"status": "inconclusive", "reason": "readback_unavailable"}
            terminal_status = "inconclusive"
        else:
            try:
                observed = readback(context)
            except Exception:
                observed = None
                readback_status = "failed"
                observed_payload = {"status": "inconclusive", "reason": "readback_failed"}
                terminal_status = "inconclusive"
            else:
                if not isinstance(observed, dict):
                    observed_payload = {
                        "status": "inconclusive",
                        "reason": "invalid_readback_contract",
                    }
                    terminal_status = "inconclusive"
                    readback_status = "failed"
                else:
                    readback_status = str(observed.get("status") or "").strip().lower()
                    if readback_status not in {"success", "failed"}:
                        observed_payload = {
                            "status": "inconclusive",
                            "reason": "invalid_readback_contract",
                        }
                        terminal_status = "inconclusive"
                        readback_status = "failed"
                    else:
                        authoritative = 1
                        terminal_status = readback_status
                        observed_payload = dict(observed)
                        observed_payload["status"] = terminal_status

        if terminal_status != "success":
            # The local mutation belongs to this transaction and must not survive
            # an absent, invalid, or failed authoritative verification.
            db.rollback()

        observed_json = _bounded_fact_json(observed_payload)
        observed_canonical = _canonical_sanitized_json(observed_payload)
        observed_hash = hashlib.sha256(observed_canonical.encode("utf-8")).hexdigest()
        db.add(
            ActionReadback(
                intent_id=intent_row.id,
                attempt_id=attempt.id,
                status=readback_status,
                authoritative=authoritative,
                observed_payload=observed_json,
                fact_hash=observed_hash,
            )
        )
        terminal_payload = result if terminal_status == "success" else observed_payload
        terminal_result = self._persist_terminal(
            db,
            intent_row,
            attempt,
            status=terminal_status,
            terminal_code=(
                "authoritative_readback_success"
                if terminal_status == "success"
                else (
                    f"authoritative_readback_{terminal_status}"
                    if authoritative
                    else f"readback_{readback_status}"
                )
            ),
            result=terminal_payload,
            expected_status="executing",
            claim_token=context.claim_token,
            expected_attempt_version=context.attempt_version,
            expected_intent_version=context.intent_version,
            commit=False,
        )
        db.commit()
        return terminal_result

    @staticmethod
    def _read_terminal_result(db: Any, intent_id: int) -> Any:
        terminal = (
            db.query(ActionTerminalFact)
            .filter_by(intent_id=intent_id)
            .first()
        )
        if terminal is None:
            return _NO_TERMINAL
        return ActionGateway._terminal_result(terminal)

    @staticmethod
    def _terminal_result(terminal: ActionTerminalFact) -> Any:
        result = json.loads(terminal.replay_result_json)
        if terminal.terminal_code == "executor_failed":
            raise ActionExecutionFailedError(result)
        return result

    def _now(self, db: Any) -> datetime:
        if self.clock is not None:
            now = self.clock()
        else:
            now = db.execute(select(func.current_timestamp())).scalar_one()
        if not isinstance(now, datetime):
            raise ActionGatewayError("database clock did not return datetime")
        return now

    @staticmethod
    def _reload_attempt(db: Any, attempt_id: int) -> ActionAttempt | None:
        return (
            db.query(ActionAttempt)
            .populate_existing()
            .filter(ActionAttempt.id == attempt_id)
            .one_or_none()
        )

    def _claim_execution(
        self,
        db: Any,
        intent_row: ActionIntentModel,
        attempt: ActionAttempt,
    ) -> ExecutionContext:
        claim_token = uuid.uuid4().hex
        now = self._now(db)
        lease_expires_at = now + timedelta(seconds=self.execution_lease_seconds)
        expected_version = int(attempt.version or 0)
        updated = (
            db.query(ActionAttempt)
            .filter(
                ActionAttempt.id == attempt.id,
                ActionAttempt.status == "persisted",
                ActionAttempt.version == expected_version,
            )
            .update(
                {
                    ActionAttempt.status: "executing",
                    ActionAttempt.claim_token: claim_token,
                    ActionAttempt.lease_expires_at: lease_expires_at,
                    ActionAttempt.execution_generation: ActionAttempt.execution_generation + 1,
                    ActionAttempt.version: ActionAttempt.version + 1,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            db.rollback()
            raise ActionInProgressError("action execution claim is already held")
        db.commit()
        claimed = self._reload_attempt(db, attempt.id)
        if claimed is None:
            db.rollback()
            raise ActionInProgressError("action execution claim disappeared")
        context = ExecutionContext(
            external_request_id=str(
                claimed.external_request_id or intent_row.idempotency_key
            ),
            intent_id=int(intent_row.id),
            attempt_id=int(claimed.id),
            generation=int(claimed.execution_generation or 0),
            claim_token=claim_token,
            claim_kind="execution",
            attempt_version=int(claimed.version or 0),
            intent_version=int(intent_row.version or 0),
        )
        db.commit()
        return context

    def _claim_fence(
        self,
        db: Any,
        intent_row: ActionIntentModel,
        attempt: ActionAttempt,
    ) -> ExecutionContext:
        claim_token = uuid.uuid4().hex
        now = self._now(db)
        lease_expires_at = now + timedelta(seconds=self.fence_lease_seconds)
        expected_version = int(attempt.version or 0)
        updated = (
            db.query(ActionAttempt)
            .filter(
                ActionAttempt.id == attempt.id,
                ActionAttempt.status.in_(("executing", "fencing")),
                ActionAttempt.lease_expires_at <= now,
                ActionAttempt.version == expected_version,
            )
            .update(
                {
                    ActionAttempt.status: "fencing",
                    ActionAttempt.claim_token: claim_token,
                    ActionAttempt.lease_expires_at: lease_expires_at,
                    ActionAttempt.version: ActionAttempt.version + 1,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            db.rollback()
            raise ActionInProgressError("action fence claim is already held")
        db.commit()
        claimed = self._reload_attempt(db, attempt.id)
        if claimed is None:
            db.rollback()
            raise ActionInProgressError("action fence claim disappeared")
        context = ExecutionContext(
            external_request_id=str(
                claimed.external_request_id or intent_row.idempotency_key
            ),
            intent_id=int(intent_row.id),
            attempt_id=int(claimed.id),
            generation=int(claimed.execution_generation or 0),
            claim_token=claim_token,
            claim_kind="fence",
            attempt_version=int(claimed.version or 0),
            intent_version=int(intent_row.version or 0),
        )
        if context.generation <= 0:
            db.rollback()
            raise ActionInProgressError("action fence generation is unavailable")
        db.commit()
        return context

    def _persist_fence_evidence(
        self,
        db: Any,
        context: ExecutionContext,
        evidence: dict[str, Any],
    ) -> None:
        if not isinstance(evidence, dict):
            raise ActionInProgressError("external fence proof unavailable")
        status = str(evidence.get("status") or "").strip().lower()
        if status != "fenced":
            raise ActionInProgressError("external fence proof unavailable")
        evidence_payload = dict(evidence)
        evidence_payload["status"] = "fenced"
        evidence_json = _bounded_fact_json(evidence_payload)
        evidence_canonical = _canonical_sanitized_json(evidence_payload)
        evidence_hash = hashlib.sha256(evidence_canonical.encode("utf-8")).hexdigest()
        updated = (
            db.query(ActionAttempt)
            .filter(
                ActionAttempt.id == context.attempt_id,
                ActionAttempt.status == "fencing",
                ActionAttempt.execution_generation == context.generation,
                ActionAttempt.claim_token == context.claim_token,
                ActionAttempt.version == context.attempt_version,
            )
            .update(
                {
                    ActionAttempt.status: "execution_unknown",
                    ActionAttempt.claim_token: None,
                    ActionAttempt.lease_expires_at: None,
                    ActionAttempt.version: ActionAttempt.version + 1,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            db.rollback()
            raise ActionInProgressError("action fence claim is no longer current")
        db.add(
            ActionFenceEvidence(
                intent_id=context.intent_id,
                attempt_id=context.attempt_id,
                external_request_id=context.external_request_id,
                execution_generation=context.generation,
                fence_claim_token=context.claim_token,
                status="fenced",
                evidence_payload=evidence_json,
                fact_hash=evidence_hash,
            )
        )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ActionInProgressError("action fence evidence already exists") from exc

    def _fence_stale_execution(
        self,
        db: Any,
        intent_row: ActionIntentModel,
        attempt: ActionAttempt,
        *,
        stale_execution_fence: Callable[[ExecutionContext], dict[str, Any]],
    ) -> None:
        context = self._claim_fence(db, intent_row, attempt)
        try:
            evidence = stale_execution_fence(context)
        except Exception as exc:
            raise ActionInProgressError("external fence proof unavailable") from exc
        self._persist_fence_evidence(db, context, evidence)

    @staticmethod
    def _mark_execution_unknown(
        db: Any,
        attempt: ActionAttempt,
        *,
        claim_token: str,
        expected_version: int,
    ) -> None:
        updated = (
            db.query(ActionAttempt)
            .filter(
                ActionAttempt.id == attempt.id,
                ActionAttempt.status == "executing",
                ActionAttempt.claim_token == claim_token,
                ActionAttempt.version == expected_version,
            )
            .update(
                {
                    ActionAttempt.status: "execution_unknown",
                    ActionAttempt.claim_token: None,
                    ActionAttempt.lease_expires_at: None,
                    ActionAttempt.version: ActionAttempt.version + 1,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            db.rollback()
            raise ActionInProgressError("action execution claim is no longer current")
        db.commit()

    def _claim_readback(
        self,
        db: Any,
        intent_row: ActionIntentModel,
        attempt: ActionAttempt,
    ) -> ExecutionContext:
        claim_token = uuid.uuid4().hex
        now = self._now(db)
        lease_expires_at = now + timedelta(seconds=self.readback_lease_seconds)
        expected_version = int(attempt.version or 0)
        claimable = [
            ActionAttempt.status == "execution_unknown",
            and_(
                ActionAttempt.status == "reconciling",
                ActionAttempt.lease_expires_at <= now,
            ),
        ]
        updated = (
            db.query(ActionAttempt)
            .filter(
                ActionAttempt.id == attempt.id,
                ActionAttempt.version == expected_version,
                or_(*claimable),
            )
            .update(
                {
                    ActionAttempt.status: "reconciling",
                    ActionAttempt.claim_token: claim_token,
                    ActionAttempt.lease_expires_at: lease_expires_at,
                    ActionAttempt.version: ActionAttempt.version + 1,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            db.rollback()
            raise ActionInProgressError("action readback claim is already held")
        db.commit()
        claimed = self._reload_attempt(db, attempt.id)
        if claimed is None:
            db.rollback()
            raise ActionInProgressError("action readback claim disappeared")
        context = ExecutionContext(
            external_request_id=str(
                claimed.external_request_id or intent_row.idempotency_key
            ),
            intent_id=int(intent_row.id),
            attempt_id=int(claimed.id),
            generation=int(claimed.execution_generation or 0),
            claim_token=claim_token,
            claim_kind="readback",
            attempt_version=int(claimed.version or 0),
            intent_version=int(intent_row.version or 0),
        )
        db.commit()
        return context

    @staticmethod
    def _release_readback_claim(
        db: Any,
        attempt: ActionAttempt,
        *,
        claim_token: str,
        expected_version: int,
    ) -> None:
        updated = (
            db.query(ActionAttempt)
            .filter(
                ActionAttempt.id == attempt.id,
                ActionAttempt.status == "reconciling",
                ActionAttempt.claim_token == claim_token,
                ActionAttempt.version == expected_version,
            )
            .update(
                {
                    ActionAttempt.status: "execution_unknown",
                    ActionAttempt.claim_token: None,
                    ActionAttempt.lease_expires_at: None,
                    ActionAttempt.version: ActionAttempt.version + 1,
                },
                synchronize_session=False,
            )
        )
        if updated == 1:
            db.commit()
        else:
            db.rollback()

    @staticmethod
    def _is_ambiguous_external_failure(exc: Exception) -> bool:
        if getattr(exc, "ambiguous_external", False) is True:
            return True
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        if isinstance(
            exc,
            (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                *_HTTPX_TRANSPORT_ERRORS,
            ),
        ):
            return True

        status_code = getattr(exc, "status_code", None)
        if not isinstance(status_code, int) or isinstance(status_code, bool):
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
        return (
            isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 500 <= status_code <= 599
        )

    def _reconcile_unknown(
        self,
        db: Any,
        intent_row: ActionIntentModel,
        attempt: ActionAttempt,
        *,
        readback: Callable[[ExecutionContext], Any] | None,
    ) -> Any:
        """Resolve a transport-unknown result only through authoritative readback."""
        fresh_attempt = self._reload_attempt(db, attempt.id)
        if fresh_attempt is None:
            raise ActionInProgressError("action readback attempt disappeared")
        attempt = fresh_attempt
        readback_context = self._claim_readback(
            db,
            intent_row,
            attempt,
        )

        authoritative = 0
        if readback is None:
            readback_status = "unavailable"
            terminal_status = "inconclusive"
            observed_payload = {"status": terminal_status, "reason": "readback_unavailable"}
        else:
            try:
                observed = readback(readback_context)
            except Exception:
                readback_status = "failed"
                terminal_status = "inconclusive"
                observed_payload = {"status": terminal_status, "reason": "readback_failed"}
            else:
                if not isinstance(observed, dict):
                    readback_status = "failed"
                    terminal_status = "inconclusive"
                    observed_payload = {
                        "status": terminal_status,
                        "reason": "invalid_readback_contract",
                    }
                else:
                    readback_status = str(observed.get("status") or "").strip().lower()
                    if readback_status not in {"success", "failed"}:
                        readback_status = "failed"
                        terminal_status = "inconclusive"
                        observed_payload = {
                            "status": terminal_status,
                            "reason": "invalid_readback_contract",
                        }
                    else:
                        authoritative = 1
                        terminal_status = readback_status
                        observed_payload = dict(observed)
                        observed_payload["status"] = terminal_status

        observed_json = _bounded_fact_json(observed_payload)
        observed_canonical = _canonical_sanitized_json(observed_payload)
        observed_hash = hashlib.sha256(observed_canonical.encode("utf-8")).hexdigest()
        try:
            db.add(
                ActionReadback(
                    intent_id=intent_row.id,
                    attempt_id=attempt.id,
                    status=readback_status,
                    authoritative=authoritative,
                    observed_payload=observed_json,
                    fact_hash=observed_hash,
                )
            )
            terminal_result = self._persist_terminal(
                db,
                intent_row,
                attempt,
                status=terminal_status,
                terminal_code=(
                    f"authoritative_readback_{terminal_status}"
                    if authoritative
                    else f"readback_{readback_status}"
                ),
                result=observed_payload,
                expected_status="reconciling",
                claim_token=readback_context.claim_token,
                expected_attempt_version=readback_context.attempt_version,
                expected_intent_version=readback_context.intent_version,
                commit=False,
            )
            db.commit()
            return terminal_result
        except ActionInProgressError:
            db.rollback()
            winner = self._read_terminal_result(db, intent_row.id)
            if winner is not _NO_TERMINAL:
                return winner
            self._release_readback_claim(
                db,
                attempt,
                claim_token=readback_context.claim_token,
                expected_version=readback_context.attempt_version,
            )
            raise
        except Exception:
            db.rollback()
            self._release_readback_claim(
                db,
                attempt,
                claim_token=readback_context.claim_token,
                expected_version=readback_context.attempt_version,
            )
            raise

    @staticmethod
    def _validate_intent(intent: ActionIntent) -> None:
        if not isinstance(intent, ActionIntent):
            raise TypeError("intent must be an ActionIntent")
        required = {
            "action_type": intent.action_type,
            "idempotency_key": intent.idempotency_key,
            "operator": intent.operator,
            "brand": intent.brand,
            "release": intent.release,
            "permission": intent.permission,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ActionAuthorizationError(f"missing action facts: {', '.join(missing)}")

    @staticmethod
    def _persist_terminal(
        db: Any,
        intent_row: ActionIntentModel,
        attempt: ActionAttempt,
        *,
        status: str,
        terminal_code: str,
        result: Any,
        commit: bool = True,
        expected_status: str | None = None,
        claim_token: str | None = None,
        expected_attempt_version: int | None = None,
        expected_intent_version: int | None = None,
    ) -> Any:
        replay_result_json = _canonical_sanitized_json(result)
        result_json = _bounded_fact_json(result)
        result_hash = hashlib.sha256(replay_result_json.encode("utf-8")).hexdigest()
        finished_at = datetime.now()
        if expected_status is not None and claim_token is not None:
            attempt_version = (
                int(attempt.version or 0)
                if expected_attempt_version is None
                else expected_attempt_version
            )
            intent_version = (
                int(intent_row.version or 0)
                if expected_intent_version is None
                else expected_intent_version
            )
            updated = (
                db.query(ActionAttempt)
                .filter(
                    ActionAttempt.id == attempt.id,
                    ActionAttempt.status == expected_status,
                    ActionAttempt.claim_token == claim_token,
                    ActionAttempt.version == attempt_version,
                )
                .update(
                    {
                        ActionAttempt.status: status,
                        ActionAttempt.finished_at: finished_at,
                        ActionAttempt.response_summary: result_json,
                        ActionAttempt.claim_token: None,
                        ActionAttempt.lease_expires_at: None,
                        ActionAttempt.version: ActionAttempt.version + 1,
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                db.rollback()
                raise ActionInProgressError("action terminal claim is no longer current")
            intent_updated = db.query(ActionIntentModel).filter(
                ActionIntentModel.id == intent_row.id,
                ActionIntentModel.version == intent_version,
            ).update(
                {
                    ActionIntentModel.status: status,
                    ActionIntentModel.version: ActionIntentModel.version + 1,
                },
                synchronize_session=False,
            )
            if intent_updated != 1:
                db.rollback()
                raise ActionInProgressError("action intent version is no longer current")
        else:
            attempt.status = status
            attempt.finished_at = finished_at
            attempt.response_summary = result_json
            intent_row.status = status
            intent_row.version = int(intent_row.version or 0) + 1
        db.add(
            ActionTerminalFact(
                intent_id=intent_row.id,
                status=status,
                terminal_code=terminal_code,
                result_json=result_json,
                replay_result_json=replay_result_json,
                fact_hash=result_hash,
            )
        )
        if commit:
            db.commit()
        return json.loads(replay_result_json)
