"""Optional Langfuse tracing for the deterministic Query governance flow.

The governance workflow owns business state and actions.  This module only
builds LangChain ``RunnableConfig`` values for model calls; a missing or
unavailable Langfuse service therefore never changes a governance result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import re
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import convert_to_openai_messages

from .models import QuerySpec


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "authorization",
    "access_token",
    "refresh_token",
    "cookie",
    "credential",
    "private_key",
    "base64",
    "binary",
    "raw_response",
    "response_body",
    "external_response",
    "raw_payload",
    "signature",
)
_LOGGER = logging.getLogger(__name__)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+\-/=]+")


class LangfuseGenerationCallback(BaseCallbackHandler):
    """Trace LangChain Core model calls without the optional langchain package."""

    def __init__(self, *, client: Any, trace_context: Mapping[str, str]) -> None:
        self.client = client
        self.trace_context = dict(trace_context)
        self._observations: dict[Any, Any] = {}
        self._lock = Lock()

    def _start(self, inputs: Any, *, run_id: Any, metadata: Any, kwargs: Any) -> None:
        try:
            details = dict(metadata or {})
            model = details.get("ls_model_name") or kwargs.get("invocation_params", {}).get("model_name")
            name = str(kwargs.get("name") or details.get("node") or "model")
            observation = self.client.start_observation(
                trace_context=self.trace_context,
                name=name,
                as_type="generation",
                input=sanitize_trace_data(inputs),
                metadata=sanitize_trace_data(details),
                model=str(model) if model else None,
            )
            with self._lock:
                self._observations[run_id] = observation
        except Exception as exc:
            _LOGGER.warning("Langfuse generation start failed: %s", exc.__class__.__name__)

    def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id: Any, metadata: Any = None, **kwargs: Any) -> None:
        try:
            if isinstance(messages, (list, tuple)) and messages and all(
                isinstance(group, (list, tuple)) for group in messages
            ):
                conversations = [convert_to_openai_messages(group) for group in messages]
                inputs = conversations[0] if len(conversations) == 1 else conversations
            else:
                inputs = convert_to_openai_messages(messages)
        except Exception as exc:
            _LOGGER.warning("Langfuse chat input conversion failed: %s", exc.__class__.__name__)
            inputs = messages
        self._start(inputs, run_id=run_id, metadata=metadata, kwargs=kwargs)

    def on_llm_start(self, serialized: Any, prompts: Any, *, run_id: Any, metadata: Any = None, **kwargs: Any) -> None:
        self._start(prompts, run_id=run_id, metadata=metadata, kwargs=kwargs)

    def _pop(self, run_id: Any) -> Any | None:
        with self._lock:
            return self._observations.pop(run_id, None)

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        observation = self._pop(run_id)
        if observation is None:
            return
        try:
            outputs = [
                getattr(item, "text", None) or getattr(getattr(item, "message", None), "content", None)
                for group in (getattr(response, "generations", None) or [])
                for item in group
            ]
            llm_output = getattr(response, "llm_output", None) or {}
            token_usage = llm_output.get("token_usage") or {}
            usage = {
                key: int(value)
                for key, value in (
                    ("input", token_usage.get("prompt_tokens")),
                    ("output", token_usage.get("completion_tokens")),
                    ("total", token_usage.get("total_tokens")),
                )
                if isinstance(value, int)
            }
            update: dict[str, Any] = {"output": sanitize_trace_data(outputs)}
            if usage:
                update["usage_details"] = usage
            if llm_output.get("model_name"):
                update["model"] = str(llm_output["model_name"])
            observation.update(**update)
            observation.end()
        except Exception as exc:
            _LOGGER.warning("Langfuse generation finish failed: %s", exc.__class__.__name__)

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        observation = self._pop(run_id)
        if observation is None:
            return
        try:
            observation.update(level="ERROR", status_message=str(error)[:240])
            observation.end()
        except Exception as exc:
            _LOGGER.warning("Langfuse generation error finish failed: %s", exc.__class__.__name__)


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    if normalized in {"token", "session_token"}:
        return True
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def sanitize_trace_data(value: Any = None, *, data: Any = None) -> Any:
    """Return a JSON-like value with credentials and binary payloads removed."""

    # Langfuse 4.x invokes a custom mask function as ``mask(data=...)`` while
    # callers inside the governance code use the positional form.
    if data is not None:
        value = data

    if isinstance(value, Mapping):
        return {
            str(key): sanitize_trace_data(item)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_trace_data(item) for item in value]
    if isinstance(value, bytes):
        return "<redacted:bytes>"
    if isinstance(value, bytearray):
        return "<redacted:bytearray>"
    if isinstance(value, str):
        if value.strip().lower().startswith("data:") and ";base64," in value[:160].lower():
            return "<redacted:base64>"
        value = _BEARER_PATTERN.sub("Bearer <redacted>", value)
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.query:
            query = [
                (key, "<redacted>" if _is_sensitive_key(key) else item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ]
            return urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
            )
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _configured(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return False
    return not any(
        marker in normalized
        for marker in ("replace", "placeholder", "your-key", "example.com")
    )


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _as_float(value: str | None, default: float) -> float:
    try:
        result = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, result))


@dataclass(frozen=True)
class QueryTrace:
    """Trace identity and base metadata for one Query."""

    batch_id: str
    batch_trace_id: str
    query_id: str
    brand: str
    query: str
    trace_id: str
    metadata: Mapping[str, Any]
    handler: Any | None = None

    def config_for(
        self,
        node: str,
        *,
        model_config_version: int | None = None,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = dict(self.metadata)
        metadata["node"] = node
        metadata["query_trace_id"] = self.trace_id
        if model_config_version is not None:
            metadata["model_config_version"] = model_config_version
        if extra_metadata:
            metadata.update(extra_metadata)
        metadata = sanitize_trace_data(metadata)
        return {
            "callbacks": [self.handler] if self.handler is not None else [],
            "metadata": metadata,
            "run_name": f"query-governance.{node}",
        }


@dataclass
class BatchTrace:
    """Batch trace plus Query traces created at workflow entry."""

    batch_id: str
    batch_trace_id: str
    query_traces: dict[str, QueryTrace]
    batch_handler: Any | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def callbacks(self, node: str) -> list[Any]:
        """Return the batch callback used by model calls spanning Queries."""
        return [self.batch_handler] if self.batch_handler is not None else []

    def query_config(
        self,
        query_id: str,
        node: str,
        *,
        model_config_version: int | None = None,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        trace = self.query_traces[query_id]
        return trace.config_for(
            node,
            model_config_version=model_config_version,
            extra_metadata=extra_metadata,
        )

    def query_trace_context(
        self,
        query_id: str,
        node: str,
        *,
        model_config_version: int | None = None,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return JSON-safe identity needed to resume an async Query trace.

        Callback instances are intentionally excluded.  The worker recreates
        them from the persisted trace id and its current environment-backed
        Langfuse client.
        """
        trace = self.query_traces[query_id]
        metadata = dict(trace.metadata)
        metadata["node"] = node
        metadata["query_trace_id"] = trace.trace_id
        if model_config_version is not None:
            metadata["model_config_version"] = model_config_version
        if extra_metadata:
            metadata.update(extra_metadata)
        return sanitize_trace_data(
            {
                "batch_id": self.batch_id,
                "batch_trace_id": self.batch_trace_id,
                "query_id": trace.query_id,
                "query_trace_id": trace.trace_id,
                "brand": trace.brand,
                "query": trace.query,
                "node": node,
                "metadata": metadata,
            }
        )

    def batch_config(
        self,
        node: str,
        query_ids: Sequence[str],
        *,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            **self.metadata,
            "batch_id": self.batch_id,
            "batch_trace_id": self.batch_trace_id,
            "query_ids": list(query_ids),
            "query_trace_ids": {
                query_id: self.query_traces[query_id].trace_id
                for query_id in query_ids
                if query_id in self.query_traces
            },
            "query_count": len(query_ids),
            "node": node,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        return {
            "callbacks": [self.batch_handler] if self.batch_handler is not None else [],
            "metadata": sanitize_trace_data(metadata),
            "run_name": f"query-governance.{node}",
        }


@dataclass
class QueryGovernanceObservability:
    """Failure-isolated Langfuse context factory for Query governance."""

    client: Any | None = None
    handler_factory: Callable[..., Any] | None = None
    # Langfuse skips callbacks without a project key once multiple clients
    # exist in one process. Bind every handler to this client's project.
    langfuse_public_key: str | None = None
    base_metadata: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    error_events: list[str] = field(default_factory=list)

    @classmethod
    def disabled(cls, reason: str | None = None) -> "QueryGovernanceObservability":
        instance = cls(client=None, handler_factory=None, enabled=False)
        if reason:
            instance._record_error(reason)
        return instance

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        client_factory: Callable[..., Any] | None = None,
        handler_factory: Callable[..., Any] | None = None,
    ) -> "QueryGovernanceObservability":
        values = dict(os.environ if env is None else env)
        if not _as_bool(values.get("LANGFUSE_TRACING_ENABLED"), True):
            return cls.disabled("tracing_disabled")
        if not _configured(values.get("LANGFUSE_PUBLIC_KEY")) or not _configured(
            values.get("LANGFUSE_SECRET_KEY")
        ):
            return cls.disabled("langfuse_not_configured")
        try:
            if client_factory is None:
                from langfuse import Langfuse

                client_factory = Langfuse
            if client_factory is not None:
                client = client_factory(
                    public_key=values.get("LANGFUSE_PUBLIC_KEY"),
                    secret_key=values.get("LANGFUSE_SECRET_KEY"),
                    base_url=values.get("LANGFUSE_BASE_URL")
                    or values.get("LANGFUSE_HOST"),
                    environment=values.get("LANGFUSE_TRACING_ENVIRONMENT")
                    or values.get("ENV", "test"),
                    release=values.get("RELEASE_SHA") or values.get("LANGFUSE_RELEASE"),
                    flush_at=int(values.get("LANGFUSE_FLUSH_AT", "20")),
                    flush_interval=float(values.get("LANGFUSE_FLUSH_INTERVAL", "5")),
                    sample_rate=_as_float(values.get("LANGFUSE_SAMPLE_RATE"), 1.0),
                    mask=sanitize_trace_data,
                )
            if handler_factory is None:
                def handler_factory(**kwargs: Any) -> Any:
                    trace_context = kwargs.get("trace_context") or {}
                    try:
                        # The optional LangChain integration enriches traces when
                        # the full ``langchain`` package is installed locally.
                        # Production only ships langchain-core, so keep a small
                        # core-compatible callback as the deterministic fallback.
                        from langfuse.langchain import CallbackHandler
                    except ModuleNotFoundError:
                        return LangfuseGenerationCallback(
                            client=client,
                            trace_context=trace_context,
                        )
                    kwargs.setdefault("public_key", values.get("LANGFUSE_PUBLIC_KEY"))
                    return CallbackHandler(**kwargs)
            return cls(
                client=client,
                handler_factory=handler_factory,
                langfuse_public_key=values.get("LANGFUSE_PUBLIC_KEY"),
                enabled=True,
                base_metadata={
                    "environment": values.get("LANGFUSE_TRACING_ENVIRONMENT")
                    or values.get("ENV", "test"),
                    "release": values.get("RELEASE_SHA")
                    or values.get("LANGFUSE_RELEASE"),
                    "langfuse_session_id": None,
                    "langfuse_trace_name": "query-governance",
                    "langfuse_tags": ["query-governance"],
                },
            )
        except Exception as exc:  # telemetry must never block governance
            return cls.disabled(f"langfuse_init_failed:{exc.__class__.__name__}")

    def _record_error(self, message: str) -> None:
        event = str(message)[:240]
        self.error_events.append(event)
        _LOGGER.warning("Query governance Langfuse disabled or degraded: %s", event)

    def _trace_id(self, seed: str) -> str:
        if self.client is None:
            return ""
        try:
            return str(self.client.create_trace_id(seed=seed))
        except Exception as exc:
            self._record_error(f"trace_id_failed:{exc.__class__.__name__}")
            return ""

    def _handler(self, trace_id: str) -> Any | None:
        if self.handler_factory is None or not trace_id:
            return None
        try:
            kwargs: dict[str, Any] = {"trace_context": {"trace_id": trace_id}}
            if self.langfuse_public_key:
                kwargs["public_key"] = self.langfuse_public_key
            return self.handler_factory(**kwargs)
        except Exception as exc:
            self._record_error(f"handler_failed:{exc.__class__.__name__}")
            return None

    @staticmethod
    def _trace_id_from_config(config: Mapping[str, Any] | None) -> str:
        payload = config if isinstance(config, Mapping) else {}
        metadata = payload.get("metadata")
        if isinstance(metadata, Mapping):
            trace_id = metadata.get("query_trace_id")
            if trace_id:
                return str(trace_id).strip()
        for key in ("query_trace_id", "trace_id"):
            trace_id = payload.get(key)
            if trace_id:
                return str(trace_id).strip()
        return ""

    def start_tool_observation(
        self,
        *,
        langfuse_config: Mapping[str, Any] | None,
        name: str,
        input: Any,
        metadata: Mapping[str, Any] | None = None,
        parent: Any | None = None,
    ) -> Any | None:
        """Start a failure-isolated TOOL observation for a graph read call."""
        if not self.enabled or self.client is None:
            return None
        trace_id = self._trace_id_from_config(langfuse_config)
        if not trace_id:
            return None
        config_metadata = (
            langfuse_config.get("metadata")
            if isinstance(langfuse_config, Mapping)
            else None
        )
        merged_metadata = (
            {**dict(config_metadata), **dict(metadata or {})}
            if isinstance(config_metadata, Mapping)
            else dict(metadata or {})
        )
        try:
            owner = parent if parent is not None else self.client
            kwargs = {} if parent is not None else {"trace_context": {"trace_id": trace_id}}
            return owner.start_observation(
                **kwargs,
                name=str(name),
                as_type="tool",
                input=sanitize_trace_data(input),
                metadata=sanitize_trace_data(merged_metadata),
            )
        except Exception as exc:
            self._record_error(f"tool_observation_start_failed:{exc.__class__.__name__}")
            return None

    def start_agent_observation(
        self, *, langfuse_config: Mapping[str, Any] | None, name: str,
        input: Any, metadata: Mapping[str, Any] | None = None,
    ) -> Any | None:
        if not self.enabled or self.client is None:
            return None
        trace_id = self._trace_id_from_config(langfuse_config)
        if not trace_id:
            return None
        try:
            return self.client.start_observation(
                trace_context={"trace_id": trace_id}, name=name, as_type="agent",
                input=sanitize_trace_data(input), metadata=sanitize_trace_data(metadata or {}),
            )
        except Exception as exc:
            self._record_error(f"agent_observation_start_failed:{exc.__class__.__name__}")
            return None

    def finish_agent_observation(
        self, observation: Any | None, *, output: Any | None = None,
        error_message: str | None = None,
    ) -> None:
        if observation is None:
            return
        try:
            if error_message:
                observation.update(
                    output=sanitize_trace_data({"error": error_message}),
                    level="ERROR", status_message=error_message,
                )
            else:
                observation.update(output=sanitize_trace_data(output))
            observation.end()
        except Exception as exc:
            self._record_error(f"agent_observation_finish_failed:{exc.__class__.__name__}")

    def finish_tool_observation(
        self,
        observation: Any | None,
        *,
        output: Any | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Complete a TOOL observation without affecting the business call."""
        if observation is None:
            return
        try:
            if error_code or error_message:
                observation.update(
                    output=sanitize_trace_data(
                        {
                            "error_code": error_code or "KG_GRAPH_READ_FAILED",
                            "error_message": error_message or "graph read failed",
                        }
                    ),
                    level="ERROR",
                    status_message=str(error_message or error_code or "graph read failed"),
                )
            else:
                observation.update(output=sanitize_trace_data(output))
            end = getattr(observation, "end", None)
            if callable(end):
                end()
        except Exception as exc:
            self._record_error(f"tool_observation_finish_failed:{exc.__class__.__name__}")

    def resume_query_config(
        self,
        context: Mapping[str, Any],
        *,
        node: str | None = None,
    ) -> dict[str, Any]:
        """Rebuild a model-call config for a persisted Query Trace."""
        payload = dict(context or {})
        trace_id = str(payload.get("query_trace_id") or "").strip()
        node_name = str(node or payload.get("node") or "kg_agent").strip() or "kg_agent"
        metadata = payload.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        for key in (
            "batch_id",
            "batch_trace_id",
            "query_id",
            "query_trace_id",
            "brand",
            "query",
        ):
            if key in payload and key not in metadata:
                metadata[key] = payload[key]
        metadata["node"] = node_name
        return {
            "callbacks": [handler] if (handler := self._handler(trace_id)) is not None else [],
            "metadata": sanitize_trace_data(metadata),
            "run_name": f"query-governance.{node_name}",
        }

    def start_batch(
        self,
        batch_id: str,
        specs: Iterable[QuerySpec],
        *,
        metadata: Mapping[str, Any] | None = None,
        trace_name: str = "query-governance",
    ) -> BatchTrace:
        items = list(specs)
        common = dict(self.base_metadata)
        common["batch_id"] = batch_id
        if metadata:
            common.update(metadata)
        common["langfuse_session_id"] = batch_id
        common.setdefault("langfuse_trace_name", trace_name)
        common.setdefault("langfuse_tags", [trace_name])
        if not self.enabled or self.client is None:
            def disabled_query_metadata(spec: QuerySpec) -> dict[str, Any]:
                metadata = {
                    **common,
                    "batch_trace_id": "",
                    "query_id": spec.query_id,
                    "brand": spec.brand,
                    "query": spec.query,
                }
                if spec.experiment_id is not None:
                    metadata["experiment_id"] = spec.experiment_id
                if spec.group_type:
                    metadata["group_type"] = spec.group_type
                return metadata

            traces = {
                spec.query_id: QueryTrace(
                    batch_id=batch_id,
                    batch_trace_id="",
                    query_id=spec.query_id,
                    brand=spec.brand,
                    query=spec.query,
                    trace_id="",
                    metadata=sanitize_trace_data(disabled_query_metadata(spec)),
                )
                for spec in items
            }
            return BatchTrace(
                batch_id,
                "",
                traces,
                metadata=sanitize_trace_data(common),
            )

        batch_trace_id = self._trace_id(f"{trace_name}:{batch_id}")
        batch_handler = self._handler(batch_trace_id)
        traces: dict[str, QueryTrace] = {}
        for spec in items:
            trace_id = self._trace_id(f"{trace_name}:{batch_id}:{spec.query_id}")
            query_metadata = dict(common)
            query_metadata.update(
                {
                    "batch_trace_id": batch_trace_id,
                    "query_id": spec.query_id,
                    "brand": spec.brand,
                    "query": spec.query,
                }
            )
            if spec.experiment_id is not None:
                query_metadata["experiment_id"] = spec.experiment_id
            if spec.group_type:
                query_metadata["group_type"] = spec.group_type
            traces[spec.query_id] = QueryTrace(
                batch_id=batch_id,
                batch_trace_id=batch_trace_id,
                query_id=spec.query_id,
                brand=spec.brand,
                query=spec.query,
                trace_id=trace_id,
                metadata=sanitize_trace_data(query_metadata),
                handler=self._handler(trace_id),
            )
        return BatchTrace(
            batch_id,
            batch_trace_id,
            traces,
            batch_handler,
            metadata=sanitize_trace_data(common),
        )


__all__ = [
    "BatchTrace",
    "QueryGovernanceObservability",
    "QueryTrace",
    "sanitize_trace_data",
]
