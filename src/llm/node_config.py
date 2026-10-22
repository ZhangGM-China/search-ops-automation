"""Pure node-level model configuration resolution and task snapshots.

This module deliberately has no database or API dependency. A later release
service can supply the published override mapping without changing the
resolution contract used by runtime entry points.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping, Optional, Sequence


class NodeConfigError(ValueError):
    """Raised when a node or its published model override is invalid."""


@dataclass(frozen=True)
class NodeSpec:
    node_key: str
    channel: str
    primary_env: str
    fallback_env: str
    default_primary: str
    default_fallback: Optional[str]
    required_capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ResolvedNodeConfig:
    node_key: str
    channel: str
    primary_model: str
    fallback_model: Optional[str]
    primary_source: str
    fallback_source: str


@dataclass(frozen=True)
class NodeConfigSnapshot:
    captured_at: str
    nodes: Mapping[str, ResolvedNodeConfig]
    version: Optional[int] = None


def _rd_spec(
    node_key: str,
    *,
    primary_env: str,
    fallback_env: str,
    default_primary: str = "qwen3.6-plus",
    default_fallback: Optional[str] = "qwen3.7-max",
    required_capabilities: frozenset[str] = frozenset(),
) -> NodeSpec:
    return NodeSpec(
        node_key=node_key,
        channel="rd_llm",
        primary_env=primary_env,
        fallback_env=fallback_env,
        default_primary=default_primary,
        default_fallback=default_fallback,
        required_capabilities=required_capabilities,
    )


def _pi_agent_spec(node_key: str, *, primary_env: str, fallback_env: str) -> NodeSpec:
    return NodeSpec(
        node_key=node_key,
        channel="pi_agent",
        primary_env=primary_env,
        fallback_env=fallback_env,
        default_primary="gpt-5.6-sol",
        default_fallback=None,
    )


_REGISTRY = {
    "query_governance.kg_agent": _rd_spec(
        "query_governance.kg_agent",
        primary_env="QUERY_GOVERNANCE_KG_AGENT_MODEL",
        fallback_env="QUERY_GOVERNANCE_KG_AGENT_FALLBACK_MODEL",
    ),
    "query_governance.relevance_governance": _rd_spec(
        "query_governance.relevance_governance",
        primary_env="QUERY_GOVERNANCE_RELEVANCE_GOVERNANCE_MODEL",
        fallback_env="QUERY_GOVERNANCE_RELEVANCE_GOVERNANCE_FALLBACK_MODEL",
    ),
    "query_governance.preference_generation": _rd_spec(
        "query_governance.preference_generation",
        primary_env="QUERY_GOVERNANCE_PREFERENCE_GENERATION_MODEL",
        fallback_env="QUERY_GOVERNANCE_PREFERENCE_GENERATION_FALLBACK_MODEL",
    ),
    "query_governance.relevance_multimodal": _rd_spec(
        "query_governance.relevance_multimodal",
        primary_env="QUERY_GOVERNANCE_RELEVANCE_MULTIMODAL_MODEL",
        fallback_env="QUERY_GOVERNANCE_RELEVANCE_MULTIMODAL_FALLBACK_MODEL",
        default_primary="qwen3.7-plus",
        default_fallback="qwen3.6-plus",
        required_capabilities=frozenset({"image"}),
    ),
    "query_governance.relevance_rules": _rd_spec(
        "query_governance.relevance_rules",
        primary_env="QUERY_GOVERNANCE_RELEVANCE_RULES_MODEL",
        fallback_env="QUERY_GOVERNANCE_RELEVANCE_RULES_FALLBACK_MODEL",
    ),
    "top_query.planner": _rd_spec(
        "top_query.planner",
        primary_env="TOP_QUERY_PLANNER_MODEL",
        fallback_env="TOP_QUERY_PLANNER_FALLBACK_MODEL",
        default_fallback="glm-5.2",
    ),
    "top_query.world_knowledge": _rd_spec(
        "top_query.world_knowledge",
        primary_env="TOP_QUERY_WORLD_KNOWLEDGE_MODEL",
        fallback_env="TOP_QUERY_WORLD_KNOWLEDGE_FALLBACK_MODEL",
    ),
    "no_result.intent": _rd_spec(
        "no_result.intent",
        primary_env="NO_RESULT_INTENT_MODEL",
        fallback_env="NO_RESULT_INTENT_FALLBACK_MODEL",
        default_fallback=None,
    ),
    "no_result.diagnosis": _rd_spec(
        "no_result.diagnosis",
        primary_env="NO_RESULT_DIAGNOSIS_MODEL",
        fallback_env="NO_RESULT_DIAGNOSIS_FALLBACK_MODEL",
        default_fallback=None,
    ),
    "assistant.chat": _pi_agent_spec(
        "assistant.chat",
        primary_env="PI_AGENT_MODEL",
        fallback_env="PI_AGENT_FALLBACK_MODEL",
    ),
}

NODE_REGISTRY: Mapping[str, NodeSpec] = MappingProxyType(_REGISTRY)

QUERY_GOVERNANCE_NODE_KEYS = (
    "query_governance.kg_agent",
    "query_governance.preference_generation",
    "query_governance.relevance_multimodal",
    "query_governance.relevance_rules",
)

_DEFAULT_MODEL_CAPABILITIES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "deepseek-vl3": frozenset({"text", "image"}),
        "qwen-vl-max": frozenset({"text", "image"}),
        "qwen2.5-vl-72b-instruct": frozenset({"text", "image"}),
        "qwen3-vl-plus": frozenset({"text", "image"}),
        "qwen3.5-plus": frozenset({"text", "image"}),
        # RD-LLM exposes these models for the current relevance benchmark;
        # they accept the same multimodal payload as the explicitly suffixed
        # VL models.
        "qwen3.7-plus": frozenset({"text", "image"}),
        "qwen3.6-plus": frozenset({"text", "image"}),
        "deepseek-v4.1-flash": frozenset({"text", "image"}),
        "qwen3.8-flash": frozenset({"text", "image"}),
    }
)


def _configured_model_capabilities() -> Mapping[str, frozenset[str]]:
    raw = os.getenv("LLM_MODEL_CAPABILITIES_JSON", "").strip()
    if not raw:
        return MappingProxyType({})
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return MappingProxyType({})
    if not isinstance(payload, Mapping):
        return MappingProxyType({})
    result: dict[str, frozenset[str]] = {}
    for model, capabilities in payload.items():
        if not isinstance(model, str) or not isinstance(capabilities, (list, tuple, set)):
            continue
        normalized = frozenset(
            str(item).strip().lower() for item in capabilities if str(item).strip()
        )
        if normalized:
            result[model.strip().lower()] = normalized
    return MappingProxyType(result)


def model_capabilities(model: str) -> frozenset[str]:
    normalized = model.strip().lower()
    configured = _configured_model_capabilities().get(normalized)
    if configured is not None:
        return configured
    return _DEFAULT_MODEL_CAPABILITIES.get(normalized, frozenset({"text"}))


def _require_spec(node_key: str) -> NodeSpec:
    try:
        return NODE_REGISTRY[node_key]
    except KeyError as exc:
        raise NodeConfigError(f"unknown nodeKey: {node_key}") from exc


def _model_value(value: object, field: str, *, allow_none: bool = False) -> Optional[str]:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise NodeConfigError(f"{field} must be a string or null")
    normalized = value.strip()
    if not normalized or any(char.isspace() for char in normalized) or any(ord(char) < 32 for char in normalized):
        raise NodeConfigError(f"{field} must be non-empty and contain no whitespace/control characters")
    if len(normalized) > 120:
        raise NodeConfigError(f"{field} exceeds 120 characters")
    return normalized


def _resolve_field(
    published: Optional[Mapping[str, object]],
    field: str,
    env_name: str,
    default: Optional[str],
    *,
    allow_none: bool = False,
) -> tuple[Optional[str], str]:
    if published is not None and field in published:
        value = _model_value(published[field], field, allow_none=allow_none)
        return value, "published"
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        return _model_value(env_value, env_name), "env"
    if default is None:
        return None, "default"
    return _model_value(default, "default"), "default"


def resolve_node_config(
    node_key: str,
    *,
    published: Optional[Mapping[str, object]] = None,
) -> ResolvedNodeConfig:
    spec = _require_spec(node_key)
    if published is not None and not isinstance(published, Mapping):
        raise NodeConfigError("published node config must be a mapping")
    if published is not None:
        unknown = set(published) - {"primaryModel", "fallbackModel"}
        if unknown:
            raise NodeConfigError(f"unsupported published fields: {sorted(unknown)}")

    primary, primary_source = _resolve_field(
        published,
        "primaryModel",
        spec.primary_env,
        spec.default_primary,
    )
    fallback, fallback_source = _resolve_field(
        published,
        "fallbackModel",
        spec.fallback_env,
        spec.default_fallback,
        allow_none=True,
    )
    if primary is None:
        raise NodeConfigError("primaryModel is required")
    if fallback == primary:
        raise NodeConfigError("fallbackModel must differ from primaryModel")
    return ResolvedNodeConfig(
        node_key=node_key,
        channel=spec.channel,
        primary_model=primary,
        fallback_model=fallback,
        primary_source=primary_source,
        fallback_source=fallback_source,
    )


def model_audit_fields(config: ResolvedNodeConfig, model_role: str) -> dict[str, object]:
    if not isinstance(config, ResolvedNodeConfig):
        raise NodeConfigError("model audit config is invalid")
    if model_role not in {"primary", "fallback"}:
        raise NodeConfigError("modelRole must be primary or fallback")
    if model_role == "fallback" and config.fallback_model is None:
        raise NodeConfigError("fallback model is not configured")
    return {
        "nodeKey": config.node_key,
        "channel": config.channel,
        "modelName": (
            config.fallback_model if model_role == "fallback" else config.primary_model
        ),
        "modelRole": model_role,
        "configSource": (
            config.fallback_source if model_role == "fallback" else config.primary_source
        ),
    }


def capture_snapshot(
    *,
    node_keys: Optional[Sequence[str]] = None,
    published: Optional[Mapping[str, Mapping[str, object]]] = None,
    version: Optional[int] = None,
) -> NodeConfigSnapshot:
    if node_keys is None:
        selected = tuple(NODE_REGISTRY.keys())
    elif isinstance(node_keys, (str, bytes)) or not isinstance(node_keys, Sequence):
        raise NodeConfigError("node_keys must be a non-empty sequence of nodeKey strings")
    else:
        selected = tuple(node_keys)
    if not selected or any(not isinstance(node_key, str) or not node_key for node_key in selected):
        raise NodeConfigError("node_keys must be a non-empty sequence of nodeKey strings")
    if published is not None and not isinstance(published, Mapping):
        raise NodeConfigError("published node configs must be a mapping")
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int) or version <= 0
    ):
        raise NodeConfigError("version must be a positive integer or null")
    published = published or {}
    unknown_published = set(published) - set(NODE_REGISTRY)
    if unknown_published:
        raise NodeConfigError(f"unknown published nodeKey: {sorted(unknown_published)}")
    if any(node_key not in NODE_REGISTRY for node_key in selected):
        raise NodeConfigError(f"unknown nodeKey: {next(node_key for node_key in selected if node_key not in NODE_REGISTRY)}")
    if any(value is not None and not isinstance(value, Mapping) for value in published.values()):
        raise NodeConfigError("published node configs must be mappings")
    resolved = {
        node_key: resolve_node_config(node_key, published=published.get(node_key))
        for node_key in selected
    }
    return NodeConfigSnapshot(
        captured_at=datetime.now(timezone.utc).isoformat(),
        nodes=MappingProxyType(resolved),
        version=version,
    )


def get_snapshot_node_config(
    snapshot: NodeConfigSnapshot,
    node_key: str,
    *,
    required_channel: Optional[str] = None,
) -> ResolvedNodeConfig:
    """Return one validated node from a task-start configuration snapshot."""
    spec = _require_spec(node_key)
    if not isinstance(snapshot, NodeConfigSnapshot):
        raise NodeConfigError(f"{node_key} model config snapshot is invalid")
    try:
        config = snapshot.nodes[node_key]
    except (AttributeError, KeyError, TypeError) as exc:
        raise NodeConfigError(f"{node_key} model config snapshot is missing {node_key}") from exc
    if not isinstance(config, ResolvedNodeConfig) or config.node_key != node_key:
        raise NodeConfigError(f"{node_key} model config snapshot is invalid")
    if config.channel != spec.channel or (
        required_channel is not None and config.channel != required_channel
    ):
        raise NodeConfigError(f"{node_key} must use {required_channel or spec.channel} channel")
    _model_value(config.primary_model, "primaryModel")
    _model_value(config.fallback_model, "fallbackModel", allow_none=True)
    if config.fallback_model == config.primary_model:
        raise NodeConfigError("fallbackModel must differ from primaryModel")
    return config
