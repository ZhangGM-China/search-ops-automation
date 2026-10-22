import json
import os
from unittest.mock import patch

import pytest

from src.llm.rd_client import (
    FallbackChatModel,
    build_rd_chat_model,
    build_rd_chat_with_fallback,
)


class RecordingModel:
    def __init__(self, *, should_fail: bool = False):
        self.should_fail = should_fail
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.should_fail:
            raise RuntimeError("boom")
        return {"ok": True}


def test_fallback_chat_model_passes_kwargs_to_primary_model():
    primary = RecordingModel()
    wrapper = FallbackChatModel(primary=primary)

    wrapper.invoke_primary([{"role": "user", "content": "hi"}], timeout=8.0, temperature=0.2)

    assert primary.calls == [
        {
            "messages": [{"role": "user", "content": "hi"}],
            "kwargs": {"timeout": 8.0, "temperature": 0.2},
        }
    ]


def test_fallback_chat_model_passes_kwargs_to_fallback_model():
    primary = RecordingModel(should_fail=True)
    fallback = RecordingModel()
    wrapper = FallbackChatModel(primary=primary, fallback=fallback)

    wrapper.invoke([{"role": "user", "content": "hi"}], timeout=5.0)

    assert primary.calls == [
        {
            "messages": [{"role": "user", "content": "hi"}],
            "kwargs": {"timeout": 5.0},
        }
    ]
    assert fallback.calls == [
        {
            "messages": [{"role": "user", "content": "hi"}],
            "kwargs": {"timeout": 5.0},
        }
    ]


def test_fallback_chat_model_sanitizes_malformed_tool_call_argument_keys():
    primary = RecordingModel()
    wrapper = FallbackChatModel(primary=primary)
    malformed_key = "<<-" + "bad-token" * 80

    wrapper.invoke([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "search_ops_context",
                        "arguments": {malformed_key: "atlas"},
                    },
                }
            ],
        }
    ])

    sanitized_arguments = primary.calls[0]["messages"][0]["tool_calls"][0]["function"]["arguments"]
    parsed_arguments = json.loads(sanitized_arguments)
    assert parsed_arguments == {
        "_malformed_arguments": True,
        "original_argument_type": "dict",
        "dropped_property_count": 1,
    }


def test_fallback_chat_model_sanitizes_primary_and_fallback_argument_shapes():
    primary = RecordingModel(should_fail=True)
    fallback = RecordingModel()
    wrapper = FallbackChatModel(primary=primary, fallback=fallback)
    messages = [
        "unchanged-message",
        {
            "role": "assistant",
            "tool_calls": [
                "unchanged-tool-call",
                {
                    "function": {
                        "name": "search_ops_context",
                        "arguments": '{"query":"atlas"}',
                    }
                },
                {
                    "name": "search_ops_context",
                    "arguments": {"query": "atlas"},
                },
            ],
        },
    ]

    wrapper.invoke(messages)

    for model in (primary, fallback):
        forwarded = model.calls[0]["messages"]
        assert forwarded[0] == "unchanged-message"
        assert forwarded[1]["tool_calls"][0] == "unchanged-tool-call"
        assert forwarded[1]["tool_calls"][1]["function"]["arguments"] == '{"query":"atlas"}'
        assert json.loads(forwarded[1]["tool_calls"][2]["arguments"]) == {"query": "atlas"}


def test_fallback_chat_model_preserves_non_list_messages():
    primary = RecordingModel()
    wrapper = FallbackChatModel(primary=primary)
    messages = ("system", "user")

    wrapper.invoke_primary(messages)

    assert primary.calls[0]["messages"] is messages


def test_fallback_chat_model_raises_when_fallback_is_unavailable():
    wrapper = FallbackChatModel(primary=RecordingModel(should_fail=True))

    with pytest.raises(RuntimeError, match="fallback model is not configured"):
        wrapper.invoke_fallback([])
    with pytest.raises(RuntimeError, match="boom"):
        wrapper.invoke([])


def test_fallback_chat_model_handles_circular_tool_arguments():
    primary = RecordingModel()
    wrapper = FallbackChatModel(primary=primary)
    circular = {}
    circular["self"] = circular

    wrapper.invoke([
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "search_ops_context",
                        "arguments": circular,
                    }
                }
            ],
        }
    ])

    arguments = primary.calls[0]["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {"value": "{'self': {...}}"}


@patch.dict(os.environ, {"RD_LLM_API_KEY": "test-key"}, clear=True)
@patch("src.llm.rd_client.ChatOpenAI")
def test_rd_chat_with_fallback_uses_new_endpoint_and_qwen_default(mock_chat_openai):
    wrapper = build_rd_chat_with_fallback(
        timeout=30,
        primary_env="TEST_PRIMARY_MODEL",
        fallback_env="TEST_FALLBACK_MODEL",
    )

    assert wrapper is not None
    assert mock_chat_openai.call_count == 2
    primary_call = mock_chat_openai.call_args_list[0].kwargs
    assert primary_call["base_url"] == "https://api-hub.example.test/v1"
    assert primary_call["model"] == "qwen3.6-plus"
    assert primary_call["openai_api_key"] == "test-key"


@patch.dict(os.environ, {"RD_LLM_API_KEY": "test-key"}, clear=True)
@patch("src.llm.rd_client.ChatOpenAI")
def test_rd_chat_model_accepts_extra_body_configuration(mock_chat_openai):
    model = build_rd_chat_model(
        timeout=30,
        default_model="qwen3.7-plus",
        extra_body={"enable_thinking": False},
    )

    assert model is mock_chat_openai.return_value
    assert mock_chat_openai.call_args.kwargs["extra_body"] == {"enable_thinking": False}


@patch.dict(
    os.environ,
    {
        "RD_LLM_API_KEY": "override-key",
        "RD_LLM_BASE_URL": "https://override.example/v1",
        "TEST_PRIMARY_MODEL": "override-primary",
        "TEST_FALLBACK_MODEL": "override-fallback",
    },
    clear=True,
)
@patch("src.llm.rd_client.ChatOpenAI")
def test_rd_chat_with_fallback_keeps_environment_overrides(mock_chat_openai):
    build_rd_chat_with_fallback(
        timeout=30,
        primary_env="TEST_PRIMARY_MODEL",
        fallback_env="TEST_FALLBACK_MODEL",
    )

    primary_call = mock_chat_openai.call_args_list[0].kwargs
    fallback_call = mock_chat_openai.call_args_list[1].kwargs
    assert primary_call["base_url"] == "https://override.example/v1"
    assert primary_call["model"] == "override-primary"
    assert primary_call["openai_api_key"] == "override-key"
    assert fallback_call["model"] == "override-fallback"


@patch.dict(os.environ, {"RD_LLM_API_KEY": "test-key"}, clear=True)
@patch("src.llm.rd_client.ChatOpenAI")
def test_rd_chat_with_fallback_applies_completion_limit_to_both_models(mock_chat_openai):
    build_rd_chat_with_fallback(
        timeout=30,
        primary_env="TEST_PRIMARY_MODEL",
        fallback_env="TEST_FALLBACK_MODEL",
        max_completion_tokens=1200,
    )

    assert mock_chat_openai.call_args_list[0].kwargs["max_completion_tokens"] == 1200
    assert mock_chat_openai.call_args_list[1].kwargs["max_completion_tokens"] == 1200
