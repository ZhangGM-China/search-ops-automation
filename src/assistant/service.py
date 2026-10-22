"""PI Agent runtime client backed by the project's Node SDK runtime.

The FastAPI process talks to a local NDJSON endpoint owned by the embedded PI
SDK service. The application no longer starts or depends on the PI CLI.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import httpx

from src.llm.node_config import ResolvedNodeConfig, model_audit_fields
from src.query_governance.langfuse import QueryGovernanceObservability
from src.query_governance.models import QuerySpec


class PiAgentAssistantService:
    def __init__(
        self,
        *,
        command: Optional[str] = None,
        workdir: Optional[str] = None,
        session_dir: Optional[str] = None,
        model_label: Optional[str] = None,
        model_name: Optional[str] = None,
        available_models: Optional[List[str]] = None,
        supports_model_switch: Optional[bool] = None,
        timeout_seconds: Optional[float] = None,
        model_config: Optional[ResolvedNodeConfig] = None,
        observability: Optional[QueryGovernanceObservability] = None,
        runtime_url: Optional[str] = None,
        runtime_transport: Any = None,
    ):
        self.workdir = Path(workdir or os.getenv("PI_AGENT_WORKDIR", os.getcwd())).resolve()
        self.session_dir = Path(session_dir or os.getenv("PI_AGENT_SESSION_DIR", ".pi/assistant-sessions")).expanduser()
        if not self.session_dir.is_absolute():
            self.session_dir = self.workdir / self.session_dir
        self.model_name = model_name if model_name is not None else (os.getenv("PI_AGENT_MODEL", "").strip() or None)
        self.model_label = model_label or os.getenv("PI_AGENT_MODEL_LABEL", "PI Agent")
        if available_models is not None:
            self.available_models = list(available_models)
        else:
            self.available_models = [item.strip() for item in os.getenv("PI_AGENT_MODELS", "").split(",") if item.strip()]
        if self.model_name and self.model_name not in self.available_models:
            self.available_models.insert(0, self.model_name)
        self.supports_model_switch = bool(
            supports_model_switch if supports_model_switch is not None else len(self.available_models) > 1
        )
        raw_timeout = timeout_seconds if timeout_seconds is not None else os.getenv("PI_AGENT_TIMEOUT_SECONDS", "120")
        try:
            self.timeout_seconds = max(3.0, float(raw_timeout))
        except (TypeError, ValueError):
            self.timeout_seconds = 120.0
        self.model_config = model_config
        self.observability = observability or QueryGovernanceObservability.from_environment()
        self.runtime_url = (runtime_url or os.getenv("PI_AGENT_RUNTIME_URL", "http://127.0.0.1:8124")).rstrip("/")
        self.runtime_transport = runtime_transport

    def chat(
        self,
        message: str,
        user_id: int,
        username: str,
        allowed_brands: List[str],
        context: Dict[str, Any],
        session_id: str,
        selected_brand: Optional[str] = None,
    ) -> Dict[str, Any]:
        events = list(self._run(message, user_id, username, allowed_brands, context, session_id, selected_brand))
        text = "".join(str(event.get("content") or "") for event in events if event.get("type") == "delta")
        tool_calls = [self._tool_call_payload(event) for event in events if event.get("type") == "tool_call"]
        tool_results = [self._tool_result_payload(event) for event in events if event.get("type") == "tool_result"]
        error = next((str(event.get("content") or "") for event in events if event.get("type") == "error"), None)
        if error and not text:
            text = error
        return {
            "sessionId": session_id,
            "message": text,
            "toolCalls": tool_calls,
            "toolResults": tool_results,
            "proposedActions": [],
            "contextSummary": self._context_summary(context),
            "status": "failed" if error else "ok",
            "runtime": self.runtime_info(),
        }

    def stream_chat(
        self,
        message: str,
        user_id: int,
        username: str,
        allowed_brands: List[str],
        context: Dict[str, Any],
        session_id: str,
        selected_brand: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        yield from self._run(message, user_id, username, allowed_brands, context, session_id, selected_brand)

    def runtime_info(self) -> Dict[str, Any]:
        runtime = {
            "provider": "PI Agent",
            "modelLabel": self.model_label,
            "modelName": self.model_name,
            "fallbackModelName": None,
            "selectedModelName": self.model_name,
            "availableModels": self.available_models,
            "supportsModelSwitch": self.supports_model_switch,
            "agentId": None,
            "teamId": None,
        }
        if self.model_config is not None:
            runtime.update(model_audit_fields(self.model_config, "primary"))
        else:
            runtime.update({
                "nodeKey": "assistant.chat",
                "channel": "pi_agent",
                "modelRole": "primary",
                "configSource": "env" if self.model_name else "unconfigured",
            })
        return runtime

    def _run(self, message, user_id, username, allowed_brands, context, session_id, selected_brand):
        payload = {
            "sessionId": session_id,
            "prompt": message,
            "operator": username,
            "userId": int(user_id),
            "permissionScope": {
                "brands": list(allowed_brands or []),
                "tools": [
                    "read", "write", "edit", "bash", "grep", "find", "ls",
                    "read_today_context", "read_top_query_summary", "read_brand_metrics",
                    "read_top_query_detail", "read_top_query_events", "read_abnormal_query_logs",
                    "read_inventory_monitoring", "search_products", "get_product_detail", "prepare_action",
                ],
                "workdir": str(self.workdir),
            },
            "context": self._compact_context(context),
            "model": self.model_name,
            "provider": os.getenv("PI_AGENT_PROVIDER", "").strip() or None,
            "workdir": str(self.workdir),
            "sessionDir": str(self.session_dir),
            "apiBaseUrl": os.getenv("AIOPS_INTERNAL_API_URL", "http://127.0.0.1:8014"),
            "internalToken": os.getenv("PI_AGENT_INTERNAL_TOKEN", "local-pi-runtime"),
        }
        saw_agent_end = False
        saw_error = False
        output_parts = []
        observation = None
        try:
            trace_query_id = f"assistant-{session_id}-{uuid4().hex}"
            trace = self.observability.start_batch(
                trace_query_id,
                [QuerySpec(trace_query_id, selected_brand or "unknown", message)],
                metadata={"operation": "assistant.chat", "user_id": user_id},
                trace_name="assistant",
            )
            langfuse_config = trace.query_config(trace_query_id, "assistant.chat")
            observation = self.observability.start_agent_observation(
                langfuse_config=langfuse_config,
                name="assistant.chat",
                input={"message": message, "session_id": session_id},
                metadata={"node": "assistant.chat", "username": username},
            )
        except Exception:
            langfuse_config = None
        try:
            for raw_line in self._iter_runtime_lines(payload):
                event = self._decode_event(raw_line)
                if not event:
                    continue
                event_type = event.get("type")
                if event_type == "phase" and event.get("phase"):
                    yield {
                        "type": "phase",
                        "phase": str(event.get("phase")),
                        "label": str(event.get("label") or ""),
                    }
                elif event_type == "delta" and event.get("content"):
                    content = str(event["content"])
                    output_parts.append(content)
                    yield {"type": "delta", "content": content}
                elif event_type == "tool_call":
                    yield {"type": "tool_call", "id": str(event.get("id") or ""), "name": str(event.get("name") or ""), "arguments": str(event.get("arguments") or "{}")}
                elif event_type == "tool_result":
                    yield {
                        "type": "tool_result",
                        "id": str(event.get("id") or ""),
                        "toolName": str(event.get("toolName") or ""),
                        "summary": str(event.get("summary") or ""),
                        "status": str(event.get("status") or "completed"),
                        "output": str(event.get("output") or ""),
                        "error": str(event.get("error") or ""),
                        "exitCode": event.get("exitCode"),
                        "durationMs": event.get("durationMs"),
                    }
                elif event_type == "agent_end":
                    saw_agent_end = True
                elif event_type == "error":
                    saw_error = True
                    yield {"type": "error", "content": str(event.get("message") or event.get("content") or "PI Agent 执行失败")}
            if not saw_agent_end and not saw_error:
                # The SDK runtime emits `done` after prompt() resolves. A missing
                # agent_end remains actionable evidence rather than a silent success.
                yield {"type": "error", "content": "PI Agent 未返回 agent_end 事件"}
            yield {"type": "done", "content": "", "runtime": self.runtime_info()}
        except (OSError, ValueError, httpx.HTTPError) as exc:
            yield {"type": "error", "content": f"PI Agent SDK runtime 调用失败: {exc}"}
            yield {"type": "done", "content": "", "runtime": self.runtime_info()}
        finally:
            self.observability.finish_agent_observation(
                observation,
                output={
                    "status": "failed" if saw_error else "ok",
                    "response_chars": len("".join(output_parts)),
                },
                error_message="PI Agent 执行失败" if saw_error else None,
            )

    def _iter_runtime_lines(self, payload: Dict[str, Any]):
        url = f"{self.runtime_url}/v1/sessions/{payload['sessionId']}/stream"
        if self.runtime_transport is not None:
            yield from self.runtime_transport(url, payload, self.timeout_seconds)
            return
        with httpx.stream("POST", url, json=payload, timeout=self.timeout_seconds) as response:
            response.raise_for_status()
            yield from response.iter_lines()

    @staticmethod
    def _compact_context(context):
        return {
            "date": context.get("date"),
            "summary": context.get("summary") or {},
            "pageContext": context.get("pageContext") or {},
            "queries": (context.get("queries") or [])[:20],
            "recentActions": (context.get("recentActions") or [])[:20],
            "inventoryMonitoring": (context.get("inventoryMonitoring") or [])[:20],
        }

    @staticmethod
    def _decode_event(raw_line):
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode("utf-8", errors="replace")
        try:
            event = json.loads(str(raw_line).strip())
        except (TypeError, ValueError):
            return {}
        return event if isinstance(event, dict) else {}

    @staticmethod
    def _message_text(message):
        if isinstance(message, str):
            return message
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text") or item.get("content") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
            )
        return ""

    def _agent_end_text(self, messages):
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                text = self._message_text(message)
                if text:
                    return text
        return ""

    @staticmethod
    def _tool_call_payload(event):
        return {
            "id": str(event.get("id") or ""),
            "name": str(event.get("name") or ""),
            "arguments": str(event.get("arguments") or "{}"),
        }

    @staticmethod
    def _tool_result_payload(event):
        return {
            "toolCallId": str(event.get("id") or ""),
            "toolName": str(event.get("toolName") or ""),
            "summary": str(event.get("summary") or ""),
            "status": str(event.get("status") or "completed"),
            "output": str(event.get("output") or ""),
            "error": str(event.get("error") or ""),
            "exitCode": event.get("exitCode"),
            "durationMs": event.get("durationMs"),
        }

    @classmethod
    def _tool_result_summary(cls, result, is_error):
        text = cls._message_text(result)
        if not text and isinstance(result, dict):
            text = cls._message_text({"content": result.get("content")})
        if not text:
            text = "工具执行失败" if is_error else "工具执行完成"
        return text[:1000]

    @staticmethod
    def _context_summary(context):
        summary = context.get("summary") if isinstance(context, dict) else None
        return summary if isinstance(summary, dict) else {}
