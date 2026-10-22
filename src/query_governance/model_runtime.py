"""Runtime wrapper for configured Query-governance model calls."""

from __future__ import annotations

import time
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import uuid4

from src.llm.node_config import (
    NodeConfigError,
    NodeConfigSnapshot,
    ResolvedNodeConfig,
    get_snapshot_node_config,
    model_audit_fields,
)
from src.llm.rd_client import FallbackChatModel, build_rd_chat_model


@dataclass(frozen=True)
class ModelInvocationResult:
    response: Any
    audit: Mapping[str, Any]


class QueryGovernanceModelRuntime:
    """Invoke one configured node with deterministic primary/fallback policy."""

    def __init__(
        self,
        node_key: str,
        model_config_snapshot: NodeConfigSnapshot,
        *,
        llm: Any,
    ) -> None:
        self.node_key = node_key
        self.model_config: ResolvedNodeConfig = get_snapshot_node_config(
            model_config_snapshot, node_key
        )
        self.model_config_snapshot = model_config_snapshot
        self.llm = llm

    @classmethod
    def from_snapshot(
        cls,
        node_key: str,
        model_config_snapshot: NodeConfigSnapshot,
        *,
        timeout: float,
        temperature: float = 0.1,
        max_completion_tokens: int | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> "QueryGovernanceModelRuntime":
        """Build the primary/fallback clients from one immutable task snapshot."""
        config = get_snapshot_node_config(model_config_snapshot, node_key)
        common = {
            "timeout": timeout,
            "default_model": config.primary_model,
            "temperature": temperature,
            "max_retries": 0,
            "max_completion_tokens": max_completion_tokens,
        }
        if extra_body:
            common["extra_body"] = dict(extra_body)
        primary = build_rd_chat_model(**common)
        if primary is None:
            raise NodeConfigError(f"{node_key} primary model is unavailable")
        fallback = None
        if config.fallback_model:
            fallback = build_rd_chat_model(
                **{**common, "default_model": config.fallback_model}
            )
        return cls(
            node_key,
            model_config_snapshot,
            llm=FallbackChatModel(primary=primary, fallback=fallback),
        )

    def invoke(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        config: Mapping[str, Any] | None = None,
        callbacks: Sequence[Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        run_name: str | None = None,
        **kwargs: Any,
    ) -> ModelInvocationResult:
        invocation_config = dict(config or {})
        if callbacks is not None:
            invocation_config["callbacks"] = list(callbacks)
        if metadata is not None:
            merged_metadata = dict(invocation_config.get("metadata") or {})
            merged_metadata.update(metadata)
            invocation_config["metadata"] = merged_metadata
        if run_name is not None:
            invocation_config["run_name"] = run_name
        if invocation_config:
            kwargs["config"] = invocation_config
        started = time.perf_counter()
        model_role = "primary"
        calls: list[dict[str, Any]] = []

        def call(role: str, method: Any) -> Any:
            attempt_started = time.perf_counter()
            record = {**model_audit_fields(self.model_config, role),
                      "requestId": uuid4().hex, "tokenUsage": None,
                      "configVersion": self.model_config_snapshot.version}
            calls.append(record)
            try:
                response = method(messages, **kwargs)
                record["tokenUsage"] = _token_usage(response)
                return response
            except Exception as exc:
                exc.model_call_audits = calls
                raise
            finally:
                record["durationSeconds"] = max(0.0, time.perf_counter() - attempt_started)
        try:
            invoke_primary = getattr(self.llm, "invoke_primary", None)
            if not callable(invoke_primary):
                raise NodeConfigError("configured model runtime has no primary invocation")
            response = call("primary", invoke_primary)
        except Exception:
            if not self.model_config.fallback_model:
                raise
            invoke_fallback = getattr(self.llm, "invoke_fallback", None)
            if not callable(invoke_fallback):
                raise
            response = call("fallback", invoke_fallback)
            model_role = "fallback"

        audit = model_audit_fields(self.model_config, model_role)
        audit.update(
            {
                "configVersion": self.model_config_snapshot.version,
                "tokenUsage": _token_usage(response),
                "modelCallAudits": calls,
                "durationSeconds": max(0.0, time.perf_counter() - started),
            }
        )
        return ModelInvocationResult(response=response, audit=audit)


def _token_usage(response: Any) -> int | None:
    def valid(value: Any) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0 and int(value) == value)

    metadata = getattr(response, "response_metadata", None)
    for usage in (getattr(response, "usage_metadata", None),
                  metadata.get("token_usage") if isinstance(metadata, Mapping) else None):
        if not isinstance(usage, Mapping):
            continue
        total = usage.get("total_tokens")
        if valid(total):
            return int(total)
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        if valid(input_tokens) and valid(output_tokens):
            return int(input_tokens) + int(output_tokens)
    return None


__all__ = ["ModelInvocationResult", "QueryGovernanceModelRuntime"]
