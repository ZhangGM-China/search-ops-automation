"""Database-backed model metrics for the Query governance workflow."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from .models import QueryGovernanceAgentEvent


@dataclass(frozen=True)
class ModelMetrics:
    model_name: str
    token_usage: int
    model_role: str = "primary"
    fallback_used: bool = False
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.model_name.strip():
            raise ValueError("model_name is required for model metrics")
        if self.model_role not in {"primary", "fallback"}:
            raise ValueError("model_role must be primary or fallback")
        if self.token_usage < 0:
            raise ValueError("token_usage must be non-negative")

    def as_storage_fields(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name.strip(),
            "model_role": self.model_role,
            "fallback_used": self.fallback_used,
            "token_usage": self.token_usage,
            "model_error_code": self.error_code,
        }


def _json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def serialize_governance_event(
    event: QueryGovernanceAgentEvent,
) -> dict[str, Any]:
    """Serialize an event without inventing model use for engineering nodes."""

    payload: dict[str, Any] = {
        "id": event.id,
        "taskId": event.task_id,
        "brand": event.brand,
        "query": event.query_text,
        "agentName": event.agent_name,
        "nodeKey": event.node_key,
        "eventType": event.event_type,
    }
    output = _json_object(event.output_json)
    if output is not None:
        payload["payload"] = output
    if event.error_code:
        payload["errorCode"] = event.error_code
    if event.duration_seconds is not None:
        payload["durationSeconds"] = float(event.duration_seconds)
    if event.created_at is not None:
        payload["createdAt"] = event.created_at.isoformat()

    if event.model_name:
        payload["modelMetrics"] = {
            "modelName": event.model_name,
            "modelRole": event.model_role or "primary",
            "fallbackUsed": bool(event.fallback_used),
            "durationSeconds": float(event.duration_seconds or 0),
            "errorCode": event.model_error_code,
        }
        if event.token_usage is not None:
            payload["modelMetrics"]["tokenUsage"] = int(event.token_usage)
    return payload


__all__ = ["ModelMetrics", "serialize_governance_event"]
