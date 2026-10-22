"""Server-side client for PI's isolated, read-only relevance KG task."""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import httpx

from src.llm.rd_client import DEFAULT_RD_LLM_BASE_URL
from src.llm.node_config import model_audit_fields
from .langfuse import sanitize_trace_data


class PiGovernanceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.error_code = code
        super().__init__(message)


class PiGovernanceClient:
    def __init__(self, *, timeout_seconds: float = 1200, runtime_url: str | None = None, post=None):
        self.timeout_seconds = timeout_seconds
        self.runtime_url = (runtime_url or os.getenv('PI_AGENT_RUNTIME_URL', 'http://127.0.0.1:8124')).rstrip('/')
        self._post = post or httpx.post

    def run(self, *, runtime: Any, brand: str, scene_id: str, request: Mapping[str, Any],
            system_prompt: str, output_schema: Mapping[str, Any], observability=None,
            langfuse_config=None) -> dict[str, Any]:
        config = runtime.model_config
        api_key = os.getenv('RD_LLM_API_KEY', '')
        if not api_key:
            raise RuntimeError('KG PI model credentials are unavailable')
        model_config = {
            'provider': 'aiops-gateway', 'model': config.primary_model,
            'fallbackModel': config.fallback_model,
            'baseUrl': os.getenv('RD_LLM_BASE_URL', DEFAULT_RD_LLM_BASE_URL),
            'apiKey': api_key,
        }
        observation = (observability.start_agent_observation(
            langfuse_config=langfuse_config, name='query-governance.relevance-agent',
            input=dict(request), metadata={'runtime': 'pi', 'prompt_version': 'relevance-kg-mcp-v1'},
        ) if observability is not None else None)
        try:
            response = self._post(
                self.runtime_url + '/v1/kg/relevance/run',
                headers={'X-PI-Internal-Token': os.getenv('PI_AGENT_INTERNAL_TOKEN', 'local-pi-runtime')},
                json={'brand': brand, 'sceneId': scene_id, 'userInput': dict(request),
                      'systemPrompt': system_prompt, 'outputSchema': dict(output_schema),
                      'modelConfig': model_config, 'maxToolCalls': 24, 'timeoutSeconds': self.timeout_seconds},
                timeout=self.timeout_seconds + 30,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get('output'), Mapping) or not isinstance(result.get('toolResults'), list):
                raise ValueError('PI KG response must include output and structured toolResults')
            for call in result['toolResults']:
                if not isinstance(call, Mapping) or not isinstance(call.get('name'), str) or not isinstance(call.get('args'), Mapping) or not isinstance(call.get('result'), Mapping):
                    raise ValueError('PI KG tool evidence has invalid structure')
            usage = result.get('usage') or {}
            audit = {**model_audit_fields(config, str(result.get('modelRole') or 'primary')),
                     'configVersion': runtime.model_config_snapshot.version,
                     'requestId': uuid4().hex, 'tokenUsage': usage.get('totalTokens') if isinstance(usage, Mapping) else None,
                     'runtime': 'pi'}
            result['audit'] = audit
            if observability is not None:
                for call in result['toolResults']:
                    tool_observation = observability.start_tool_observation(
                        langfuse_config=langfuse_config, parent=observation, name=call['name'],
                        input=sanitize_trace_data(call['args']), metadata={'runtime': 'pi'},
                    )
                    observability.finish_tool_observation(tool_observation, output=sanitize_trace_data(call['result']))
                if observation is not None:
                    generation = observation.start_observation(name='query-governance.relevance-agent.pi', as_type='generation',
                        input=sanitize_trace_data(request), metadata=sanitize_trace_data(audit))
                    updates = {'output': sanitize_trace_data(result['output']), 'model': config.primary_model}
                    if isinstance(audit['tokenUsage'], (int, float)):
                        updates['usage_details'] = {'total_tokens': audit['tokenUsage']}
                    generation.update(**updates)
                    generation.end()
                observability.finish_agent_observation(observation, output=sanitize_trace_data(result['output']))
            return result
        except (httpx.HTTPError, ValueError) as exc:
            # HTTP errors can carry request bodies or gateway credentials. Only a
            # fixed diagnosis and status code cross the task/log boundary.
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if isinstance(exc, httpx.TimeoutException):
                code, message = 'PI_RUNTIME_TIMEOUT', 'PI 治理请求超时'
            elif isinstance(exc, httpx.ConnectError):
                code, message = 'PI_RUNTIME_UNAVAILABLE', '无法连接 PI Runtime，请检查运行时服务与地址配置'
            elif status in (401, 403):
                code, message = 'PI_RUNTIME_AUTH_FAILED', 'PI Runtime 内部认证失败'
            elif status:
                code, message = 'PI_RUNTIME_HTTP_FAILED', f'PI 治理请求失败（HTTP {status}）'
            elif isinstance(exc, ValueError):
                code, message = 'PI_OUTPUT_INVALID', 'PI 返回的数据不符合结构化输出契约'
            else:
                code, message = 'PI_RUNTIME_TRANSPORT_FAILED', 'PI Runtime 通信失败' 
            if observability is not None:
                observability.finish_agent_observation(observation, error_message=message)
            raise PiGovernanceError(code, message) from None
