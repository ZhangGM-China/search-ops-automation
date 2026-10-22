from dataclasses import FrozenInstanceError

import pytest

from src.llm.node_config import (
    NODE_REGISTRY,
    NodeConfigError,
    capture_snapshot,
    resolve_node_config,
)


def test_snapshot_exposes_node_model_names_for_runtime_construction():
    snapshot = capture_snapshot(
        node_keys=("no_result.intent",),
        published={
            "no_result.intent": {
                "primaryModel": "DeepSeek-V3",
                "fallbackModel": "DeepSeek-R1",
            }
        },
    )

    node = snapshot.nodes["no_result.intent"]
    assert node.channel == "rd_llm"
    assert node.primary_model == "DeepSeek-V3"
    assert node.fallback_model == "DeepSeek-R1"


def test_published_primary_and_fallback_override_environment_and_defaults(monkeypatch):
    spec = NODE_REGISTRY["no_result.intent"]
    monkeypatch.setenv(spec.primary_env, "env-primary")
    monkeypatch.setenv(spec.fallback_env, "env-fallback")

    resolved = resolve_node_config(
        "no_result.intent",
        published={"primaryModel": "DeepSeek-V3", "fallbackModel": "DeepSeek-R1"},
    )

    assert resolved.channel == "rd_llm"
    assert resolved.primary_model == "DeepSeek-V3"
    assert resolved.fallback_model == "DeepSeek-R1"
    assert resolved.primary_source == "published"
    assert resolved.fallback_source == "published"


def test_missing_published_values_keep_legacy_environment_behavior(monkeypatch):
    spec = NODE_REGISTRY["no_result.intent"]
    monkeypatch.setenv(spec.primary_env, "legacy-primary")
    monkeypatch.setenv(spec.fallback_env, "legacy-fallback")

    resolved = resolve_node_config("no_result.intent")

    assert resolved.primary_model == "legacy-primary"
    assert resolved.fallback_model == "legacy-fallback"
    assert resolved.primary_source == "env"
    assert resolved.fallback_source == "env"


def test_missing_environment_keeps_current_code_defaults(monkeypatch):
    spec = NODE_REGISTRY["no_result.intent"]
    monkeypatch.delenv(spec.primary_env, raising=False)
    monkeypatch.delenv(spec.fallback_env, raising=False)

    resolved = resolve_node_config("no_result.intent")

    assert resolved.primary_model == spec.default_primary
    assert resolved.fallback_model == spec.default_fallback
    assert resolved.primary_source == "default"
    assert resolved.fallback_source == "default"


def test_rd_llm_node_keeps_its_registered_fallback_default(monkeypatch):
    spec = NODE_REGISTRY["no_result.intent"]
    monkeypatch.delenv(spec.primary_env, raising=False)
    monkeypatch.delenv(spec.fallback_env, raising=False)

    assert resolve_node_config("no_result.intent").fallback_model is None


def test_pi_agent_is_the_assistant_channel_and_custom_model_names_are_allowed():
    resolved = resolve_node_config(
        "assistant.chat",
        published={"primaryModel": "DeepSeek-V3.1", "fallbackModel": None},
    )

    assert resolved.channel == "pi_agent"
    assert resolved.primary_model == "DeepSeek-V3.1"
    assert resolved.primary_model != resolved.channel
    assert resolved.fallback_model is None


def test_snapshot_is_immutable_after_later_configuration_changes(monkeypatch):
    spec = NODE_REGISTRY["no_result.intent"]
    monkeypatch.setenv(spec.primary_env, "before-release")
    snapshot = capture_snapshot(node_keys=("no_result.intent",))

    monkeypatch.setenv(spec.primary_env, "after-release")
    later = capture_snapshot(node_keys=("no_result.intent",))

    assert snapshot.nodes["no_result.intent"].primary_model == "before-release"
    assert later.nodes["no_result.intent"].primary_model == "after-release"
    with pytest.raises(FrozenInstanceError):
        snapshot.nodes = {}


@pytest.mark.parametrize(
    "published",
    [
        {"primaryModel": "", "fallbackModel": None},
        {"primaryModel": "has space", "fallbackModel": None},
        {"primaryModel": "x" * 121, "fallbackModel": None},
        {"primaryModel": "valid", "fallbackModel": "has space"},
        {"primaryModel": "valid", "fallbackModel": "x" * 121},
        {"primaryModel": "same", "fallbackModel": "same"},
        {"primaryModel": "valid", "fallbackModel": 123},
    ],
)
@pytest.mark.parametrize("node_key", ["no_result.intent", "query_governance.relevance_multimodal"])
def test_invalid_published_config_fails_closed(published, node_key):
    with pytest.raises(NodeConfigError):
        resolve_node_config(node_key, published=published)


def test_unknown_node_fails_closed():
    with pytest.raises(NodeConfigError):
        resolve_node_config("unknown.node")


@pytest.mark.parametrize("published", [[], (), "not-a-mapping"])
def test_resolve_rejects_non_mapping_published_input(published):
    with pytest.raises(NodeConfigError):
        resolve_node_config("no_result.intent", published=published)


@pytest.mark.parametrize(
    "node_keys,published",
    [
        (None, []),
        (None, {"unknown.node": {"primaryModel": "DeepSeek-V3"}}),
        ((), None),
        ("no_result.intent", None),
        (("",), None),
        (("unknown.node",), None),
    ],
)
def test_snapshot_rejects_malformed_input_without_defaulting(node_keys, published):
    with pytest.raises(NodeConfigError):
        capture_snapshot(node_keys=node_keys, published=published)
