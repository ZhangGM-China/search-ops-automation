"""KG Agent and its isolated asynchronous task boundary.

The agent may inspect the graph and submit a structured proposal, but it never
calls a knowledge-graph write endpoint.  Human execution remains a separate
step after the current graph state has been checked again.
"""

from __future__ import annotations

import json
import datetime
import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence, TypedDict

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.query_governance.async_tasks import (
    AsyncTaskRecord,
    AsyncTaskStatus,
    QueryGovernanceAsyncTaskService,
)
from src.query_governance.models import (
    GovernancePlanExecutorType,
    GovernancePlanFailureStage,
    GovernancePlanStatus,
    NodeStatus,
    QueryGovernanceActionPlan,
    QuerySpec,
)
from src.query_governance.langfuse import QueryGovernanceObservability
from src.governance.query_identity import transform_governance_payload_query
from src.query_governance.kg_action_contract import normalize_kg_actions
from src.query_governance.kg_evidence import graph_context as build_graph_context, digest as graph_digest, validate_actions
from src.query_governance.visibility import strictest_execution_mode
from src.query_governance.read_only_agent import AgentToolError, run_read_only_agent
from src.query_governance.relevance_remediation import RelevanceRootCauseAgent
from src.query_governance.kg_execution import QueryGovernanceKGActionExecutor


KG_TRIGGER_SOURCES = frozenset({"recall_anomaly"})
KG_READ_ACTIONS = frozenset({"query_tag", "query_relationship", "query_dimension"})
KG_PLAN_ACTIONS = frozenset(
    {"add_tag", "update_tag", "blacklist_tag", "add_relationship", "update_relationship", "delete_relationship"}
)
KG_ACTION_ENDPOINTS = {
    "add_tag": "/admin/tag/addTag",
    "update_tag": "/admin/tag/updateTag",
    "blacklist_tag": "/admin/tag/updateTagInfo",
    "add_relationship": "/admin/tagRelationship/addRelationship",
    "update_relationship": "/admin/tagRelationship/update",
    "delete_relationship": "/admin/tagRelationship/delete",
}

logger = logging.getLogger(__name__)

_ACTION_PLAN_MAX_BYTES = 60_000
_PERSISTED_ACTION_KEYS = frozenset({
    "actionType", "action_type", "title", "description", "params", "reason",
    "riskLevel", "risk_level", "targetInterface", "target_interface", "dependsOn",
    "depends_on", "rollbackPlan", "rollback_plan", "step", "index", "causeGroupId",
})
_PERSISTED_GROUP_KEYS = frozenset({
    "groupId", "group_id", "rootCause", "root_cause", "causeKey", "cause_key",
    "title", "conclusion", "reason", "reasons", "confidence", "productIds",
    "product_ids", "status", "statusLabel", "status_label", "executionMode",
    "execution_mode", "executable", "coreTags", "core_tags", "graphContextDigest",
    "graph_context_digest", "analysisEvidence", "analysis_evidence",
    "executionResult", "execution_result", "error",
    "rawActions", "repairedActions", "initialValidationErrors", "validationErrors", "repairAttempts",
})


def _compact_action(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items() if str(key) in _PERSISTED_ACTION_KEYS}


def _compact_tool_evidence(value: Any) -> dict[str, Any]:
    evidence = value if isinstance(value, Mapping) else {}
    compact: dict[str, Any] = {}
    searches = evidence.get("searches")
    if isinstance(searches, Mapping):
        compact["searches"] = {
            str(query): ({"result_count": result.get("result_count")}
                         if isinstance(result, Mapping) and result.get("result_count") is not None
                         else {"error": True} if isinstance(result, Mapping) and result.get("error")
                         else {})
            for query, result in searches.items()
        }
    for source_key in ("tags", "relationships"):
        source = evidence.get(source_key)
        if isinstance(source, Mapping):
            compact[source_key] = {
                str(name): _compact_graph_result(result)
                for name, result in source.items()
            }
    errors = evidence.get("errors")
    if isinstance(errors, list):
        compact["errors"] = [
            {
                key: item.get(key)
                for key in ("operation", "tag", "query", "code")
                if item.get(key) is not None
            }
            for item in errors
            if isinstance(item, Mapping)
        ]
    calls = evidence.get("calls")
    if isinstance(calls, list):
        compact["calls"] = [
            {
                "name": item.get("name"),
                "args": dict(item.get("args") or {}) if isinstance(item.get("args"), Mapping) else {},
                "result": ({"result_count": item["result"].get("result_count"), "error": item["result"].get("error")} if item.get("name") == "search_products" and isinstance(item.get("result"), Mapping) else _compact_graph_result(item.get("result"))),
            }
            for item in calls
            if isinstance(item, Mapping)
        ]
    return compact


def _compact_graph_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    if value.get("error"):
        error = value.get("error")
        return {"error": {"code": error.get("code")} if isinstance(error, Mapping) else True}
    rows = value.get("data")
    if not isinstance(rows, list):
        return {"queried": True}
    compact_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = {str(key): value for key, value in row.items() if key not in {"createTime", "updateTime", "raw", "childTags"}}
        compact_rows.append(item)
    return {"data": compact_rows}


def _compact_plan_evidence(value: Any) -> dict[str, Any]:
    evidence = value if isinstance(value, Mapping) else {}
    compact: dict[str, Any] = {}
    for key, item in evidence.items():
        if key in {"products", "blacklist_candidates", "blacklistCandidates"} and isinstance(item, list):
            compact[key] = [
                {
                    str(field): row[field]
                    for field in (
                        "product_id", "productId", "sku_id", "skuId", "product_name",
                        "productName", "name", "reason", "status", "broken_rate_percent",
                        "executed_broken_rate", "restored_broken_rate",
                        "product_details", "tags", "gender", "category", "style", "material", "description",
                    )
                    if row.get(field) is not None
                }
                for row in item
                if isinstance(row, Mapping)
            ]
        elif key in {"causeGroups", "cause_groups", "queryTags", "query_tags", "segmented_tags", "segmentedTags"}:
            compact[key] = item
        elif isinstance(item, (str, int, float, bool)) or item is None:
            compact[key] = item
    return compact


