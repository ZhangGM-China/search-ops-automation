from __future__ import annotations

from dataclasses import dataclass
import datetime
import json
from typing import Any, Callable, Optional

from sqlalchemy.exc import IntegrityError


class BudgetExceededError(RuntimeError):
    pass


class BudgetReservationError(RuntimeError):
    pass


class WorkflowRunBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetModels:
    account: Any
    ledger: Any


@dataclass(frozen=True)
class WorkflowRunModels:
    run: Any


@dataclass(frozen=True)
class BudgetEntryResult:
    ledger_id: int
    entry_type: str
    idempotency_key: str


@dataclass(frozen=True)
class BudgetReservationRequest:
    account_id: int
    workflow_run_id: int
    task_id: int
    round_no: int
    tokens: int
    actions: int
    idempotency_key: str
    reason: str = ""
    detail: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class BudgetSettlementRequest:
    reservation_key: str
    actual_tokens: int
    actual_actions: int
    idempotency_key: str
    reason: str = ""
    detail: Optional[dict[str, Any]] = None
    allow_overage: bool = False


@dataclass(frozen=True)
class WorkflowRunSnapshot:
    id: int
    task_id: int
    run_key: str
    status: str
    current_node: Optional[str]
    round_no: int
    checkpoint: dict[str, Any]
    checkpoint_version: int
    planner_failures: int
    tool_failures: int
    no_improvement: int
    lease_owner: Optional[str]
    lease_expires_at: Optional[datetime.datetime]


