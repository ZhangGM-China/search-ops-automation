from typing import Any, Dict

from src.llm.node_config import NodeConfigSnapshot, get_snapshot_node_config
from src.llm.config_store import capture_runtime_snapshot
from .governance import NoResultGovernanceService


def _no_result_model_config_snapshot(state: Dict[str, Any]) -> NodeConfigSnapshot:
    snapshot = state.get("model_config_snapshot") or capture_runtime_snapshot(
        node_keys=("no_result.intent", "no_result.diagnosis"),
    )
    get_snapshot_node_config(snapshot, "no_result.intent", required_channel="rd_llm")
    get_snapshot_node_config(snapshot, "no_result.diagnosis", required_channel="rd_llm")
    return snapshot


def no_result_normalize_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """无结果词治理：按品牌 SQL 输出归一化为统一 Query 契约。"""
    service = state.get("service") or NoResultGovernanceService()
    records = service.normalize_rows(
        brand=state["brand"],
        target_date=state["target_date"],
        rows=state.get("raw_rows", []),
        excluded_keywords=state.get("excluded_keywords", []),
    )
    return {"records": records}


def no_result_execute_plan_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """无结果词治理：执行 Agent 结构化图谱修复计划并记录可回滚动作。"""
    service = state.get("service") or NoResultGovernanceService()
    result = service.execute_agent_plan(
        query_id=state["query_id"],
        plan=state.get("plan", {}),
    )
    return {"execution_result": result}


def no_result_filter_intent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """无结果词治理：大模型二筛购物意图。"""
    service = state.get("service") or NoResultGovernanceService()
    snapshot = _no_result_model_config_snapshot(state)
    result = service.filter_query_intent(
        query_id=state["query_id"],
        agent=state.get("agent"),
        model_config_snapshot=snapshot,
    )
    return {"intent_result": result, "model_config_snapshot": snapshot}


def no_result_segment_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """无结果词治理：分词生成候选标签。"""
    service = state.get("service") or NoResultGovernanceService()
    tags = service.segment_query(query_id=state["query_id"])
    return {"candidate_tags": tags}


def no_result_graph_context_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """无结果词治理：查询知识图谱上下文。"""
    service = state.get("service") or NoResultGovernanceService()
    context = service.fetch_graph_context(
        query_id=state["query_id"],
        candidate_tags=state.get("candidate_tags"),
    )
    return {"graph_context": context}


def no_result_diagnose_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """无结果词治理：归因分析与任务路由。"""
    service = state.get("service") or NoResultGovernanceService()
    snapshot = _no_result_model_config_snapshot(state)
    result = service.diagnose_query(
        query_id=state["query_id"],
        graph_context=state.get("graph_context"),
        agent=state.get("agent"),
        model_config_snapshot=snapshot,
    )
    return {"diagnosis_result": result, "model_config_snapshot": snapshot}


def no_result_queue_retag_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """无结果词治理：将已产生图谱变更的批次加入品牌级重新打标签队列。"""
    service = state.get("service") or NoResultGovernanceService()
    result = service.queue_retag_for_batch(
        batch_id=state["batch_id"],
        now=state.get("now"),
        cooldown_hours=state.get("cooldown_hours", 2),
    )
    return {"retag_queue_result": result}


def no_result_review_recall_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """无结果词治理：重新打标签后复查 Query 召回结果。"""
    service = state.get("service") or NoResultGovernanceService()
    result = service.review_query_recall(
        query_id=state["query_id"],
        ops_service=state.get("ops_service"),
        top_n=state.get("top_n", 16),
    )
    return {"review_result": result}