def _compact_action_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    graph_context = plan.get("graph_context") or plan.get("graphContext") or {}
    compact: dict[str, Any] = {
        key: plan[key]
        for key in (
            "brand", "query_text", "trigger_source", "core_tags", "coreTags",
            "decision", "reason", "summary", "queryTags", "errorProductIds", "warnings", "executionMode", "evidence_omitted",
        )
        if key in plan
    }
    compact["actions"] = [
        action
        for raw in (plan.get("actions") or [])
        if (action := _compact_action(raw)) is not None
    ]
    groups: list[dict[str, Any]] = []
    for raw_group in (plan.get("causeGroups") or plan.get("cause_groups") or []):
        if not isinstance(raw_group, Mapping):
            continue
        group = {
            str(key): item
            for key, item in raw_group.items()
            if str(key) in _PERSISTED_GROUP_KEYS
        }
        group["actions"] = [
            action
            for raw in (raw_group.get("actions") or raw_group.get("steps") or [])
            if (action := _compact_action(raw)) is not None
        ]
        group_context = raw_group.get("graphContext") or raw_group.get("graph_context") or {}
        existing_group_digest = str(
            raw_group.get("graphContextDigest") or raw_group.get("graph_context_digest") or ""
        ).strip()
        if isinstance(group_context, Mapping) and group_context.get("version") == 2:
            group["graphContext"] = dict(group_context)
        if existing_group_digest:
            group["graphContextDigest"] = existing_group_digest
        elif isinstance(group_context, Mapping) and group_context:
            group["graphContextDigest"] = hashlib.sha256(_canonical_json(group_context).encode("utf-8")).hexdigest()
        groups.append(group)
    if groups:
        compact["causeGroups"] = groups
    compact_graph_context = dict(graph_context) if graph_context.get("version") == 2 else _compact_tool_evidence(graph_context)
    compact["graph_context"] = compact_graph_context
    compact["tool_evidence"] = _compact_tool_evidence(plan.get("tool_evidence") or graph_context)
    existing_digest = str(plan.get("graph_context_digest") or "").strip()
    if existing_digest:
        compact["graph_context_digest"] = existing_digest
    elif isinstance(graph_context, Mapping) and graph_context:
        compact["graph_context_digest"] = hashlib.sha256(_canonical_json(graph_context).encode("utf-8")).hexdigest()
    serialized = _canonical_json(compact)
    if len(serialized.encode("utf-8")) > _ACTION_PLAN_MAX_BYTES:
        compact["tool_evidence"] = {}
        compact["evidence_omitted"] = True
        serialized = _canonical_json(compact)
    if len(serialized.encode("utf-8")) > _ACTION_PLAN_MAX_BYTES:
        # 执行前会重放查询并比较完整结果摘要；超限时仅省略重复结果。
        contexts = [compact["graph_context"]] + [g.get("graphContext") for g in groups]
        for context in contexts:
            if isinstance(context, dict) and context.get("version") == 2:
                context["reads"] = [{"name": r["name"], "args": r["args"]} for r in context.get("reads", [])]
                context["resultsOmitted"] = True
        serialized = _canonical_json(compact)
    if len(serialized.encode("utf-8")) > _ACTION_PLAN_MAX_BYTES:
        raise KGAgentError("KG_PLAN_TOO_LARGE", "KG Agent 方案保存失败")
    return compact


class KGAgentStatus(str, Enum):
    PLAN_GENERATED = "plan_generated"
    ERROR = "error"