class WorkflowCheckpointCodec:
    SCHEMA_VERSION = 1
    _STATE_KEYS = {
        "task_id", "round_count", "current_stage_index", "dry_run", "context",
        "initial_context", "plan", "executor_results", "executed_check_items",
        "verification", "final_status", "score", "replan_reason", "has_manual_actions",
        "round_action_ids", "executed_action_ids", "budget_stop", "failure_stop", "compliance_stop",
        "run_id", "checkpoint_version", "atomic_account_ids", "resume_node", "batch_key",
        "kg_intents", "prepared_kg_result", "prepared_kg_actions", "prepared_kg_duration",
        "action_reservation_keys",
        "planner_failures", "tool_failures", "no_improvement", "retry_node",
        "token_budget_exhausted", "token_budget_reason", "workflow_lease_seconds",
    }

    @classmethod
    def encode(cls, *, next_node: str, state: dict[str, Any]) -> dict[str, Any]:
        if not next_node:
            raise ValueError("checkpoint next_node is required")
        return {
            "schemaVersion": cls.SCHEMA_VERSION,
            "nextNode": next_node,
            "state": {
                key: cls._encode_value(value)
                for key, value in state.items()
                if key in cls._STATE_KEYS
            },
        }

    @classmethod
    def decode(cls, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if int(payload.get("schemaVersion") or 0) != cls.SCHEMA_VERSION:
            raise ValueError("unsupported workflow checkpoint schema")
        next_node = str(payload.get("nextNode") or "")
        if not next_node:
            raise ValueError("checkpoint next_node is required")
        state = payload.get("state")
        if not isinstance(state, dict):
            raise ValueError("checkpoint state must be an object")
        return next_node, {key: cls._decode_value(value) for key, value in state.items()}

    @classmethod
    def _encode_value(cls, value: Any) -> Any:
        from src.top_query.schemas import (
            ActionCandidate, ExecutorResult, GovernanceStage,
            GovernanceStrategy, PlannerCheck, PlannerOutput,
        )
        type_name = type(value).__name__
        supported = {
            "PlannerCheck": PlannerCheck,
            "ActionCandidate": ActionCandidate,
            "GovernanceStage": GovernanceStage,
            "GovernanceStrategy": GovernanceStrategy,
            "PlannerOutput": PlannerOutput,
            "ExecutorResult": ExecutorResult,
        }
        if type_name in supported and isinstance(value, supported[type_name]):
            return {
                "__type__": type_name,
                "fields": {
                    field_name: cls._encode_value(getattr(value, field_name))
                    for field_name in value.__dataclass_fields__
                },
            }
        if isinstance(value, dict):
            return {str(key): cls._encode_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._encode_value(item) for item in value]
        if isinstance(value, datetime.datetime):
            return {"__type__": "datetime", "value": value.isoformat()}
        if isinstance(value, datetime.date):
            return {"__type__": "date", "value": value.isoformat()}
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError(f"unsupported checkpoint value type: {type(value).__name__}")

    @classmethod
    def _decode_value(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [cls._decode_value(item) for item in value]
        if not isinstance(value, dict):
            return value
        type_name = value.get("__type__")
        if not type_name:
            return {key: cls._decode_value(item) for key, item in value.items()}
        if type_name == "datetime":
            return datetime.datetime.fromisoformat(str(value.get("value") or ""))
        if type_name == "date":
            return datetime.date.fromisoformat(str(value.get("value") or ""))
        from src.top_query.schemas import (
            ActionCandidate, ExecutorResult, GovernanceStage,
            GovernanceStrategy, PlannerCheck, PlannerOutput,
        )
        classes = {
            "PlannerCheck": PlannerCheck,
            "ActionCandidate": ActionCandidate,
            "GovernanceStage": GovernanceStage,
            "GovernanceStrategy": GovernanceStrategy,
            "PlannerOutput": PlannerOutput,
            "ExecutorResult": ExecutorResult,
        }
        model = classes.get(str(type_name))
        if model is None:
            raise ValueError(f"unsupported checkpoint value type: {type_name}")
        fields = value.get("fields")
        if not isinstance(fields, dict):
            raise ValueError("checkpoint typed value fields must be an object")
        return model(**{key: cls._decode_value(item) for key, item in fields.items()})


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dump_json(value: Optional[dict[str, Any]]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class TopQueryAtomicBudgetService:
    def __init__(self, session_factory: Callable[[], Any], models: BudgetModels):
        self._session_factory = session_factory
        self._account = models.account
        self._ledger = models.ledger

    def ensure_account(
        self,
        *,
        scope_type: str,
        scope_key: str,
        period_start: datetime.datetime,
        period_end: datetime.datetime,
        token_limit: int,
        action_limit: int,
    ) -> int:
        self._validate_amounts(token_limit, action_limit)
        if token_limit <= 0 or action_limit <= 0:
            raise ValueError("budget account limits must be positive")
        if not scope_type or not scope_key or period_end <= period_start:
            raise ValueError("invalid budget account scope or period")
        db = self._session_factory()
        try:
            existing = db.query(self._account).filter(
                self._account.scope_type == scope_type,
                self._account.scope_key == scope_key,
                self._account.period_start == period_start,
            ).first()
            if existing is not None:
                self._assert_account_contract(existing, period_end, token_limit, action_limit)
                return int(existing.id)
            row = self._account(
                scope_type=scope_type,
                scope_key=scope_key,
                period_start=period_start,
                period_end=period_end,
                token_limit=token_limit,
                action_limit=action_limit,
                reserved_tokens=0,
                consumed_tokens=0,
                reserved_actions=0,
                consumed_actions=0,
                version=0,
            )
            db.add(row)
            db.commit()
            return int(row.id)
        except IntegrityError:
            db.rollback()
            existing = db.query(self._account).filter(
                self._account.scope_type == scope_type,
                self._account.scope_key == scope_key,
                self._account.period_start == period_start,
            ).first()
            if existing is None:
                raise
            self._assert_account_contract(existing, period_end, token_limit, action_limit)
            return int(existing.id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _assert_account_contract(row: Any, period_end: datetime.datetime, token_limit: int, action_limit: int) -> None:
        if (
            row.period_end != period_end
            or int(row.token_limit) != token_limit
            or int(row.action_limit) != action_limit
        ):
            raise BudgetReservationError("budget account limit conflict")

    def reserve(
        self,
        *,
        account_id: int,
        workflow_run_id: int,
        task_id: int,
        round_no: int,
        tokens: int,
        actions: int,
        idempotency_key: str,
        reason: str = "",
        detail: Optional[dict[str, Any]] = None,
    ) -> BudgetEntryResult:
        return self.reserve_many([BudgetReservationRequest(
            account_id=account_id,
            workflow_run_id=workflow_run_id,
            task_id=task_id,
            round_no=round_no,
            tokens=tokens,
            actions=actions,
            idempotency_key=idempotency_key,
            reason=reason,
            detail=detail,
        )])[0]

    def reserve_many(self, requests: list[BudgetReservationRequest]) -> list[BudgetEntryResult]:
        return self._reserve_many(requests)

    def reserve_many_in_session(
        self,
        db: Any,
        requests: list[BudgetReservationRequest],
    ) -> list[BudgetEntryResult]:
        return self._reserve_many(requests, db=db)

    def _reserve_many(
        self,
        requests: list[BudgetReservationRequest],
        *,
        db: Any = None,
    ) -> list[BudgetEntryResult]:
        if not requests:
            raise ValueError("at least one reservation is required")
        keys = [request.idempotency_key for request in requests]
        if len(set(keys)) != len(keys):
            raise ValueError("reservation idempotency keys must be unique")
        for request in requests:
            self._validate_request(request)
        ordered = sorted(requests, key=lambda item: (item.account_id, item.idempotency_key))
        owns_session = db is None
        db = db or self._session_factory()
        try:
            existing_rows = {key: self._ledger_by_key(db, key) for key in keys}
            if all(existing_rows.values()):
                for request in requests:
                    self._assert_same_reservation(
                        existing_rows[request.idempotency_key], request.account_id, request.task_id,
                        request.round_no, request.tokens, request.actions, request.workflow_run_id,
                    )
                return [self._entry_result(existing_rows[request.idempotency_key]) for request in requests]
            if any(existing_rows.values()):
                raise BudgetReservationError("reservation group is partially persisted")
            if any(self._ledger_by_key(db, self._reject_key(key)) is not None for key in keys):
                raise BudgetExceededError("budget account limit exceeded")

            account = self._account
            for request in ordered:
                updated = db.query(account).filter(
                    account.id == request.account_id,
                    account.reserved_tokens + account.consumed_tokens + request.tokens <= account.token_limit,
                    account.reserved_actions + account.consumed_actions + request.actions <= account.action_limit,
                ).update({
                    account.reserved_tokens: account.reserved_tokens + request.tokens,
                    account.reserved_actions: account.reserved_actions + request.actions,
                    account.version: account.version + 1,
                }, synchronize_session=False)
                if updated != 1:
                    if db.get(account, request.account_id) is None:
                        raise BudgetReservationError("budget account not found")
                    db.rollback()
                    self._record_rejections(requests)
                    raise BudgetExceededError("budget account limit exceeded")
            rows = []
            for request in requests:
                row = self._ledger(
                    account_id=request.account_id,
                    workflow_run_id=request.workflow_run_id,
                    task_id=request.task_id,
                    round_no=request.round_no,
                    entry_type="reserve",
                    token_delta=request.tokens,
                    action_delta=request.actions,
                    idempotency_key=request.idempotency_key,
                    reason=request.reason,
                    detail_payload=_dump_json(request.detail),
                )
                db.add(row)
                rows.append(row)
            if owns_session:
                db.commit()
            else:
                db.flush()
            return [self._entry_result(row) for row in rows]
        except IntegrityError:
            if not owns_session:
                raise
            db.rollback()
            persisted = {key: self._ledger_by_key(db, key) for key in keys}
            if all(persisted.values()):
                for request in requests:
                    self._assert_same_reservation(
                        persisted[request.idempotency_key], request.account_id, request.task_id,
                        request.round_no, request.tokens, request.actions, request.workflow_run_id,
                    )
                return [self._entry_result(persisted[request.idempotency_key]) for request in requests]
            if any(self._ledger_by_key(db, self._reject_key(key)) is not None for key in keys):
                raise BudgetExceededError("budget account limit exceeded")
            raise
        except Exception:
            if owns_session:
                db.rollback()
            raise
        finally:
            if owns_session:
                db.close()

    def _record_rejections(self, requests: list[BudgetReservationRequest]) -> None:
        db = self._session_factory()
        try:
            for request in requests:
                if self._ledger_by_key(db, self._reject_key(request.idempotency_key)) is not None:
                    continue
                db.add(self._ledger(
                    account_id=request.account_id,
                    workflow_run_id=request.workflow_run_id,
                    task_id=request.task_id,
                    round_no=request.round_no,
                    entry_type="reject",
                    token_delta=0,
                    action_delta=0,
                    idempotency_key=self._reject_key(request.idempotency_key),
                    reason="budget account limit exceeded",
                    detail_payload=_dump_json({
                        **(request.detail or {}),
                        "requestedTokens": request.tokens,
                        "requestedActions": request.actions,
                        "reservationKey": request.idempotency_key,
                    }),
                ))
            db.commit()
        except IntegrityError:
            db.rollback()
        finally:
            db.close()

    def settle(
        self,
        *,
        reservation_key: str,
        actual_tokens: int,
        actual_actions: int,
        idempotency_key: str,
        reason: str = "",
        detail: Optional[dict[str, Any]] = None,
    ) -> BudgetEntryResult:
        self._validate_amounts(actual_tokens, actual_actions)
        return self._close_reservation(
            reservation_key=reservation_key,
            idempotency_key=idempotency_key,
            entry_type="settle",
            actual_tokens=actual_tokens,
            actual_actions=actual_actions,
            reason=reason,
            detail=detail,
        )

    def settle_many(self, requests: list[BudgetSettlementRequest]) -> list[BudgetEntryResult]:
        return self._close_many(requests, entry_type="settle")

    def settle_many_in_session(
        self,
        db: Any,
        requests: list[BudgetSettlementRequest],
    ) -> list[BudgetEntryResult]:
        return self._close_many(requests, entry_type="settle", db=db)

    def release_many(self, requests: list[BudgetSettlementRequest]) -> list[BudgetEntryResult]:
        normalized = [BudgetSettlementRequest(
            reservation_key=item.reservation_key,
            actual_tokens=0,
            actual_actions=0,
            idempotency_key=item.idempotency_key,
            reason=item.reason,
            detail=item.detail,
        ) for item in requests]
        return self._close_many(normalized, entry_type="release")

    def release_many_in_session(
        self,
        db: Any,
        requests: list[BudgetSettlementRequest],
    ) -> list[BudgetEntryResult]:
        normalized = [BudgetSettlementRequest(
            reservation_key=item.reservation_key,
            actual_tokens=0,
            actual_actions=0,
            idempotency_key=item.idempotency_key,
            reason=item.reason,
            detail=item.detail,
        ) for item in requests]
        return self._close_many(normalized, entry_type="release", db=db)

    def settle_outstanding_for_run_in_session(
        self,
        db: Any,
        *,
        workflow_run_id: int,
    ) -> list[BudgetEntryResult]:
        reservations = db.query(self._ledger).filter(
            self._ledger.workflow_run_id == workflow_run_id,
            self._ledger.entry_type == "reserve",
            self._ledger.idempotency_key.like("expert:%"),
        ).all()
        requests = []
        for reservation in reservations:
            reservation_key = str(reservation.idempotency_key)
            if self._terminal_for_reservation(db, reservation_key) is not None:
                continue
            requests.append(BudgetSettlementRequest(
                reservation_key=reservation_key,
                actual_tokens=max(0, int(reservation.token_delta or 0)),
                actual_actions=max(0, int(reservation.action_delta or 0)),
                idempotency_key=f"expert:stale-run:{workflow_run_id}:{reservation.id}:settle",
                reason="stale expert operation conservative settlement",
                detail={"recoveredWorkflowRunId": workflow_run_id},
            ))
        if not requests:
            return []
        return self._close_many(requests, entry_type="settle", db=db)

    def _close_many(
        self,
        requests: list[BudgetSettlementRequest],
        *,
        entry_type: str,
        db: Any = None,
    ) -> list[BudgetEntryResult]:
        if not requests:
            raise ValueError("at least one settlement is required")
        if len({item.idempotency_key for item in requests}) != len(requests):
            raise ValueError("settlement idempotency keys must be unique")
        if len({item.reservation_key for item in requests}) != len(requests):
            raise BudgetReservationError("settlement reservation keys must be unique")
        for item in requests:
            self._validate_amounts(item.actual_tokens, item.actual_actions)
            if not item.reservation_key or not item.idempotency_key:
                raise ValueError("reservation_key and idempotency_key are required")
        owns_session = db is None
        db = db or self._session_factory()
        try:
            existing = {item.idempotency_key: self._ledger_by_key(db, item.idempotency_key) for item in requests}
            if all(existing.values()):
                for item in requests:
                    self._assert_same_terminal(existing[item.idempotency_key], item, entry_type)
                return [self._entry_result(existing[item.idempotency_key]) for item in requests]
            if any(existing.values()):
                raise BudgetReservationError("settlement group is partially persisted")
            reservations = {}
            for item in requests:
                reservation = self._ledger_by_key(db, item.reservation_key)
                if reservation is None or reservation.entry_type != "reserve":
                    raise BudgetReservationError("reservation not found")
                if (
                    not item.allow_overage
                    and (
                        item.actual_tokens > int(reservation.token_delta or 0)
                        or item.actual_actions > int(reservation.action_delta or 0)
                    )
                ):
                    raise BudgetReservationError("actual usage exceeds reservation")
                reservations[item.reservation_key] = reservation

            account_ids = sorted({int(row.account_id) for row in reservations.values()})
            for account_id in account_ids:
                locked = db.query(self._account).filter(self._account.id == account_id).update({
                    self._account.version: self._account.version + 1,
                }, synchronize_session=False)
                if locked != 1:
                    raise BudgetReservationError("budget account not found")
            for item in requests:
                if self._terminal_for_reservation(db, item.reservation_key) is not None:
                    raise BudgetReservationError("reservation already settled or released")

            totals: dict[int, dict[str, int]] = {}
            for item in requests:
                reservation = reservations[item.reservation_key]
                account_totals = totals.setdefault(int(reservation.account_id), {
                    "reserved_tokens": 0, "reserved_actions": 0,
                    "actual_tokens": 0, "actual_actions": 0,
                })
                account_totals["reserved_tokens"] += int(reservation.token_delta or 0)
                account_totals["reserved_actions"] += int(reservation.action_delta or 0)
                account_totals["actual_tokens"] += item.actual_tokens
                account_totals["actual_actions"] += item.actual_actions
            for account_id, values in totals.items():
                updated = db.query(self._account).filter(
                    self._account.id == account_id,
                    self._account.reserved_tokens >= values["reserved_tokens"],
                    self._account.reserved_actions >= values["reserved_actions"],
                ).update({
                    self._account.reserved_tokens: self._account.reserved_tokens - values["reserved_tokens"],
                    self._account.reserved_actions: self._account.reserved_actions - values["reserved_actions"],
                    self._account.consumed_tokens: self._account.consumed_tokens + values["actual_tokens"],
                    self._account.consumed_actions: self._account.consumed_actions + values["actual_actions"],
                }, synchronize_session=False)
                if updated != 1:
                    raise BudgetReservationError("reservation counters are inconsistent")
            rows = []
            for item in requests:
                reservation = reservations[item.reservation_key]
                detail = dict(item.detail or {})
                detail.update({
                    "reservationKey": item.reservation_key,
                    "reservedTokens": int(reservation.token_delta or 0),
                    "reservedActions": int(reservation.action_delta or 0),
                    "overageTokens": max(0, item.actual_tokens - int(reservation.token_delta or 0)),
                    "overageActions": max(0, item.actual_actions - int(reservation.action_delta or 0)),
                })
                row = self._ledger(
                    account_id=reservation.account_id,
                    workflow_run_id=reservation.workflow_run_id,
                    task_id=reservation.task_id,
                    round_no=reservation.round_no,
                    entry_type=entry_type,
                    token_delta=item.actual_tokens if entry_type == "settle" else -int(reservation.token_delta or 0),
                    action_delta=item.actual_actions if entry_type == "settle" else -int(reservation.action_delta or 0),
                    idempotency_key=item.idempotency_key,
                    reason=item.reason,
                    detail_payload=_dump_json(detail),
                )
                db.add(row)
                rows.append(row)
            if owns_session:
                db.commit()
            else:
                db.flush()
            return [self._entry_result(row) for row in rows]
        except Exception:
            if owns_session:
                db.rollback()
            raise
        finally:
            if owns_session:
                db.close()

    @staticmethod
    def _assert_same_terminal(row: Any, request: BudgetSettlementRequest, entry_type: str) -> None:
        detail = _json_object(row.detail_payload)
        matches = str(row.entry_type) == entry_type and detail.get("reservationKey") == request.reservation_key
        if not matches:
            raise BudgetReservationError("idempotency key payload conflict")

    def release(
        self,
        *,
        reservation_key: str,
        idempotency_key: str,
        reason: str = "",
        detail: Optional[dict[str, Any]] = None,
    ) -> BudgetEntryResult:
        return self._close_reservation(
            reservation_key=reservation_key,
            idempotency_key=idempotency_key,
            entry_type="release",
            actual_tokens=0,
            actual_actions=0,
            reason=reason,
            detail=detail,
        )

    def _close_reservation(
        self,
        *,
        reservation_key: str,
        idempotency_key: str,
        entry_type: str,
        actual_tokens: int,
        actual_actions: int,
        reason: str,
        detail: Optional[dict[str, Any]],
    ) -> BudgetEntryResult:
        if not reservation_key or not idempotency_key:
            raise ValueError("reservation_key and idempotency_key are required")
        db = self._session_factory()
        try:
            existing = self._ledger_by_key(db, idempotency_key)
            if existing is not None:
                existing_detail = _json_object(existing.detail_payload)
                matches = (
                    str(existing.entry_type) == entry_type
                    and existing_detail.get("reservationKey") == reservation_key
                )
                if not matches:
                    raise BudgetReservationError("idempotency key payload conflict")
                return self._entry_result(existing)

            reservation = self._ledger_by_key(db, reservation_key)
            if reservation is None or reservation.entry_type != "reserve":
                raise BudgetReservationError("reservation not found")
            reserved_tokens = int(reservation.token_delta or 0)
            reserved_actions = int(reservation.action_delta or 0)
            if actual_tokens > reserved_tokens or actual_actions > reserved_actions:
                raise BudgetReservationError("actual usage exceeds reservation")

            account = self._account
            # The version update acquires the account row before terminal-ledger inspection.
            locked = db.query(account).filter(account.id == reservation.account_id).update({
                account.version: account.version + 1,
            }, synchronize_session=False)
            if locked != 1:
                raise BudgetReservationError("budget account not found")
            terminal = self._terminal_for_reservation(db, reservation_key)
            if terminal is not None:
                if terminal.idempotency_key == idempotency_key and terminal.entry_type == entry_type:
                    db.rollback()
                    return self._entry_result(terminal)
                raise BudgetReservationError("reservation already settled or released")

            updated = db.query(account).filter(
                account.id == reservation.account_id,
                account.reserved_tokens >= reserved_tokens,
                account.reserved_actions >= reserved_actions,
            ).update({
                account.reserved_tokens: account.reserved_tokens - reserved_tokens,
                account.reserved_actions: account.reserved_actions - reserved_actions,
                account.consumed_tokens: account.consumed_tokens + actual_tokens,
                account.consumed_actions: account.consumed_actions + actual_actions,
            }, synchronize_session=False)
            if updated != 1:
                raise BudgetReservationError("reservation counters are inconsistent")

            payload = dict(detail or {})
            payload["reservationKey"] = reservation_key
            payload["reservedTokens"] = reserved_tokens
            payload["reservedActions"] = reserved_actions
            ledger = self._ledger(
                account_id=reservation.account_id,
                workflow_run_id=reservation.workflow_run_id,
                task_id=reservation.task_id,
                round_no=reservation.round_no,
                entry_type=entry_type,
                token_delta=actual_tokens if entry_type == "settle" else -reserved_tokens,
                action_delta=actual_actions if entry_type == "settle" else -reserved_actions,
                idempotency_key=idempotency_key,
                reason=reason,
                detail_payload=_dump_json(payload),
            )
            db.add(ledger)
            db.commit()
            return self._entry_result(ledger)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _terminal_for_reservation(self, db: Any, reservation_key: str) -> Any:
        candidates = db.query(self._ledger).filter(
            self._ledger.entry_type.in_(["settle", "release"]),
        ).all()
        for candidate in candidates:
            if _json_object(candidate.detail_payload).get("reservationKey") == reservation_key:
                return candidate
        return None

    def _ledger_by_key(self, db: Any, key: str) -> Any:
        return db.query(self._ledger).filter(self._ledger.idempotency_key == key).first()

    @staticmethod
    def _reject_key(idempotency_key: str) -> str:
        return f"{idempotency_key}:reject"

    @staticmethod
    def _validate_amounts(tokens: int, actions: int) -> None:
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("tokens must be a non-negative integer")
        if isinstance(actions, bool) or not isinstance(actions, int) or actions < 0:
            raise ValueError("actions must be a non-negative integer")

    @classmethod
    def _validate_request(cls, request: BudgetReservationRequest) -> None:
        cls._validate_amounts(request.tokens, request.actions)
        if not isinstance(request.workflow_run_id, int) or isinstance(request.workflow_run_id, bool):
            raise ValueError("workflow_run_id must be an integer")
        if not request.idempotency_key:
            raise ValueError("idempotency_key is required")

    @staticmethod
    def _entry_result(row: Any) -> BudgetEntryResult:
        return BudgetEntryResult(int(row.id), str(row.entry_type), str(row.idempotency_key))

    @staticmethod
    def _assert_same_reservation(
        row: Any,
        account_id: int,
        task_id: int,
        round_no: int,
        tokens: int,
        actions: int,
        workflow_run_id: Optional[int] = None,
    ) -> None:
        identity = (
            str(row.entry_type), int(row.account_id), int(row.task_id), int(row.round_no),
            int(row.token_delta or 0), int(row.action_delta or 0),
        )
        if identity != ("reserve", account_id, task_id, round_no, tokens, actions):
            raise BudgetReservationError("idempotency key payload conflict")
        if workflow_run_id is not None and int(row.workflow_run_id) != workflow_run_id:
            raise BudgetReservationError("idempotency key payload conflict")


class TopQueryWorkflowRunService:
    ACTIVE_SLOT = "active"

    def __init__(self, session_factory: Callable[[], Any], models: WorkflowRunModels):
        self._session_factory = session_factory
        self._run = models.run

    def claim(
        self,
        *,
        task_id: int,
        run_key: str,
        worker_id: str = "legacy-local-worker",
        lease_seconds: int = 60,
        now: Optional[datetime.datetime] = None,
    ) -> WorkflowRunSnapshot:
        if not run_key or not worker_id:
            raise ValueError("run_key and worker_id are required")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        now_value = now or datetime.datetime.now()
        lease_expires_at = now_value + datetime.timedelta(seconds=lease_seconds)
        db = self._session_factory()
        try:
            existing_key = db.query(self._run).filter(self._run.run_key == run_key).first()
            if existing_key is not None:
                if int(existing_key.task_id) != task_id:
                    raise RuntimeError("run_key belongs to another task")
                if existing_key.active_slot != self.ACTIVE_SLOT:
                    return self._snapshot(existing_key)
                return self._acquire_lease(
                    db, existing_key.id, worker_id, now_value, lease_expires_at,
                )
            active = self._active_query(db, task_id).first()
            if active is not None:
                return self._acquire_lease(
                    db, active.id, worker_id, now_value, lease_expires_at,
                )
            row = self._run(
                task_id=task_id,
                run_key=run_key,
                active_slot=self.ACTIVE_SLOT,
                status="running",
                current_node="context",
                round_no=0,
                checkpoint_payload="{}",
                checkpoint_version=0,
                consecutive_planner_failures=0,
                consecutive_tool_failures=0,
                consecutive_no_improvement=0,
                started_at=now_value,
                heartbeat_at=now_value,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
            )
            db.add(row)
            db.commit()
            return self._snapshot(row)
        except IntegrityError:
            db.rollback()
            active = self._active_query(db, task_id).first()
            if active is None:
                raise
            return self._acquire_lease(
                db, active.id, worker_id, now_value, lease_expires_at,
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _acquire_lease(
        self,
        db: Any,
        run_id: int,
        worker_id: str,
        now: datetime.datetime,
        lease_expires_at: datetime.datetime,
    ) -> WorkflowRunSnapshot:
        run = self._run
        updated = db.query(run).filter(
            run.id == run_id,
            run.active_slot == self.ACTIVE_SLOT,
            (
                (run.lease_owner.is_(None))
                | (run.lease_owner == worker_id)
                | (run.lease_expires_at.is_(None))
                | (run.lease_expires_at <= now)
            ),
        ).update({
            run.lease_owner: worker_id,
            run.lease_expires_at: lease_expires_at,
            run.heartbeat_at: now,
        }, synchronize_session=False)
        if updated != 1:
            db.rollback()
            raise WorkflowRunBusyError("workflow run is leased by another worker")
        db.commit()
        db.expire_all()
        return self._snapshot(db.get(run, run_id))

    def load_active(self, *, task_id: int) -> Optional[WorkflowRunSnapshot]:
        db = self._session_factory()
        try:
            row = self._active_query(db, task_id).first()
            return self._snapshot(row) if row is not None else None
        finally:
            db.close()

    def heartbeat(
        self,
        *,
        run_id: int,
        worker_id: str,
        lease_seconds: int = 60,
        now: Optional[datetime.datetime] = None,
    ) -> WorkflowRunSnapshot:
        now_value = now or datetime.datetime.now()
        db = self._session_factory()
        try:
            run = self._run
            updated = db.query(run).filter(
                run.id == run_id,
                run.active_slot == self.ACTIVE_SLOT,
                run.lease_owner == worker_id,
                run.lease_expires_at > now_value,
            ).update({
                run.heartbeat_at: now_value,
                run.lease_expires_at: now_value + datetime.timedelta(seconds=lease_seconds),
            }, synchronize_session=False)
            if updated != 1:
                raise WorkflowRunBusyError("workflow run lease is not owned by worker")
            db.commit()
            return self._snapshot(db.get(run, run_id))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def write_if_owned(
        self,
        *,
        run_id: int,
        worker_id: str,
        write: Callable[[Any], Any],
        now: Optional[datetime.datetime] = None,
    ) -> Any:
        db = self._session_factory()
        try:
            run = self._run
            now_value = now or datetime.datetime.now()
            updated = db.query(run).filter(
                run.id == run_id,
                run.active_slot == self.ACTIVE_SLOT,
                run.lease_owner == worker_id,
                run.lease_expires_at > now_value,
            ).update({run.heartbeat_at: now_value}, synchronize_session=False)
            if updated != 1:
                raise WorkflowRunBusyError("workflow run lease is not owned by worker")
            result = write(db)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def save_checkpoint(
        self,
        *,
        run_id: int,
        expected_version: int,
        current_node: str,
        round_no: int,
        checkpoint: dict[str, Any],
        worker_id: str = "legacy-local-worker",
        lease_seconds: int = 60,
        now: Optional[datetime.datetime] = None,
        planner_failures: int = 0,
        tool_failures: int = 0,
        no_improvement: int = 0,
    ) -> WorkflowRunSnapshot:
        counters = (planner_failures, tool_failures, no_improvement)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counters):
            raise ValueError("failure counters must be non-negative integers")
        db = self._session_factory()
        try:
            run = self._run
            now_value = now or datetime.datetime.now()
            updated = db.query(run).filter(
                run.id == run_id,
                run.active_slot == self.ACTIVE_SLOT,
                run.checkpoint_version == expected_version,
                run.lease_owner == worker_id,
                run.lease_expires_at > now_value,
            ).update({
                run.current_node: current_node,
                run.round_no: round_no,
                run.checkpoint_payload: _dump_json(checkpoint),
                run.checkpoint_version: expected_version + 1,
                run.consecutive_planner_failures: planner_failures,
                run.consecutive_tool_failures: tool_failures,
                run.consecutive_no_improvement: no_improvement,
                run.heartbeat_at: now_value,
                run.lease_expires_at: now_value + datetime.timedelta(seconds=lease_seconds),
            }, synchronize_session=False)
            if updated != 1:
                current = db.get(run, run_id)
                if current is not None and current.lease_owner != worker_id:
                    raise WorkflowRunBusyError("workflow run lease is not owned by worker")
                raise RuntimeError("checkpoint version conflict")
            db.commit()
            row = db.get(run, run_id)
            return self._snapshot(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def finish(
        self,
        *,
        run_id: int,
        status: str,
        expected_version: int,
        worker_id: str = "legacy-local-worker",
        now: Optional[datetime.datetime] = None,
    ) -> WorkflowRunSnapshot:
        if status not in {
            "completed", "manual_required", "failed", "cancelled", "skipped", "superseded",
        }:
            raise ValueError("invalid terminal workflow run status")
        db = self._session_factory()
        try:
            run = self._run
            now_value = now or datetime.datetime.now()
            updated = db.query(run).filter(
                run.id == run_id,
                run.active_slot == self.ACTIVE_SLOT,
                run.checkpoint_version == expected_version,
                run.lease_owner == worker_id,
                run.lease_expires_at > now_value,
            ).update({
                run.status: status,
                run.active_slot: None,
                run.finished_at: now_value,
                run.heartbeat_at: now_value,
                run.checkpoint_version: expected_version + 1,
                run.lease_owner: None,
                run.lease_expires_at: None,
            }, synchronize_session=False)
            if updated != 1:
                current = db.get(run, run_id)
                if current is not None and current.lease_owner != worker_id:
                    raise WorkflowRunBusyError("workflow run lease is not owned by worker")
                raise RuntimeError("workflow run finish conflict")
            db.commit()
            return self._snapshot(db.get(run, run_id))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def finish_with_writes(
        self,
        *,
        run_id: int,
        status: str,
        expected_version: int,
        worker_id: str,
        write: Callable[[Any], Any],
        now: Optional[datetime.datetime] = None,
    ) -> tuple[WorkflowRunSnapshot, Any]:
        if status not in {
            "completed", "manual_required", "failed", "cancelled", "skipped", "superseded",
        }:
            raise ValueError("invalid terminal workflow run status")
        db = self._session_factory()
        try:
            run = self._run
            now_value = now or datetime.datetime.now()
            updated = db.query(run).filter(
                run.id == run_id,
                run.active_slot == self.ACTIVE_SLOT,
                run.checkpoint_version == expected_version,
                run.lease_owner == worker_id,
                run.lease_expires_at > now_value,
            ).update({
                run.status: status,
                run.active_slot: None,
                run.finished_at: now_value,
                run.heartbeat_at: now_value,
                run.checkpoint_version: expected_version + 1,
                run.lease_owner: None,
                run.lease_expires_at: None,
            }, synchronize_session=False)
            if updated != 1:
                current = db.get(run, run_id)
                if current is not None and current.lease_owner != worker_id:
                    raise WorkflowRunBusyError("workflow run lease is not owned by worker")
                raise RuntimeError("workflow run finish conflict")
            result = write(db)
            db.commit()
            return self._snapshot(db.get(run, run_id)), result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _active_query(self, db: Any, task_id: int) -> Any:
        return db.query(self._run).filter(
            self._run.task_id == task_id,
            self._run.active_slot == self.ACTIVE_SLOT,
        )

    @staticmethod
    def _snapshot(row: Any) -> WorkflowRunSnapshot:
        return WorkflowRunSnapshot(
            id=int(row.id),
            task_id=int(row.task_id),
            run_key=str(row.run_key),
            status=str(row.status),
            current_node=str(row.current_node) if row.current_node is not None else None,
            round_no=int(row.round_no or 0),
            checkpoint=_json_object(row.checkpoint_payload),
            checkpoint_version=int(row.checkpoint_version or 0),
            planner_failures=int(row.consecutive_planner_failures or 0),
            tool_failures=int(row.consecutive_tool_failures or 0),
            no_improvement=int(row.consecutive_no_improvement or 0),
            lease_owner=str(row.lease_owner) if row.lease_owner is not None else None,
            lease_expires_at=row.lease_expires_at,
        )


def create_top_query_persistence_services() -> tuple[
    TopQueryAtomicBudgetService,
    TopQueryWorkflowRunService,
]:
    from src.persistence import (
        SessionLocal,
        TopQueryBudgetAccount,
        TopQueryBudgetLedger,
        TopQueryWorkflowRun,
    )

    return (
        TopQueryAtomicBudgetService(
            SessionLocal,
            BudgetModels(account=TopQueryBudgetAccount, ledger=TopQueryBudgetLedger),
        ),
        TopQueryWorkflowRunService(
            SessionLocal,
            WorkflowRunModels(run=TopQueryWorkflowRun),
        ),
    )
