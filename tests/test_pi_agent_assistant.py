import json
from pathlib import Path

from src.assistant.service import PiAgentAssistantService


def test_pi_runtime_client_posts_to_embedded_sdk_without_cli_flags():
    captured = {}

    def transport(url, payload, timeout):
        captured.update(url=url, payload=payload, timeout=timeout)
        yield json.dumps({"type": "agent_end"})

    service = PiAgentAssistantService(
        workdir="/tmp/aiops-pi",
        session_dir="/tmp/pi-test-sessions",
        model_name="qwen3.8-flash",
        runtime_url="http://127.0.0.1:8124",
        runtime_transport=transport,
    )
    events = list(service.stream_chat(
        message="检查昨天的自动化运营状态",
        user_id=7,
        username="operator",
        allowed_brands=["atlas", "harbor"],
        context={"summary": {"taskCount": 1}},
        session_id="assistant-7-yesterday",
    ))

    assert captured["url"].endswith("/v1/sessions/assistant-7-yesterday/stream")
    assert "--mode" not in captured["url"]
    assert captured["payload"]["permissionScope"]["brands"] == ["atlas", "harbor"]
    assert "read_today_context" in captured["payload"]["permissionScope"]["tools"]
    assert captured["payload"]["userId"] == 7
    assert captured["payload"]["permissionScope"]["workdir"] == str(Path("/tmp/aiops-pi").resolve())
    assert [event["type"] for event in events] == ["done"]


def test_pi_runtime_client_maps_sdk_events_and_preserves_account_scope():
    def transport(_url, payload, _timeout):
        assert payload["permissionScope"]["brands"] == ["atlas"]
        yield json.dumps({"type": "tool_call", "id": "call-1", "name": "bash", "arguments": '{"command":"pwd"}'})
        yield json.dumps({"type": "tool_result", "toolName": "bash", "summary": "/tmp/aiops"})
        yield json.dumps({"type": "delta", "content": "已检查本地目录。"})
        yield json.dumps({"type": "agent_end"})

    service = PiAgentAssistantService(runtime_transport=transport, runtime_url="http://pi-runtime")
    result = service.chat(
        message="检查当前目录",
        user_id=7,
        username="operator",
        allowed_brands=["atlas"],
        context={"date": "2026-09-10"},
        session_id="assistant-7-20260910",
    )

    assert result["message"] == "已检查本地目录。"
    assert result["toolCalls"][0]["name"] == "bash"
    assert result["toolResults"][0]["toolName"] == "bash"
    assert result["status"] == "ok"


def test_pi_runtime_client_preserves_phase_and_complete_tool_execution_details():
    def transport(_url, _payload, _timeout):
        yield json.dumps({"type": "phase", "phase": "thinking", "label": "正在思考"})
        yield json.dumps({"type": "tool_call", "id": "call-1", "name": "bash", "arguments": '{"command":"pwd"}'})
        yield json.dumps({
            "type": "tool_result",
            "id": "call-1",
            "toolName": "bash",
            "summary": "command failed",
            "status": "failed",
            "output": "permission denied",
            "error": "denied",
            "exitCode": 1,
            "durationMs": 42,
        })
        yield json.dumps({"type": "agent_end"})

    events = list(PiAgentAssistantService(runtime_transport=transport).stream_chat(
        message="执行命令",
        user_id=7,
        username="operator",
        allowed_brands=["atlas"],
        context={},
        session_id="assistant-tool-details",
    ))

    assert events[0] == {"type": "phase", "phase": "thinking", "label": "正在思考"}
    assert events[2] == {
        "type": "tool_result",
        "id": "call-1",
        "toolName": "bash",
        "summary": "command failed",
        "status": "failed",
        "output": "permission denied",
        "error": "denied",
        "exitCode": 1,
        "durationMs": 42,
    }


def test_pi_runtime_client_surfaces_missing_runtime_as_actionable_error():
    def transport(_url, _payload, _timeout):
        raise OSError("connection refused")
        yield  # pragma: no cover

    events = list(PiAgentAssistantService(runtime_transport=transport).stream_chat(
        message="执行诊断",
        user_id=7,
        username="operator",
        allowed_brands=["atlas"],
        context={},
        session_id="assistant-7-failed",
    ))

    assert events[0]["type"] == "error"
    assert "SDK runtime" in events[0]["content"]
    assert events[-1]["type"] == "done"
