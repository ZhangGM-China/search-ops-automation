from __future__ import annotations

from src.assistant import PiAgentAssistantService
from src.llm.node_config import capture_snapshot


def _config(node_key: str, primary: str, fallback: str | None = None):
    snapshot = capture_snapshot(
        node_keys=(node_key,),
        published={
            node_key: {
                "primaryModel": primary,
                "fallbackModel": fallback,
            },
        },
    )
    return snapshot, snapshot.nodes[node_key]


def test_assistant_runtime_exposes_pi_agent_channel_and_selected_model():
    _snapshot, config = _config("assistant.chat", "qwen3.7-max", "glm-5.2")
    service = PiAgentAssistantService(
        model_name=config.primary_model,
        model_config=config,
    )

    runtime = service.runtime_info()

    assert runtime["provider"] == "PI Agent"
    assert runtime["modelName"] == "qwen3.7-max"
    assert runtime["nodeKey"] == "assistant.chat"
    assert runtime["channel"] == "pi_agent"
    assert runtime["modelRole"] == "primary"
    assert runtime["configSource"] == "published"
    assert runtime["agentId"] is None
    assert runtime["teamId"] is None
