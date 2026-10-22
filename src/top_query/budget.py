from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Optional

from src.persistence import (
    SessionLocal,
    TopQueryActionPlan,
    TopQueryAgentEvent,
)


_CONFIG_KEYS = (
    "top_query_token_limit",
    "top_query_active_duration_seconds",
    "top_query_round_action_limit",
    "top_query_fingerprint_window",
)

@dataclass(frozen=True)
class BudgetConfig:
    token_limit: int = 0
    active_duration_seconds: float = 0
    round_action_limit: int = 0
    fingerprint_window: int = 0
    valid: bool = False
    error: str = ""

    @classmethod
    def from_mapping(cls, config: Optional[Mapping[str, Any]]) -> "BudgetConfig":
        values = config or {}
        parsed: dict[str, Any] = {}
        for key in _CONFIG_KEYS:
            if key not in values or values.get(key) in (None, ""):
                return cls(error=f"missing budget config: {key}")
            value = values[key]
            if key == "top_query_active_duration_seconds":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return cls(error=f"invalid budget config: {key}")
                parsed[key] = float(value)
                if not math.isfinite(parsed[key]):
                    return cls(error=f"invalid budget config: {key}")
            elif isinstance(value, bool) or not isinstance(value, int):
                return cls(error=f"invalid budget config: {key}")
            else:
                parsed[key] = value
            if parsed[key] <= 0:
                return cls(error=f"invalid budget config: {key}")
        return cls(
            token_limit=parsed["top_query_token_limit"],
            active_duration_seconds=parsed["top_query_active_duration_seconds"],
            round_action_limit=parsed["top_query_round_action_limit"],
            fingerprint_window=parsed["top_query_fingerprint_window"],
            valid=True,
        )


@dataclass(frozen=True)
class QueryBudgetSnapshot:
    token_usage: int
    active_duration_seconds: float
    action_count: int
    action_fingerprints: list[str]
    plan_fingerprints: list[str]


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str = ""
    detail: Optional[dict[str, Any]] = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


_VOLATILE_FINGERPRINT_KEYS = {
    "reason", "evidence", "telemetry", "llmmetrics", "agentmetrics",
    "createdat", "updatedat", "requestedat", "duration", "tokenusage",
    "confidence", "expectedimpact", "negativeimpact",
}


def _normalized_effect(value: Any) -> Any:
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            compact_key = "".join(character for character in str(key).lower() if character.isalnum())
            if compact_key in _VOLATILE_FINGERPRINT_KEYS:
                continue
            normalized[str(key)] = _normalized_effect(item)
        return normalized
    if isinstance(value, list):
        return [_normalized_effect(item) for item in value]
    return value


def action_fingerprint(action_type: str, check_key: str, payload: Any) -> str:
    return _hash({
        "actionType": str(action_type or ""),
        "checkKey": str(check_key or ""),
        "payload": _normalized_effect(payload) if isinstance(payload, (dict, list)) else {},
    })


def plan_fingerprint(payload: Any) -> str:
    source = payload if isinstance(payload, dict) else {}
    return _hash(_normalized_effect({
        "checks": source.get("checks") or [],
        "actions": source.get("actions") or [],
        "strategy": source.get("strategy") or {},
        "stages": source.get("stages") or [],
    }))


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_query_budget(task_id: int, *, now: Any = None) -> QueryBudgetSnapshot:
    db = SessionLocal()
    try:
        events = db.query(TopQueryAgentEvent).filter(
            TopQueryAgentEvent.task_id == task_id,
        ).order_by(TopQueryAgentEvent.id.asc()).all()
        actions = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
        ).order_by(TopQueryActionPlan.id.asc()).all()
    finally:
        db.close()

    active_duration = sum(max(0.0, float(row.duration or 0.0)) for row in events)

    action_fingerprints = [
        action_fingerprint(row.action_type, row.check_key, _load_json(row.request_payload))
        for row in actions
    ]
    plan_fingerprints = [
        plan_fingerprint(_load_json(row.output_payload))
        for row in events
        if row.event_type == "plan_created"
    ]
    return QueryBudgetSnapshot(
        token_usage=sum(max(0, int(row.token_usage or 0)) for row in events),
        active_duration_seconds=active_duration,
        action_count=len(actions),
        action_fingerprints=action_fingerprints,
        plan_fingerprints=plan_fingerprints,
    )


def check_before_planner(config: BudgetConfig, snapshot: QueryBudgetSnapshot) -> BudgetDecision:
    if not config.valid:
        return BudgetDecision(False, "invalid_budget_config", {"error": config.error})
    if snapshot.token_usage >= config.token_limit:
        return BudgetDecision(False, "query_token_limit_reached", {
            "used": snapshot.token_usage,
            "limit": config.token_limit,
        })
    if snapshot.active_duration_seconds >= config.active_duration_seconds:
        return BudgetDecision(False, "query_active_duration_limit_reached", {
            "usedSeconds": snapshot.active_duration_seconds,
            "limitSeconds": config.active_duration_seconds,
        })
    return BudgetDecision(True)


def check_plan(config: BudgetConfig, snapshot: QueryBudgetSnapshot, payload: dict[str, Any]) -> BudgetDecision:
    if not config.valid:
        return BudgetDecision(False, "invalid_budget_config", {"error": config.error})
    fingerprint = plan_fingerprint(payload)
    recent = snapshot.plan_fingerprints[-config.fingerprint_window:]
    if fingerprint in recent:
        return BudgetDecision(False, "duplicate_plan_fingerprint", {
            "fingerprint": fingerprint,
            "window": config.fingerprint_window,
        })
    return BudgetDecision(True, detail={"fingerprint": fingerprint})


def check_round_actions(
    config: BudgetConfig,
    snapshot: QueryBudgetSnapshot,
    actions: list[dict[str, Any]],
) -> BudgetDecision:
    if not config.valid:
        return BudgetDecision(False, "invalid_budget_config", {"error": config.error})
    if len(actions) > config.round_action_limit:
        return BudgetDecision(False, "round_action_limit_exceeded", {
            "planned": len(actions),
            "limit": config.round_action_limit,
        })
    fingerprints = [
        action_fingerprint(item.get("actionType"), item.get("checkKey"), item.get("payload"))
        for item in actions
    ]
    if len(set(fingerprints)) != len(fingerprints):
        return BudgetDecision(False, "duplicate_action_fingerprint", {
            "scope": "current_round",
        })
    recent = set(snapshot.action_fingerprints[-config.fingerprint_window:])
    duplicate = next((value for value in fingerprints if value in recent), None)
    if duplicate:
        return BudgetDecision(False, "duplicate_action_fingerprint", {
            "scope": "persisted_window",
            "fingerprint": duplicate,
            "window": config.fingerprint_window,
        })
    return BudgetDecision(True, detail={"fingerprints": fingerprints})
