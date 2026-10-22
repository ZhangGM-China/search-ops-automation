import os
import json
from typing import Any, Mapping, Optional

from langchain_openai import ChatOpenAI


DEFAULT_RD_LLM_BASE_URL = "https://api-hub.example.test/v1"
DEFAULT_RD_LLM_MODEL = "qwen3.6-plus"


class FallbackChatModel:
    def __init__(self, primary: Any, fallback: Optional[Any] = None, session_key: Optional[str] = None):
        self.primary = primary
        self.fallback = fallback
        self.primary_model_name = getattr(primary, "model_name", None) or getattr(primary, "model", None)
        self.fallback_model_name = (
            getattr(fallback, "model_name", None) or getattr(fallback, "model", None)
            if fallback is not None else None
        )
        self.session_key = session_key

    def invoke_primary(self, messages, **kwargs):
        return self.primary.invoke(_sanitize_chat_messages(messages), **kwargs)

    def invoke_fallback(self, messages, **kwargs):
        if self.fallback is None:
            raise RuntimeError("fallback model is not configured")
        return self.fallback.invoke(_sanitize_chat_messages(messages), **kwargs)

    def invoke(self, messages, **kwargs):
        try:
            return self.invoke_primary(messages, **kwargs)
        except Exception:
            if self.fallback is None:
                raise
            return self.invoke_fallback(messages, **kwargs)


def _sanitize_chat_messages(messages: Any) -> Any:
    if not isinstance(messages, list):
        return messages
    return [_sanitize_chat_message(message) for message in messages]


def _sanitize_chat_message(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    sanitized = dict(message)
    tool_calls = sanitized.get("tool_calls")
    if isinstance(tool_calls, list):
        sanitized["tool_calls"] = [
            _sanitize_tool_call(tool_call)
            for tool_call in tool_calls
        ]
    return sanitized


def _sanitize_tool_call(tool_call: Any) -> Any:
    if not isinstance(tool_call, dict):
        return tool_call
    sanitized = dict(tool_call)
    function_payload = sanitized.get("function")
    if isinstance(function_payload, dict):
        sanitized_function = dict(function_payload)
        if "arguments" in sanitized_function:
            sanitized_function["arguments"] = _sanitize_tool_arguments(
                sanitized_function.get("arguments")
            )
        sanitized["function"] = sanitized_function
    elif "arguments" in sanitized:
        sanitized["arguments"] = _sanitize_tool_arguments(sanitized.get("arguments"))
    return sanitized


def _sanitize_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, dict):
        malformed_keys = [
            key
            for key in arguments.keys()
            if len(str(key).encode("utf-8")) > 256
        ]
        if malformed_keys:
            return json.dumps(
                {
                    "_malformed_arguments": True,
                    "original_argument_type": "dict",
                    "dropped_property_count": len(malformed_keys),
                },
                ensure_ascii=False,
            )
    try:
        return json.dumps(arguments if arguments is not None else {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"value": str(arguments)}, ensure_ascii=False)


def build_rd_chat_model(
    *,
    timeout: Optional[float] = None,
    model_env: Optional[str] = None,
    default_model: str = DEFAULT_RD_LLM_MODEL,
    temperature: float = 0.1,
    max_retries: int = 0,
    max_completion_tokens: Optional[int] = None,
    extra_body: Optional[Mapping[str, Any]] = None,
) -> Optional[Any]:
    api_key = os.getenv("RD_LLM_API_KEY")
    if not api_key:
        return None

    model = os.getenv(model_env, default_model) if model_env else default_model
    kwargs = {
        "model": model,
        "openai_api_key": api_key,
        "base_url": os.getenv("RD_LLM_BASE_URL", DEFAULT_RD_LLM_BASE_URL),
        "temperature": temperature,
        "max_retries": max_retries,
        "max_completion_tokens": max_completion_tokens,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
        kwargs["request_timeout"] = timeout
    if extra_body:
        kwargs["extra_body"] = dict(extra_body)
    return ChatOpenAI(**kwargs)


def build_rd_chat_with_fallback(
    *,
    timeout: float,
    primary_env: str,
    fallback_env: str,
    default_primary_model: str = DEFAULT_RD_LLM_MODEL,
    default_fallback_model: str = "qwen3.7-max",
    temperature: float = 0.1,
    max_retries: int = 0,
    max_completion_tokens: Optional[int] = None,
    primary_model_name: Optional[str] = None,
    session_key: Optional[str] = None,
) -> Optional[FallbackChatModel]:
    primary_model = os.getenv(primary_env, default_primary_model)
    fallback_model = os.getenv(fallback_env, default_fallback_model)

    primary = build_rd_chat_model(
        timeout=timeout,
        model_env=primary_env,
        default_model=default_primary_model,
        temperature=temperature,
        max_retries=max_retries,
        max_completion_tokens=max_completion_tokens,
    )
    if primary is None:
        return None
    fallback = None
    if fallback_model and fallback_model != primary_model:
        fallback = build_rd_chat_model(
            timeout=timeout,
            model_env=fallback_env,
            default_model=default_fallback_model,
            temperature=temperature,
            max_retries=max_retries,
            max_completion_tokens=max_completion_tokens,
        )
    return FallbackChatModel(primary=primary, fallback=fallback, session_key=session_key)