class KGAgentError(RuntimeError):
    """A stable, user-readable KG Agent failure."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.message = message
        super().__init__(f"{error_code}: {message}")


class KGPlanConflictError(RuntimeError):
    """The graph changed after a proposal was generated."""

    def __init__(self, message: str = "KG_PLAN_CONFLICT: knowledge graph changed") -> None:
        super().__init__(message)
        self.error_code = "KG_PLAN_CONFLICT"


@dataclass(frozen=True)
class KGAgentResult:
    status: KGAgentStatus
    plan: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


class KnowledgeGraphClientProtocol(Protocol):
    def execute(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


class KnowledgeGraphReadTools:
    """Expose graph reads and the real product-search evidence boundary."""

    def __init__(
        self,
        client: KnowledgeGraphClientProtocol,
        *,
        search: Any | None = None,
        observability: QueryGovernanceObservability | None = None,
    ) -> None:
        self._client = client
        self._search = search
        self.observability = observability

    def query_tag(
        self,
        brand: str,
        tag_name: str,
        *,
        langfuse_config: Mapping[str, Any] | None = None,
        parent_observation: Any | None = None,
    ) -> Mapping[str, Any]:
        return self._read_pages(
            "query_tag",
            brand,
            {"name": tag_name, "nameAcc": tag_name, "pageNum": 1, "pageSize": 30},
            langfuse_config=langfuse_config,
            parent_observation=parent_observation,
        )

    def query_relationship(
        self,
        brand: str,
        tag_name: str,
        *,
        side: str = "both",
        langfuse_config: Mapping[str, Any] | None = None,
        parent_observation: Any | None = None,
    ) -> Mapping[str, Any]:
        normalized_side = str(side or "both").strip().lower()
        if normalized_side not in {"source", "target", "both"}:
            raise ValueError("relationship side must be source, target, or both")
        requests: list[tuple[str, Mapping[str, Any]]] = []
        if normalized_side in {"source", "both"}:
            requests.append(("source", {"tagNameAcc": tag_name, "pageNum": 1, "pageSize": 30}))
        if normalized_side in {"target", "both"}:
            requests.append(("target", {"resTagName": tag_name, "pageNum": 1, "pageSize": 30}))
        results = [
            (request_side, self._read_pages("query_relationship", brand, params, langfuse_config=langfuse_config, parent_observation=parent_observation))
            for request_side, params in requests
        ]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for request_side, result in results:
            data = result.get("data") if isinstance(result, Mapping) else None
            for row in data if isinstance(data, list) else []:
                if not isinstance(row, Mapping):
                    continue
                field = "responseTagName" if request_side == "target" else "tagName"
                if str(row.get(field) or "").strip() != tag_name:
                    continue
                key = str(row.get("id") or json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
                if key not in seen:
                    seen.add(key)
                    rows.append(dict(row))
        return {"data": rows, "count": len(rows), "query_side": normalized_side}

    def query_dimension(
        self,
        brand: str,
        dimension_name: str,
        *,
        langfuse_config: Mapping[str, Any] | None = None,
        parent_observation: Any | None = None,
    ) -> Mapping[str, Any]:
        return self._read_pages(
            "query_dimension",
            brand,
            {"name": dimension_name, "pageNum": 1, "pageSize": 30},
            langfuse_config=langfuse_config,
            parent_observation=parent_observation,
        )

    def search_products(
        self,
        brand: str,
        query: str,
        *,
        page_size: int = 16,
        page_num: int = 1,
        langfuse_config: Mapping[str, Any] | None = None,
        parent_observation: Any | None = None,
    ) -> Mapping[str, Any]:
        search_fn = None
        if self._search is not None:
            search_fn = getattr(self._search, "mixed_with_total", None)
            if not callable(search_fn):
                search_fn = getattr(self._search, "search_products_with_total", None)
        if not callable(search_fn):
            raise KGAgentError("KG_SEARCH_UNAVAILABLE", "搜索工具不可用，无法基于真实召回数据判断")
        observation = None
        observability = self.observability
        if observability is None and langfuse_config:
            observability = QueryGovernanceObservability.from_environment()
            self.observability = observability
        if observability is not None:
            observation = observability.start_tool_observation(
                langfuse_config=langfuse_config,
                name="search.products",
                input={"brand": brand, "query": query, "page_size": page_size, "page_num": page_num},
                metadata={"node": "kg_agent", "tool": "search_products"},
                parent=parent_observation,
            )
        try:
            raw = search_fn(brand, query, page_size=page_size, page_num=page_num)
            if not isinstance(raw, Mapping):
                raise KGAgentError("KG_SEARCH_INVALID", "搜索接口返回格式无效")
            count = raw.get("total_count", raw.get("totalCount"))
            products = raw.get("products") if isinstance(raw.get("products"), list) else []
            if raw.get("success") is False:
                return {
                    "success": False,
                    "query": query,
                    "error": {
                        "code": "KG_SEARCH_FAILED",
                        "message": str(raw.get("error_reason") or raw.get("message") or "搜索接口返回失败"),
                    },
                }
            result = {"success": True, "query": query,
                      "result_count": count if isinstance(count, (int, float)) else len(products),
                      "products": products, "raw": dict(raw)}
            if observation is not None:
                observability.finish_tool_observation(observation, output=result)
            return result
        except Exception as exc:
            if observation is not None:
                observability.finish_tool_observation(observation, error_code="KG_SEARCH_FAILED", error_message=str(exc))
            raise

    def collect_context(
        self,
        brand: str,
        core_tags: Sequence[str],
        *,
        langfuse_config: Mapping[str, Any] | None = None,
        parent_observation: Any | None = None,
    ) -> dict[str, Any]:
        tags: dict[str, Any] = {}
        relationships: dict[str, Any] = {}
        searches: dict[str, Any] = {}
        errors: list[dict[str, str]] = []
        for tag in dict.fromkeys(str(item).strip() for item in core_tags if str(item).strip()):
            try:
                tags[tag] = self.query_tag(brand, tag, langfuse_config=langfuse_config, parent_observation=parent_observation)
            except Exception as exc:
                error = self._error_payload(exc)
                tags[tag] = {"error": error}
                errors.append({"operation": "query_tag", "tag": tag, **error})
            try:
                relationships[tag] = self.query_relationship(
                    brand, tag, side="both", langfuse_config=langfuse_config, parent_observation=parent_observation
                )
            except Exception as exc:
                error = self._error_payload(exc)
                relationships[tag] = {"error": error}
                errors.append({"operation": "query_relationship", "tag": tag, **error})
            try:
                searches[tag] = self.search_products(brand, tag, langfuse_config=langfuse_config, parent_observation=parent_observation)
            except Exception as exc:
                error = self._error_payload(exc)
                searches[tag] = {"error": error}
                errors.append({"operation": "search_products", "query": tag, **error})
            # Existing related KG labels are also candidates for evidence. The
            # model must never invent a term that was not returned by KG.
            tag_rows = tags[tag].get("data") if isinstance(tags.get(tag), Mapping) else None
            if isinstance(tag_rows, list):
                for row in tag_rows:
                    if not isinstance(row, Mapping):
                        continue
                    related = str(row.get("name") or row.get("tagName") or "").strip()
                    if not related or related in searches:
                        continue
                    try:
                        searches[related] = self.search_products(brand, related, langfuse_config=langfuse_config, parent_observation=parent_observation)
                    except Exception as exc:
                        error = self._error_payload(exc)
                        searches[related] = {"error": error}
                        errors.append({"operation": "search_products", "query": related, **error})
        result = {
            "tags": tags,
            "relationships": relationships,
            "searches": searches,
        }
        if errors:
            result["errors"] = errors
        return result

    def scene_id(self, brand: str) -> str:
        resolver = getattr(self._client, "get_scene_id", None)
        return str(resolver(brand) or "") if callable(resolver) else ""

    def _read_pages(self, action_type, brand, params, **kwargs):
        rows = []
        for page in range(1, 101):
            result = self._read(action_type, brand, {**params, "pageNum": page}, **kwargs)
            data = result.get("data")
            if not isinstance(data, list):
                raise KGAgentError("KG_GRAPH_READ_INVALID", "图谱结果缺少列表")
            rows.extend(data)
            pages, count = result.get("pages"), result.get("count")
            if isinstance(pages, int):
                done = page >= pages
            elif isinstance(count, int):
                done = len(rows) >= count
            else:
                done = len(data) < params.get("pageSize", 30)
            if done:
                return {"data": rows, "complete": True}
            if not data:
                break
        raise KGAgentError("KG_GRAPH_READ_INCOMPLETE", "图谱分页未读全")

    def execute(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        """Fail closed if a caller tries to use this read-only facade to write."""
        action_type = str(action.get("actionType") or action.get("action_type") or "")
        if action_type not in KG_READ_ACTIONS:
            raise PermissionError("KG Agent tools cannot execute graph writes")
        brand = str(action.get("brand") or "")
        params = action.get("params") if isinstance(action.get("params"), Mapping) else {}
        return self._read(action_type, brand, params)

    def _read(
        self,
        action_type: str,
        brand: str,
        params: Mapping[str, Any],
        *,
        langfuse_config: Mapping[str, Any] | None = None,
        parent_observation: Any | None = None,
    ) -> Mapping[str, Any]:
        if action_type not in KG_READ_ACTIONS:
            raise PermissionError("unsupported KG read operation")
        payload = {"brand": brand, "actionType": action_type, "params": dict(params)}
        observability = self.observability
        if observability is None and langfuse_config:
            observability = QueryGovernanceObservability.from_environment()
            self.observability = observability
        observation = (
            observability.start_tool_observation(
                langfuse_config=langfuse_config,
                name=f"kg.{action_type}",
                input={"brand": brand, "params": dict(params)},
                metadata={"node": "kg_agent", "tool": action_type},
                parent=parent_observation,
            )
            if observability is not None
            else None
        )
        try:
            result = self._client.execute(payload)
            if not isinstance(result, Mapping):
                raise KGAgentError(
                    "KG_GRAPH_READ_INVALID", f"{action_type} returned a non-object"
                )
            if result.get("success") is False or result.get("error") or ("code" in result and result["code"] not in (0, 1, 200)):
                raise KGAgentError("KG_GRAPH_READ_FAILED", "图谱接口返回业务失败")
            if observability is not None:
                observability.finish_tool_observation(observation, output=result)
            return result
        except Exception as exc:
            if observability is not None:
                observability.finish_tool_observation(
                    observation,
                    error_code=getattr(exc, "error_code", None) or "KG_GRAPH_READ_FAILED",
                    error_message=str(exc).strip() or exc.__class__.__name__,
                )
            raise

    @staticmethod
    def _error_payload(exc: Exception) -> dict[str, str]:
        return {
            "code": str(getattr(exc, "error_code", None) or "KG_GRAPH_READ_FAILED"),
            "message": str(exc).strip() or exc.__class__.__name__,
        }


class _KGState(TypedDict, total=False):
    brand: str
    query_text: str
    trigger_source: str
    evidence: Mapping[str, Any]
    core_tags: list[str]
    graph_context: Mapping[str, Any]
    plan_payload: Mapping[str, Any]
    langfuse_config: Mapping[str, Any]


class KGModelRuntime(Protocol):
    def invoke(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> Any:
        ...


class KGAgent:
    """Investigate one recall shortage with bounded read-only tool calls."""

    _JSON_RESPONSE_FORMAT = {"type": "json_object"}

    from .kg_prompts import RECALL_KG_PROMPT as _SYSTEM_PROMPT

    _EXTRACT_SYSTEM = (
        "你是知识图谱治理 Agent。只能输出 JSON，不得调用写接口。"
        "从输入 Query 和触发证据中提取需要核对的核心标签，格式为 "
        '{"core_tags":["标签"]}。'
    )
    _PLAN_SYSTEM = (
        "你是知识图谱治理 Agent。你只能使用 query_tag、query_relationship、"
        "query_dimension、search_products 和 structured_plan_submission 四类工具。"
        "图谱写入必须只作为方案输出，不能在本次调用中执行。"
        "必须基于 tool_evidence 中的真实搜索结果作判断；没有搜索证据时不得声称无需调整。"
        "decision 只能是 adjust、no_change、manual_review。"
        "只输出 JSON：{\"decision\":\"adjust|no_change|manual_review\",\"reason\":\"...\",\"actions\":[{"
        "\"actionType\":\"add_tag|update_tag|blacklist_tag|add_relationship|update_relationship|"
        "delete_relationship\","
        "\"targetInterface\":\"接口路径\",\"riskLevel\":\"low|medium|high\","
        "\"params\":{},\"reason\":\"...\"}]}。"
    )

    def __init__(self, *, runtime: KGModelRuntime, tools: KnowledgeGraphReadTools) -> None:
        self.runtime = runtime
        self.tools = tools
        self._graph = self._build_graph()

    def generate(
        self,
        *,
        brand: str,
        query_text: str,
        trigger_source: str,
        evidence: Mapping[str, Any] | None,
        langfuse_config: Mapping[str, Any] | None = None,
    ) -> KGAgentResult:
        if trigger_source not in KG_TRIGGER_SOURCES:
            return KGAgentResult(
                status=KGAgentStatus.ERROR,
                error_code="KG_AGENT_UNSUPPORTED_TRIGGER",
                error_message=f"unsupported trigger source: {trigger_source}",
            )
        try:
            query = transform_governance_payload_query(brand, query_text)
            if not query:
                raise KGAgentError("KG_AGENT_EMPTY_QUERY", "query is empty after preprocessing")
            # The full task identity remains in the durable task; the model sees
            # one query only and never receives Atlas's appended demographic.
            recall_evidence = {
                key: value for key, value in dict(evidence or {}).items()
                if key not in {"query", "query_text", "originalQuery", "analysisQuery", "gender", "queryTags", "segmented_tags", "segmentedTags"}
            }
            tool_calls: list[dict[str, Any]] = []

            def query_tag(*, tag_name: str, _agent_observation: Any | None = None) -> Mapping[str, Any]:
                return self.tools.query_tag(brand, tag_name, langfuse_config=langfuse_config, parent_observation=_agent_observation)

            def query_relationship(*, tag_name: str, side: str = "both", _agent_observation: Any | None = None) -> Mapping[str, Any]:
                return self.tools.query_relationship(brand, tag_name, side=side, langfuse_config=langfuse_config, parent_observation=_agent_observation)

            def query_dimension(*, dimension_name: str, _agent_observation: Any | None = None) -> Mapping[str, Any]:
                return self.tools.query_dimension(brand, dimension_name, langfuse_config=langfuse_config, parent_observation=_agent_observation)

            def search_products(*, query: str, _agent_observation: Any | None = None) -> Mapping[str, Any]:
                return self.tools.search_products(brand, query, langfuse_config=langfuse_config, parent_observation=_agent_observation)

            try:
                payload, tool_calls = run_read_only_agent(
                    runtime=self.runtime, system_prompt=self._SYSTEM_PROMPT,
                    user_input={"query": query, "recall": recall_evidence, "sceneId": self.tools.scene_id(brand)},
                    tools={"query_tag": query_tag, "query_relationship": query_relationship, "query_dimension": query_dimension, "search_products": search_products},
                    langfuse_config=langfuse_config,
                    observability=self.tools.observability,
                    agent_name="query-governance.kg-agent", max_tool_calls=24,
                    tool_observation_names=set(),
                    response_format=self._JSON_RESPONSE_FORMAT,
                )
            except AgentToolError as legacy_exc:
                # Compatibility for already-persisted tasks and local runtimes
                # using the former extract/context/plan contract. New runtime
                # prompts always use the bounded tool loop above.
                first_payload = getattr(legacy_exc, "payload", {})
                if not first_payload:
                    raise
                if isinstance(first_payload, Mapping) and "core_tags" in first_payload:
                    state = {
                        "brand": str(brand).strip(), "query_text": query,
                        "trigger_source": trigger_source, "evidence": dict(evidence or {}),
                        "langfuse_config": dict(langfuse_config or {}),
                    }
                    values = first_payload.get("core_tags")
                    if not isinstance(values, list):
                        raise KGAgentError("KG_AGENT_INVALID_TAGS", "core_tags must be an array")
                    segmented = (evidence or {}).get("segmented_tags") or (evidence or {}).get("segmentedTags") or []
                    tags = [str(value).strip() for value in [*values, *(segmented if isinstance(segmented, list) else [])] if str(value).strip() and str(value).strip() in query]
                    tags = list(dict.fromkeys(tags or ([query] if query else [])))
                    tags.sort(key=lambda value: (-len(value), query.find(value)))
                    state["core_tags"] = tags
                    state.update(self._collect_graph_context(state))
                    state.update(self._generate_plan(state))
                else:
                    state = self._graph.invoke({
                        "brand": str(brand).strip(), "query_text": query,
                        "trigger_source": trigger_source, "evidence": dict(evidence or {}),
                        "langfuse_config": dict(langfuse_config or {}),
                    })
                return KGAgentResult(status=KGAgentStatus.PLAN_GENERATED, plan=dict(state["plan_payload"]))
            decision = str(payload.get("decision") or "").strip()
            if decision not in {"adjust", "no_change", "manual_review"}:
                raise KGAgentError("KG_AGENT_INVALID_PLAN", "invalid decision")
            actions = normalize_kg_actions(payload.get("actions"))
            if decision != "adjust" and actions:
                raise KGAgentError("KG_AGENT_INVALID_PLAN", "non-adjust decision has actions")
            if decision == "adjust" and not actions:
                raise KGAgentError("KG_AGENT_INVALID_PLAN", "adjust decision has no actions")
            if not any(call["name"] == "search_products" for call in tool_calls):
                decision, actions = "manual_review", []
            if any("error" in call.get("result", {}) for call in tool_calls if isinstance(call.get("result"), Mapping)):
                decision, actions = "manual_review", []
            plan = {
                "brand": str(brand).strip(), "query_text": query,
                "trigger_source": trigger_source, "decision": decision,
                "reason": str(payload.get("reason") or "").strip(),
                "actions": actions, "tool_evidence": {"calls": tool_calls},
                "core_tags": list(dict.fromkeys(str(call.get("args", {}).get("tag_name") or "").strip() for call in tool_calls if call.get("name") in {"query_tag", "query_relationship"} and str(call.get("args", {}).get("tag_name") or "").strip())) or [query], "evidence": recall_evidence,
            }
            try:
                context = build_graph_context(tool_calls)
                if actions:
                    validate_actions(actions, context, self.tools.scene_id(brand))
                plan["graph_context"] = context
                plan["graph_context_digest"] = graph_digest(context)
            except ValueError as exc:
                plan["decision"], plan["actions"] = "manual_review", []
                plan["reason"] = f"{exc}，需要人工复核"
            return KGAgentResult(status=KGAgentStatus.PLAN_GENERATED, plan=plan)
        except KGAgentError as exc:
            return KGAgentResult(
                status=KGAgentStatus.ERROR,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        except AgentToolError as exc:
            return KGAgentResult(
                status=KGAgentStatus.ERROR,
                error_code="KG_AGENT_INVALID_JSON",
                error_message=str(exc),
            )
        except TimeoutError as exc:
            return KGAgentResult(
                status=KGAgentStatus.ERROR,
                error_code="KG_AGENT_MODEL_FAILED",
                error_message=str(exc),
            )
        except Exception as exc:
            return KGAgentResult(
                status=KGAgentStatus.ERROR,
                error_code="KG_AGENT_FAILED",
                error_message=str(exc).strip() or exc.__class__.__name__,
            )

    def _build_graph(self):
        from langgraph.graph import END, StateGraph

        graph = StateGraph(_KGState)
        graph.add_node("extract_core_tags", self._extract_core_tags)
        graph.add_node("collect_graph_context", self._collect_graph_context)
        graph.add_node("generate_plan", self._generate_plan)
        graph.set_entry_point("extract_core_tags")
        graph.add_edge("extract_core_tags", "collect_graph_context")
        graph.add_edge("collect_graph_context", "generate_plan")
        graph.add_edge("generate_plan", END)
        return graph.compile()

    def _extract_core_tags(self, state: _KGState) -> dict[str, Any]:
        request = {
            "query": state["query_text"],
            "recall": {
                key: value for key, value in (state.get("evidence") or {}).items()
                if key not in {"query", "query_text", "originalQuery", "analysisQuery", "gender", "queryTags", "segmented_tags", "segmentedTags"}
            },
        }
        payload = self._invoke_json(
            self._EXTRACT_SYSTEM,
            json.dumps(request, ensure_ascii=False, sort_keys=True),
            langfuse_config=state.get("langfuse_config"),
        )
        values = payload.get("core_tags", payload.get("coreTags"))
        if not isinstance(values, list):
            raise KGAgentError("KG_AGENT_INVALID_TAGS", "core_tags must be an array")
        raw_tags = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        evidence = state.get("evidence") or {}
        segmented = evidence.get("segmented_tags") or evidence.get("segmentedTags")
        candidates = raw_tags + (list(segmented) if isinstance(segmented, list) else [])
        query_text = state["query_text"]
        # A tag proposed by the model is admissible only when it is an exact
        # contiguous substring of the original Query. Existing segmentation
        # tags are included as the deterministic fallback; no cross-field or
        # invented term can enter the tool chain.
        tags = list(dict.fromkeys(
            str(value).strip() for value in candidates
            if str(value).strip() and str(value).strip() in query_text
        ))
        tags.sort(key=lambda value: (-len(value), query_text.find(value)))
        if not tags and state["query_text"]:
            tags = [state["query_text"]]
        if not tags:
            raise KGAgentError("KG_AGENT_INVALID_TAGS", "no core tags were returned")
        return {"core_tags": tags}

    def _collect_graph_context(self, state: _KGState) -> dict[str, Any]:
        return {
            "graph_context": self.tools.collect_context(
                state["brand"],
                state["core_tags"],
                langfuse_config=state.get("langfuse_config"),
            )
        }

    def _generate_plan(self, state: _KGState) -> dict[str, Any]:
        request = {
            "query": state["query_text"],
            "recall": {
                key: value for key, value in (state.get("evidence") or {}).items()
                if key not in {"query", "query_text", "originalQuery", "analysisQuery", "gender", "queryTags", "segmented_tags", "segmentedTags"}
            },
            "core_tags": state["core_tags"],
            "graph_context": state["graph_context"],
        }
        payload = self._invoke_json(
            self._PLAN_SYSTEM,
            json.dumps(request, ensure_ascii=False, sort_keys=True),
            langfuse_config=state.get("langfuse_config"),
        )
        actions = payload.get("actions")
        if not isinstance(actions, list):
            raise KGAgentError("KG_AGENT_INVALID_PLAN", "actions must be an array")
        normalized_actions = []
        for index, action in enumerate(actions):
            if not isinstance(action, Mapping):
                raise KGAgentError("KG_AGENT_INVALID_PLAN", f"action {index} must be an object")
            action_type = str(action.get("actionType") or action.get("action_type") or "")
            if action_type not in KG_PLAN_ACTIONS:
                raise KGAgentError(
                    "KG_AGENT_INVALID_PLAN", f"unsupported action type: {action_type}"
                )
            params = action.get("params")
            reason = str(action.get("reason") or "").strip()
            if not isinstance(params, Mapping) or not reason:
                raise KGAgentError(
                    "KG_AGENT_INVALID_PLAN",
                    f"action {index} requires params and reason",
                )
            risk = str(action.get("riskLevel") or action.get("risk_level") or "medium").lower()
            if risk not in {"low", "medium", "high"}:
                raise KGAgentError("KG_AGENT_INVALID_PLAN", f"invalid risk level: {risk}")
            normalized_actions.append(
                {
                    "actionType": action_type,
                    # The model may describe an endpoint, but the executable
                    # boundary is always derived from the validated action.
                    "targetInterface": KG_ACTION_ENDPOINTS[action_type],
                    "riskLevel": risk,
                    "params": dict(params),
                    "reason": reason,
                }
            )
        tool_evidence = state["graph_context"]
        decision = str(payload.get("decision") or ("adjust" if normalized_actions else "no_change")).strip().lower()
        if decision not in {"adjust", "no_change", "manual_review"}:
            raise KGAgentError("KG_AGENT_INVALID_PLAN", f"unsupported decision: {decision}")
        graph_errors = tool_evidence.get("errors") if isinstance(tool_evidence, Mapping) else None
        if graph_errors:
            decision = "manual_review"
            normalized_actions = []
        reason = str(payload.get("reason") or "").strip()
        if decision == "manual_review" and graph_errors:
            first_error = next((item for item in graph_errors if isinstance(item, Mapping)), {})
            evidence_reason = str(first_error.get("message") or "工具证据不完整")
            reason = f"{evidence_reason}，需要人工复核"
        if decision == "manual_review" and not reason:
            reason = "工具证据不完整，需要人工复核"
        if decision == "no_change" and normalized_actions:
            raise KGAgentError("KG_AGENT_INVALID_PLAN", "no_change decision cannot contain actions")
        plan_payload = {
            "brand": state["brand"],
            "query_text": state["query_text"],
            "trigger_source": state["trigger_source"],
            "core_tags": state["core_tags"],
            "evidence": dict(state.get("evidence") or {}),
            "graph_context": state["graph_context"],
            "decision": decision,
            "reason": reason,
            "actions": normalized_actions,
            "tool_evidence": tool_evidence,
        }
        return {"plan_payload": plan_payload}

    def _invoke_json(
        self,
        system_prompt: str,
        user_content: str,
        *,
        langfuse_config: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            invoke_kwargs = {"response_format": dict(self._JSON_RESPONSE_FORMAT)}
            if langfuse_config:
                invoke_kwargs = {**dict(langfuse_config), **invoke_kwargs}
            result = self.runtime.invoke(messages, **invoke_kwargs)
        except Exception as exc:
            raise KGAgentError(
                "KG_AGENT_MODEL_FAILED", str(exc).strip() or exc.__class__.__name__
            ) from exc
        response = getattr(result, "response", result)
        content = getattr(response, "content", response)
        if not isinstance(content, str):
            raise KGAgentError("KG_AGENT_INVALID_JSON", "model response content must be text")
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise KGAgentError("KG_AGENT_INVALID_JSON", "model response is not valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise KGAgentError("KG_AGENT_INVALID_JSON", "model response must be a JSON object")
        return payload


class QueryGovernanceActionPlanRepository:
    """Persist one auditable KG proposal in the rewrite-owned action table."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        model: type[Any] = QueryGovernanceActionPlan,
    ) -> None:
        self._session_factory = session_factory
        self._model = model

    def create_or_get(
        self, task: AsyncTaskRecord, plan: Mapping[str, Any]
    ) -> int:
        idempotency_key = f"{task.idempotency_key}:plan"
        persisted_plan = _compact_action_plan(plan)
        with self._session_factory() as session:
            existing = session.execute(
                select(self._model).where(self._model.idempotency_key == idempotency_key)
            ).scalar_one_or_none()
            if existing is not None:
                if existing.task_id != task.task_id:
                    raise ValueError("KG action plan idempotency key collision")
                return int(existing.id)
            row = self._model(
                task_id=task.task_id,
                batch_id=task.batch_id,
                brand=task.brand,
                query_text=task.query_text,
                node_key=(
                    "query_governance.relevance_governance"
                    if task.trigger_source == "relevance_root_cause"
                    else "query_governance.kg_agent"
                ),
                action_type=(
                    "relevance_plan"
                    if task.trigger_source == "relevance_root_cause"
                    else "kg_plan"
                ),
                idempotency_key=idempotency_key,
                status=GovernancePlanStatus.PENDING.value,
                request_json=json.dumps(
                    {
                        "trigger_source": task.trigger_source,
                        "execution_mode": plan.get("executionMode", "human_approval"),
                        "evidence": _compact_plan_evidence(task.evidence),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                response_json=json.dumps(
                    persisted_plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.execute(
                    select(self._model).where(self._model.idempotency_key == idempotency_key)
                ).scalar_one_or_none()
                if existing is None:
                    raise
                return int(existing.id)
            session.refresh(row)
            return int(row.id)

    def record_error(
        self,
        plan_id: int,
        error_code: str,
        error_message: str,
        *,
        executor_type: GovernancePlanExecutorType = GovernancePlanExecutorType.AI_AUTO,
        executor_user_id: str | None = None,
        failure_stage: GovernancePlanFailureStage = GovernancePlanFailureStage.GENERATION,
    ) -> None:
        """Mark a persisted proposal as failed with its actual failure provenance."""
        if not str(error_code).strip() or not str(error_message).strip():
            raise ValueError("error code and message are required")
        with self._session_factory() as session:
            row = session.get(self._model, plan_id)
            if row is None:
                raise KeyError(f"KG action plan not found: {plan_id}")
            row.status = GovernancePlanStatus.FAILED.value
            row.executor_type = executor_type.value
            row.executor_user_id = executor_user_id
            row.failure_stage = failure_stage.value
            row.error_code = str(error_code).strip()
            row.error_message = str(error_message).strip()
            session.commit()

    def get(self, plan_id: int) -> Any | None:
        with self._session_factory() as session:
            row = session.get(self._model, plan_id)
            if row is None:
                return None
            # Detached rows are safe for this small execution boundary.
            session.expunge(row)
            return row

    def update_execution(
        self,
        plan_id: int,
        *,
        status: GovernancePlanStatus,
        response: Mapping[str, Any],
        executor_type: GovernancePlanExecutorType = GovernancePlanExecutorType.HUMAN,
        executor_user_id: str | None = None,
        failure_stage: GovernancePlanFailureStage | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            row = session.get(self._model, plan_id)
            if row is None:
                raise KeyError(f"KG action plan not found: {plan_id}")
            row.status = status.value
            row.response_json = _canonical_json(response)
            row.executor_type = executor_type.value
            row.executor_user_id = executor_user_id
            row.failure_stage = failure_stage.value if failure_stage else None
            row.error_code = error_code
            row.error_message = error_message
            if status is GovernancePlanStatus.VERIFIED:
                row.executed_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            session.commit()

    def claim(self, plan_id: int) -> tuple[Mapping[str, Any], bool]:
        """Atomically claim a pending plan; already terminal plans are idempotent."""
        with self._session_factory() as session:
            row = session.get(self._model, plan_id)
            if row is None:
                raise KeyError(f"KG action plan not found: {plan_id}")
            payload = _json_object(row.response_json) or {}
            status = str(row.status or "pending")
            if status == GovernancePlanStatus.VERIFIED.value:
                return payload, False
            if status != GovernancePlanStatus.PENDING.value:
                raise ValueError(f"KG plan cannot execute from status: {status}")
            claimed = session.execute(
                update(self._model)
                .where(
                    self._model.id == plan_id,
                    self._model.status == GovernancePlanStatus.PENDING.value,
                )
                .values(status=GovernancePlanStatus.EXECUTING.value)
            ).rowcount
            if claimed != 1:
                session.rollback()
                raise ValueError("KG plan is already executing")
            session.commit()
            return payload, True


def _json_object(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


class KGPlanExecutor:
    """Human-approved, idempotent KG write path with conflict and readback gates."""

    def __init__(
        self,
        *,
        client: KnowledgeGraphClientProtocol,
        tools: KnowledgeGraphReadTools,
        action_plan_repository: QueryGovernanceActionPlanRepository,
    ) -> None:
        self.client = client
        self.tools = tools
        self.action_plan_repository = action_plan_repository

    def execute(self, plan_id: int, *, executor_user_id: str | None = None) -> dict[str, Any]:
        payload, claimed = self.action_plan_repository.claim(plan_id)
        if not claimed:
            return {"status": "verified", "plan_id": plan_id, "idempotent": True}
        plan = payload.get("plan") if isinstance(payload.get("plan"), Mapping) else payload
        if not isinstance(plan, Mapping):
            self.action_plan_repository.record_error(plan_id, "KG_PLAN_INVALID", "KG 方案内容无效", executor_user_id=executor_user_id, failure_stage=GovernancePlanFailureStage.EXECUTION)
            raise KGAgentError("KG_PLAN_INVALID", "KG 方案内容无效")
        try:
            KGPlanExecutionGuard(
                checker=KGPlanConflictChecker(self.tools),
                action_plan_repository=self.action_plan_repository,
            ).assert_current(plan_id, plan, executor_user_id=executor_user_id)
            steps: list[dict[str, Any]] = []
            for index, proposed in enumerate(plan.get("actions") or [], 1):
                if not isinstance(proposed, Mapping):
                    raise KGAgentError("KG_PLAN_INVALID", f"第 {index} 个动作无效")
                action = {
                    "brand": plan.get("brand"),
                    "actionType": proposed.get("actionType") or proposed.get("action_type"),
                    "params": dict(proposed.get("params") or {}),
                }
                if action["actionType"] not in KG_PLAN_ACTIONS:
                    raise KGAgentError("KG_PLAN_INVALID", f"不支持的动作类型: {action['actionType']}")
                snapshot_fn = getattr(self.client, "snapshot", None)
                snapshot = snapshot_fn(action) if callable(snapshot_fn) else None
                response = self.client.execute(action)
                if not isinstance(response, Mapping) or response.get("success") is False or response.get("code") in {0, "0", -1, "-1"}:
                    raise KGAgentError("KG_PLAN_EXECUTION_FAILED", f"第 {index} 个 KG 动作执行失败")
                steps.append({"index": index, "action": action, "status": "verified", "response": dict(response), "snapshot": snapshot})
            # A second readback is mandatory after writes. If the external
            # service does not mutate the queried state yet, the checker still
            # protects against any concurrent graph change.
            verification_context = self.tools.collect_context(
                str(plan.get("brand") or ""), list(plan.get("core_tags") or [])
            )
            execution = {"status": "verified", "steps": steps, "verification": verification_context}
            self.action_plan_repository.update_execution(
                plan_id,
                status=GovernancePlanStatus.VERIFIED,
                response={"plan": dict(plan), "execution": execution},
                executor_user_id=executor_user_id,
            )
            return {"status": "verified", "plan_id": plan_id, "execution": execution}
        except KGPlanConflictError:
            raise
        except Exception as exc:
            code = str(getattr(exc, "error_code", None) or "KG_PLAN_EXECUTION_FAILED")
            message = str(exc).strip() or exc.__class__.__name__
            self.action_plan_repository.record_error(
                plan_id,
                code,
                message,
                executor_type=GovernancePlanExecutorType.HUMAN,
                executor_user_id=executor_user_id,
                failure_stage=GovernancePlanFailureStage.EXECUTION,
            )
            raise


class KGPlanConflictChecker:
    """Re-read the graph before an operator executes a generated proposal."""

    def __init__(self, tools: KnowledgeGraphReadTools) -> None:
        self.tools = tools

    def assert_current(self, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        brand = str(plan.get("brand") or "").strip()
        core_tags = plan.get("core_tags")
        expected = plan.get("graph_context")
        expected_digest = str(plan.get("graph_context_digest") or "").strip()
        if not brand or not isinstance(core_tags, list) or (not isinstance(expected, Mapping) and not expected_digest):
            raise KGPlanConflictError("KG_PLAN_CONFLICT: plan graph evidence is incomplete")
        if isinstance(expected, Mapping) and expected.get("version") == 2:
            calls = []
            for read in expected.get("reads", []):
                method = getattr(self.tools, read["name"])
                calls.append({**read, "result": method(brand, **read["args"])})
            current = build_graph_context(calls)
            if not current["reads"] or graph_digest(current) != expected_digest:
                raise KGPlanConflictError()
            validate_actions(plan.get("actions") or [], current, self.tools.scene_id(brand))
            return current
        current = self.tools.collect_context(brand, core_tags)
        if expected_digest:
            current_digest = hashlib.sha256(_canonical_json(current).encode("utf-8")).hexdigest()
            if current_digest != expected_digest:
                raise KGPlanConflictError()
        elif _canonical_json(current) != _canonical_json(expected):
            raise KGPlanConflictError()
        return current


class KGPlanExecutionGuard:
    """Keep stale KG proposals from reaching the human execution path."""

    def __init__(
        self,
        *,
        checker: KGPlanConflictChecker,
        action_plan_repository: QueryGovernanceActionPlanRepository,
    ) -> None:
        self.checker = checker
        self.action_plan_repository = action_plan_repository

    def assert_current(
        self,
        plan_id: int,
        plan: Mapping[str, Any],
        *,
        executor_user_id: str | None = None,
    ) -> Mapping[str, Any]:
        try:
            return self.checker.assert_current(plan)
        except KGPlanConflictError as exc:
            self.action_plan_repository.record_error(
                plan_id,
                exc.error_code,
                str(exc),
                executor_type=GovernancePlanExecutorType.HUMAN,
                executor_user_id=executor_user_id,
                failure_stage=GovernancePlanFailureStage.VERIFICATION,
            )
            raise


class QueryGovernanceKGWorker:
    """Consume persisted KG tasks without ever blocking the mainline."""

    def __init__(
        self,
        *,
        async_service: QueryGovernanceAsyncTaskService,
        agent: KGAgent,
        relevance_root_cause_agent: RelevanceRootCauseAgent | None = None,
        agent_execution_modes: Mapping[str, str] | None = None,
        kg_action_executor: QueryGovernanceKGActionExecutor | None = None,
        kg_execution_modes: Mapping[str, str] | None = None,
        action_plan_repository: QueryGovernanceActionPlanRepository,
        observability: QueryGovernanceObservability | None = None,
    ) -> None:
        self.async_service = async_service
        self.agent = agent
        self.relevance_root_cause_agent = relevance_root_cause_agent
        self.agent_execution_modes = dict(agent_execution_modes or {})
        self.kg_action_executor = kg_action_executor
        self.kg_execution_modes = {
            str(key): str(value)
            for key, value in (kg_execution_modes or {}).items()
        }
        self.action_plan_repository = action_plan_repository
        self.observability = observability

    def process(
        self,
        task_id: int,
        *,
        langfuse_config: Mapping[str, Any] | None = None,
    ) -> KGAgentResult:
        record = self.async_service.get(task_id)
        if record is None:
            return KGAgentResult(
                status=KGAgentStatus.ERROR,
                error_code="KG_TASK_NOT_FOUND",
                error_message=f"async KG task not found: {task_id}",
            )
        if record.status is AsyncTaskStatus.PLAN_GENERATED:
            return KGAgentResult(status=KGAgentStatus.PLAN_GENERATED, plan=record.plan)
        if record.status is AsyncTaskStatus.ERROR:
            return KGAgentResult(
                status=KGAgentStatus.ERROR,
                plan=record.plan,
                error_code=record.error_code,
                error_message=record.error_message,
            )
        try:
            if record.status is not AsyncTaskStatus.PENDING:
                raise KGAgentError(
                    "KG_TASK_INVALID_STATE",
                    f"cannot process task in {record.status.value} state",
                )
            self.async_service.mark_processing(task_id)
            generate_kwargs: dict[str, Any] = {
                "brand": record.brand,
                "query_text": record.query_text,
                "trigger_source": record.trigger_source,
                "evidence": record.evidence,
            }
            if langfuse_config is None and record.langfuse_context:
                observability = self.observability
                if observability is None:
                    observability = QueryGovernanceObservability.from_environment()
                resume = getattr(observability, "resume_query_config", None)
                if callable(resume):
                    try:
                        langfuse_config = resume(
                            record.langfuse_context,
                            node="relevance_governance_agent" if record.trigger_source == "relevance_root_cause" else "kg_agent",
                        )
                    except Exception:
                        # Observability is a sidecar; a resume failure must not
                        # prevent the KG plan from being generated.
                        langfuse_config = None
            if langfuse_config is not None:
                generate_kwargs["langfuse_config"] = langfuse_config
            try:
                if (
                    record.trigger_source == "relevance_root_cause"
                    and self.relevance_root_cause_agent is not None
                ):
                    remediation = self._run_analysis_with_retry(
                        lambda: self.relevance_root_cause_agent.analyze(
                            brand=record.brand,
                            query_text=record.query_text,
                            evidence=record.evidence,
                            langfuse_config=langfuse_config,
                        ),
                        task_id=task_id,
                    )
                    # The relevance agent is a separate owner. It returns its own
                    # complete KG parameters when the root cause is KG-related;
                    # this branch must never call KGAgent.generate.
                    groups = []
                    for raw_group in remediation.get("causeGroups") or []:
                        group = dict(raw_group) if isinstance(raw_group, Mapping) else {}
                        actions = [
                            {**dict(action), "causeGroupId": group.get("groupId")}
                            for action in group.get("actions") or []
                            if isinstance(action, Mapping)
                            and str(action.get("actionType") or action.get("action_type") or "") != "product_blacklist"
                        ]
                        group["actions"] = actions
                        group["executionMode"] = self._execution_mode_for_actions(actions, check_key="relevance")
                        group["status"] = GovernancePlanStatus.PENDING.value
                        group["executable"] = bool(actions) and group["executionMode"] != "disabled"
                        groups.append(group)
                    result = KGAgentResult(
                        status=KGAgentStatus.PLAN_GENERATED,
                        plan={
                            "brand": record.brand,
                            "query_text": record.query_text,
                            "trigger_source": record.trigger_source,
                            "causeGroups": groups,
                            "queryTags": remediation["queryTags"],
                            "kgContext": remediation["kgContext"],
                            "graph_context": remediation.get("graph_context") or {},
                            "tool_evidence": remediation.get("tool_evidence") or {},
                            "warnings": remediation.get("warnings") or [],
                            "reason": "；".join(str(group.get("conclusion") or "").strip() for group in groups if group.get("conclusion")),
                            "actions": [action for group in groups for action in group.get("actions") or []],
                        },
                    )
                else:
                    result = self._run_analysis_with_retry(
                        lambda: self.agent.generate(**generate_kwargs),
                        task_id=task_id,
                    )
            except KGAgentError:
                raise
            except Exception as exc:
                error_code = str(getattr(exc, "error_code", None) or "KG_AGENT_ANALYSIS_FAILED")
                detail = str(exc).strip() or exc.__class__.__name__
                agent_label = (
                    "相关性治理 Agent"
                    if record.trigger_source == "relevance_root_cause"
                    else "KG Agent"
                )
                error_message = f"{agent_label} 分析失败：{detail}"
                logger.exception("KG Agent analysis failed for async task %s", task_id)
                try:
                    self.async_service.record_error(task_id, error_code, error_message)
                except Exception:
                    pass
                return KGAgentResult(
                    status=KGAgentStatus.ERROR,
                    error_code=error_code,
                    error_message=error_message,
                )
            if result.status is KGAgentStatus.ERROR:
                self.async_service.record_error(
                    task_id,
                    result.error_code or "KG_AGENT_FAILED",
                    result.error_message or "KG Agent failed",
                )
                return result
            assert result.plan is not None
            check_key = "relevance" if record.trigger_source.startswith("relevance") else "recall"
            groups = result.plan.get("causeGroups") or result.plan.get("cause_groups") or []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                actions = group.get("actions") or group.get("steps") or []
                group["executionMode"] = self._execution_mode_for_actions(actions, check_key=check_key)
                group["executable"] = bool(actions) and group["executionMode"] != "disabled"
            result.plan["executionMode"] = self._execution_mode_for_actions(
                result.plan.get("actions") or [], check_key=check_key,
            )
            # A purely individual/uncertain result is a valid terminal
            # analysis, not an empty KG action that could block daily retag.
            has_kg_group = any(
                isinstance(group, Mapping)
                and group.get("rootCause") == "kg_issue"
                and bool(group.get("actions") or group.get("steps"))
                for group in (result.plan.get("causeGroups") or [])
            )
            plan_id = None
            persisted_plan = _compact_action_plan(result.plan)
            if record.trigger_source != "relevance_root_cause" or has_kg_group:
                plan_id = self.action_plan_repository.create_or_get(record, persisted_plan)
            self.async_service.record_plan(task_id, persisted_plan)
            if plan_id is not None and self.kg_action_executor is not None:
                groups = result.plan.get("causeGroups") or result.plan.get("cause_groups") or []
                group_ids = [
                    str(group.get("groupId") or group.get("group_id") or "").strip()
                    for group in groups
                    if isinstance(group, Mapping)
                    and str(group.get("rootCause") or group.get("root_cause") or "") == "kg_issue"
                ]
                if not group_ids and result.plan.get("actions"):
                    group_ids = [""]
                for group_id in group_ids:
                    group = next(
                        (
                            item
                            for item in groups
                            if isinstance(item, Mapping)
                            and str(item.get("groupId") or item.get("group_id") or "").strip() == group_id
                        ),
                        {},
                    )
                    actions = group.get("actions") or group.get("steps") or result.plan.get("actions") or []
                    if self._execution_mode_for_actions(actions, check_key=check_key) != "auto":
                        continue
                    self.kg_action_executor.execute(
                        plan_id,
                        group_id=group_id or None,
                        executor_type=GovernancePlanExecutorType.AI_AUTO.value,
                    )
            return result
        except KGAgentError as exc:
            self.async_service.record_error(task_id, exc.error_code, exc.message)
            return KGAgentResult(
                status=KGAgentStatus.ERROR,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        except Exception as exc:
            error_code = "KG_AGENT_PERSIST_FAILED"
            logger.exception("KG Agent plan persistence failed for async task %s", task_id)
            message = "KG Agent 方案保存失败"
            try:
                self.async_service.record_error(task_id, error_code, message)
            except Exception:
                pass
            return KGAgentResult(
                status=KGAgentStatus.ERROR,
                error_code=error_code,
                error_message=message,
            )

    def process_pending(self, *, limit: int = 100) -> list[KGAgentResult]:
        return [self.process(record.id) for record in self.async_service.list_pending(limit=limit)]

    @staticmethod
    def _run_analysis_with_retry(callback: Callable[[], Any], *, task_id: int) -> Any:
        """Retry one read-only analysis attempt when the model/tool path flakes."""
        for attempt in range(2):
            try:
                return callback()
            except KGAgentError:
                raise
            except Exception as exc:
                retryable = isinstance(exc, (AgentToolError, ConnectionError, OSError, TimeoutError))
                if attempt == 0 and retryable:
                    logger.warning(
                        "KG Agent transient analysis failure for async task %s; retrying once: %s",
                        task_id,
                        str(exc).strip() or exc.__class__.__name__,
                    )
                    continue
                raise

    def _execution_mode_for_actions(
        self, actions: Sequence[Any], *, check_key: str = "recall",
    ) -> str:
        action_types = {
            str(item.get("actionType") or item.get("action_type") or "")
            for item in actions if isinstance(item, Mapping)
        }
        tool_keys = {
            "kg_relation" if action_type in {
                "add_relationship", "update_relationship", "delete_relationship",
            } else "kg_tag"
            for action_type in action_types
        } or {"kg_tag"}
        return strictest_execution_mode(
            self.agent_execution_modes.get(check_key, "auto"),
            *(self.kg_execution_modes.get(key, "human_approval") for key in tool_keys),
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "KGAgent",
    "KGAgentError",
    "KGAgentResult",
    "KGAgentStatus",
    "KGPlanConflictChecker",
    "KGPlanConflictError",
    "KGPlanExecutionGuard",
    "KGPlanExecutor",
    "KnowledgeGraphReadTools",
    "QueryGovernanceActionPlanRepository",
    "QueryGovernanceKGWorker",
]
