"""Bounded model/tool loop for the two independent governance agents."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .langfuse import sanitize_trace_data


class AgentToolError(ValueError):
    def __init__(self, message: str, *, payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = dict(payload or {})


def parse_model_object(result: Any) -> Mapping[str, Any]:
    content = getattr(getattr(result, "response", result), "content", result)
    if not isinstance(content, str):
        raise AgentToolError("agent response must be text")
    body = content.strip()
    if body.startswith("```json"):
        body = body[7:].removesuffix("```").strip()
    elif body.startswith("```"):
        body = body[3:].removesuffix("```").strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AgentToolError("agent response must be JSON") from exc
    if not isinstance(payload, Mapping):
        raise AgentToolError("agent response must be a JSON object")
    return payload


def run_read_only_agent(
    *,
    runtime: Any,
    system_prompt: str,
    user_input: Mapping[str, Any],
    tools: Mapping[str, Callable[..., Any]],
    langfuse_config: Mapping[str, Any] | None = None,
    observability: Any | None = None,
    agent_name: str,
    max_tool_calls: int = 12,
    tool_observation_names: set[str] | None = None,
    response_format: Mapping[str, Any] | None = None,
    initial_tool_results: list[dict[str, Any]] | None = None,
) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(dict(user_input), ensure_ascii=False)},
    ]
    calls: list[dict[str, Any]] = list(initial_tool_results or [])
    initial_count = len(calls)
    agent_observation = (
        observability.start_agent_observation(
            langfuse_config=langfuse_config, name=agent_name, input=user_input,
            metadata={"prompt_version": "dual-agent-v1"},
        )
        if observability is not None else None
    )
    try:
        for _ in range(max_tool_calls + 1):
            invoke_kwargs = dict(langfuse_config or {})
            generation = None
            if agent_observation is not None:
                # Manual child observations keep each reasoning step and tool
                # call together under the correct Agent observation.
                try:
                    generation = agent_observation.start_observation(
                        name=f"{agent_name}.reason", as_type="generation",
                        input=sanitize_trace_data(messages),
                        metadata={"prompt_version": "dual-agent-v1"},
                    )
                    invoke_kwargs.pop("callbacks", None)
                except Exception:
                    generation = None
            try:
                if response_format is not None:
                    invoke_kwargs["response_format"] = dict(response_format)
                result = runtime.invoke(messages, **invoke_kwargs) if invoke_kwargs else runtime.invoke(messages)
            except Exception as exc:
                if generation is not None:
                    generation.update(output={"error": str(exc)}, level="ERROR", status_message=str(exc))
                    generation.end()
                raise
            payload = parse_model_object(result)
            if generation is not None:
                audit = getattr(result, "audit", None)
                audit = dict(audit) if isinstance(audit, Mapping) else {}
                update = {"output": sanitize_trace_data(payload), "metadata": sanitize_trace_data(audit)}
                if audit.get("modelName"):
                    update["model"] = str(audit["modelName"])
                if isinstance(audit.get("tokenUsage"), int):
                    update["usage_details"] = {"total_tokens": audit["tokenUsage"]}
                generation.update(**update)
                generation.end()
            request = payload.get("toolCall")
            if request is None:
                if calls:
                    if observability is not None:
                        observability.finish_agent_observation(agent_observation, output=payload)
                    return payload, calls
                raise AgentToolError("agent must inspect read-only tools before finalizing", payload=payload)
            if len(calls) - initial_count >= max_tool_calls:
                raise AgentToolError("agent tool-call limit exceeded")
            if not isinstance(request, Mapping):
                raise AgentToolError("toolCall must be an object")
            name = str(request.get("name") or "").strip()
            args = request.get("args") or {}
            if name not in tools or not isinstance(args, Mapping):
                raise AgentToolError(f"unsupported read-only tool: {name}")
            observation = (
                observability.start_tool_observation(
                    langfuse_config=langfuse_config, parent=agent_observation,
                    name=name, input=args, metadata={"agent": agent_name},
                ) if observability is not None and (
                    tool_observation_names is None or name in tool_observation_names
                ) else None
            )
            try:
                tool_args = dict(args)
                if agent_observation is not None:
                    tool_args["_agent_observation"] = agent_observation
                output = tools[name](**tool_args)
            except Exception as exc:
                if observability is not None:
                    observability.finish_tool_observation(
                        observation, error_code="AGENT_TOOL_FAILED", error_message=str(exc)
                    )
                output = {"error": str(exc).strip() or exc.__class__.__name__}
            else:
                if observability is not None:
                    observability.finish_tool_observation(observation, output=output)
            calls.append({"name": name, "args": dict(args), "result": output})
            messages.append({"role": "assistant", "content": json.dumps(dict(payload), ensure_ascii=False)})
            messages.append({"role": "user", "content": json.dumps({"toolResult": {"name": name, "result": output}}, ensure_ascii=False, default=str)})
        raise AgentToolError("agent did not finish")
    except Exception as exc:
        if observability is not None:
            observability.finish_agent_observation(agent_observation, error_message=str(exc))
        raise
