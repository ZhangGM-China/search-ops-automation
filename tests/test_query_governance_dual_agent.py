"""The two new agent paths share only read-only tools and execution contracts."""

from __future__ import annotations

import json

import pytest

from src.query_governance.kg_action_contract import normalize_kg_action
from src.query_governance.kg_agent import KGAgent, KGAgentStatus, KnowledgeGraphReadTools
from src.query_governance.relevance_remediation import RelevanceRootCauseAgent
from src.query_governance.langfuse import QueryGovernanceObservability


class Runtime:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def invoke(self, messages, **_kwargs):
        self.messages.append(list(messages))
        return self.responses.pop(0)


class KGClient:
    def execute(self, action):
        if action["actionType"] == "query_tag":
            return {"data": [{"id": "tag-1", "name": "薄开衫", "sceneId": "scene-1", "dimensions": ["dimension-1"]}]}
        if action["actionType"] == "query_relationship":
            return {"data": [{"id": "rel-1", "tagName": "薄开衫", "responseTagName": "开衫"}]}
        return {"data": []}


class Search:
    def mixed_with_total(self, _brand, query, **_kwargs):
        return {"success": True, "total_count": 20, "products": [{"productId": "p1", "name": query}]}


class Observation:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.children = []
        self.updates = []

    def start_observation(self, **kwargs):
        child = Observation(**kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def end(self):
        return None


class TraceClient:
    def __init__(self):
        self.observations = []

    def create_trace_id(self, seed=None):
        return "trace-dual-agent"

    def start_observation(self, **kwargs):
        observation = Observation(**kwargs)
        self.observations.append(observation)
        return observation


def test_recall_agent_uses_only_preprocessed_query_and_can_continue_after_tool_result():
    runtime = Runtime([
        '{"toolCall":{"name":"query_tag","args":{"tag_name":"薄开衫"}}}',
        '{"toolCall":{"name":"search_products","args":{"query":"薄开衫"}}}',
        '{"decision":"no_change","reason":"现有标签与召回证据未支持更改","actions":[]}',
    ])
    agent = KGAgent(runtime=runtime, tools=KnowledgeGraphReadTools(KGClient(), search=Search()))
    result = agent.generate(
        brand="atlas", query_text="薄开衫 女士", trigger_source="recall_anomaly",
        evidence={"result_count": 20, "originalQuery": "薄开衫 女士", "gender": "女士"},
    )
    assert result.status is KGAgentStatus.PLAN_GENERATED
    assert result.plan["query_text"] == "薄开衫"
    assert len(result.plan["tool_evidence"]["calls"]) == 2
    assert "女士" not in json.dumps(runtime.messages, ensure_ascii=False)


def test_kg_action_contract_rejects_missing_fields_and_preserves_relationship_direction():
    with pytest.raises(ValueError, match="sceneId"):
        normalize_kg_action({"actionType": "add_relationship", "params": {"tagName": "薄开衫"}})
    action = normalize_kg_action({"actionType": "add_relationship", "params": {
        "sceneId": "scene-1", "relationships": "902227092548485120",
        "tagId": "child-id", "tagName": "薄开衫", "dimensionNames": "服装",
        "responseTagId": "parent-id", "responseTagName": "开衫",
    }})
    assert action["params"]["tagId"] == "child-id"
    assert action["targetInterface"] == "/admin/tagRelationship/addRelationship"


def test_kg_action_contract_supports_blacklist_tag_with_explicit_tag_identity():
    action = normalize_kg_action({"actionType": "blacklist_tag", "params": {
        "id": "tag-node-1", "sceneId": "scene-1", "tagId": "tag-1", "isBlack": 1,
    }})
    assert action["targetInterface"] == "/admin/tag/updateTagInfo"
    assert action["params"]["isBlack"] == 1


def test_query_relationship_reads_both_endpoints_and_marks_search_failure():
    class RelationshipClient:
        def __init__(self):
            self.actions = []

        def execute(self, action):
            self.actions.append(action)
            params = action["params"]
            if params.get("tagNameAcc") == "半身裙":
                return {"data": [{"id": "r1", "tagName": "半身裙", "responseTagName": "裙子"}]}
            return {"data": [{"id": "r2", "tagName": "连衣裙", "responseTagName": "半身裙"}]}

    client = RelationshipClient()
    tools = KnowledgeGraphReadTools(client, search=Search())
    result = tools.query_relationship("atlas", "半身裙", side="both")
    assert {row["id"] for row in result["data"]} == {"r1", "r2"}
    assert {call["params"].get("tagNameAcc") for call in client.actions} == {"半身裙", None}

    class FailedSearch:
        def mixed_with_total(self, *_args, **_kwargs):
            return {"success": False, "total_count": 0, "products": []}

    failed = KnowledgeGraphReadTools(client, search=FailedSearch()).search_products("atlas", "半身裙")
    assert failed["success"] is False
    assert failed["error"]["code"] == "KG_SEARCH_FAILED"


def test_relevance_agent_fetches_current_product_tags_and_owns_kg_actions():
    class ProductDetails:
        def fetch(self, _brand, ids):
            return {ids[0]: {"productId": ids[0], "name": "男士针织衫", "tags": ["男装", "针织"], "gender": "男"}}

    runtime = Runtime([
        '{"toolCall":{"name":"product_details","args":{}}}',
        '{"toolCall":{"name":"query_relationship","args":{"tag_name":"薄开衫"}}}',
        '{"groups":[{"groupId":"kg:wide","rootCause":"kg_issue","causeKey":"wide","productIds":["p1"],"actions":[{"actionType":"delete_relationship","params":{"id":"rel-1"}}]}]}',
    ])
    agent = RelevanceRootCauseAgent(
        runtime=runtime, kg_tools=KnowledgeGraphReadTools(KGClient(), search=Search()),
        product_details=ProductDetails(),
    )
    result = agent.analyze(
        brand="atlas", query_text="薄开衫 女士",
        evidence={"blacklist_candidates": [{"product_id": "p1", "reason": "性别不符"}]},
    )
    assert result["query"] == "薄开衫"
    assert result["causeGroups"][0]["actions"][0]["params"] == {"id": "rel-1"}
    assert "男装" in json.dumps(runtime.messages, ensure_ascii=False)
    assert "女士" not in json.dumps(runtime.messages, ensure_ascii=False)


def test_relevance_agent_keeps_group_auditable_when_kg_action_is_malformed():
    class ProductDetails:
        def fetch(self, _brand, ids):
            return {ids[0]: {"productId": ids[0], "name": "商品"}}

    runtime = Runtime([
        '{"toolCall":{"name":"product_details","args":{}}}',
        '{"groups":[{"groupId":"kg:invalid","rootCause":"kg_issue","causeKey":"wide","productIds":["p1"],"actions":[{"actionType":""}]}]}',
        '{"actions":[{"actionType":""}]}',
    ])
    agent = RelevanceRootCauseAgent(
        runtime=runtime,
        kg_tools=KnowledgeGraphReadTools(KGClient(), search=Search()),
        product_details=ProductDetails(),
    )

    result = agent.analyze(
        brand="atlas",
        query_text="薄开衫",
        evidence={"blacklist_candidates": [{"product_id": "p1", "reason": "错误召回"}]},
    )

    assert result["causeGroups"][0]["rootCause"] == "kg_issue"
    assert result["causeGroups"][0]["actions"] == []
    assert result["warnings"] == ["KG 动作格式无效（wide）：unsupported KG action:"]


def test_relevance_agent_only_analyzes_individual_root_cause_without_blacklist_action():
    class ProductDetails:
        def fetch(self, _brand, ids):
            return {ids[0]: {"productId": ids[0], "name": "男士针织衫", "tags": ["男装"]}}

    runtime = Runtime([
        '{"toolCall":{"name":"product_details","args":{}}}',
        '{"groups":[{"rootCause":"individual","causeKey":"wrong-product","productIds":["p1"]}]}',
    ])
    agent = RelevanceRootCauseAgent(
        runtime=runtime,
        kg_tools=KnowledgeGraphReadTools(KGClient(), search=Search()),
        product_details=ProductDetails(),
    )
    result = agent.analyze(
        brand="atlas", query_text="薄开衫 女士",
        evidence={"blacklist_candidates": [{"product_id": "p1", "product_name": "男士针织衫", "reason": "性别不符"}]},
    )
    assert result["causeGroups"][0]["actions"] == []
    assert result["causeGroups"][0]["productIds"] == ["p1"]


def test_relevance_agent_keeps_generating_when_some_product_details_are_missing():
    class PartialProductDetails:
        def fetch(self, _brand, ids):
            return {ids[0]: {"productId": ids[0], "name": "男士针织衫"}}

    runtime = Runtime([
        '{"toolCall":{"name":"product_details","args":{}}}',
        '{"groups":[{"rootCause":"individual","productIds":["p1"]}]}',
    ])
    agent = RelevanceRootCauseAgent(
        runtime=runtime,
        kg_tools=KnowledgeGraphReadTools(KGClient(), search=Search()),
        product_details=PartialProductDetails(),
    )

    result = agent.analyze(
        brand="atlas",
        query_text="薄开衫 女士",
        evidence={
            "blacklist_candidates": [
                {"product_id": "p1", "product_name": "男士针织衫"},
                {"product_id": "p2", "product_name": "未知商品"},
            ]
        },
    )

    assert result["errorProductIds"] == ["p1", "p2"]
    assert result["warnings"] == ["商品详情缺失：p2"]


def test_relevance_agent_keeps_generating_when_product_detail_api_is_unavailable():
    class BrokenProductDetails:
        def fetch(self, _brand, _ids):
            raise RuntimeError("product api unavailable")

    runtime = Runtime([
        '{"toolCall":{"name":"product_details","args":{}}}',
        '{"groups":[{"rootCause":"uncertain","productIds":["p1"]}]}',
    ])
    agent = RelevanceRootCauseAgent(
        runtime=runtime,
        kg_tools=KnowledgeGraphReadTools(KGClient(), search=Search()),
        product_details=BrokenProductDetails(),
    )

    result = agent.analyze(
        brand="atlas",
        query_text="薄开衫 女士",
        evidence={"blacklist_candidates": [{"product_id": "p1", "product_name": "未知商品"}]},
    )

    assert result["errorProductIds"] == ["p1"]
    assert result["warnings"] == ["商品详情读取失败：product api unavailable"]


def test_relevance_agent_does_not_execute_actions_for_products_without_details():
    class PartialProductDetails:
        def fetch(self, _brand, _ids):
            return {"p1": {"productId": "p1", "name": "已核实商品"}}

    runtime = Runtime([
        '{"toolCall":{"name":"product_details","args":{}}}',
        '{"groups":[{"rootCause":"individual","productIds":["p1","p2"]}]}',
    ])
    agent = RelevanceRootCauseAgent(
        runtime=runtime,
        kg_tools=KnowledgeGraphReadTools(KGClient(), search=Search()),
        product_details=PartialProductDetails(),
    )

    result = agent.analyze(
        brand="atlas",
        query_text="薄开衫 女士",
        evidence={"blacklist_candidates": [
            {"product_id": "p1", "reason": "错误召回"},
            {"product_id": "p2", "reason": "错误召回"},
        ]},
    )

    individual = next(group for group in result["causeGroups"] if group["rootCause"] == "individual")
    uncertain = next(group for group in result["causeGroups"] if group["rootCause"] == "uncertain")
    assert individual["productIds"] == ["p1"]
    assert individual["actions"] == []
    assert uncertain["productIds"] == ["p2"]
    assert uncertain["actions"] == []


def test_both_agents_create_distinct_langfuse_agent_observations():
    client = TraceClient()
    observability = QueryGovernanceObservability(client=client, enabled=True)
    runtime = Runtime([
        '{"toolCall":{"name":"search_products","args":{"query":"薄开衫"}}}',
        '{"decision":"no_change","reason":"evidence","actions":[]}',
    ])
    kg_tools = KnowledgeGraphReadTools(KGClient(), search=Search(), observability=observability)
    kg = KGAgent(runtime=runtime, tools=kg_tools)
    kg.generate(
        brand="atlas", query_text="薄开衫", trigger_source="recall_anomaly",
        evidence={"result_count": 20}, langfuse_config={"metadata": {"query_trace_id": "trace-dual-agent"}},
    )
    relevance_runtime = Runtime([
        '{"toolCall":{"name":"product_details","args":{}}}',
        '{"groups":[{"rootCause":"uncertain","productIds":["p1"]}]}',
    ])
    relevance = RelevanceRootCauseAgent(
        runtime=relevance_runtime, kg_tools=kg_tools,
        product_details=type("Details", (), {"fetch": lambda self, _brand, ids: {ids[0]: {"tags": ["tag"]}}})(),
    )
    relevance.observability = observability
    relevance.analyze(
        brand="atlas", query_text="薄开衫", evidence={"blacklist_candidates": [{"product_id": "p1"}]},
        langfuse_config={"metadata": {"query_trace_id": "trace-dual-agent"}},
    )
    assert {item.kwargs["name"] for item in client.observations if item.kwargs["as_type"] == "agent"} == {
        "query-governance.kg-agent", "query-governance.relevance-agent"
    }
    assert {item.kwargs["as_type"] for item in client.observations} <= {"agent", "tool"}
