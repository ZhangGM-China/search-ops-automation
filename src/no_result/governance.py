import datetime
import inspect
import json
import os
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import or_

from src.persistence import (
    NoResultAgentEvent,
    NoResultGovernanceAction,
    NoResultGovernanceBatch,
    NoResultIgnoreRecord,
    NoResultQuery,
    NoResultRetagTask,
    SessionLocal,
)
from src.integrations.knowledge_graph import (
    DryRunKnowledgeGraphClient,
    KnowledgeGraphClient,
)
from src.governance.query_policy import assert_query_governance_allowed
from src.llm.node_config import NodeConfigSnapshot, get_snapshot_node_config
from src.llm.config_store import capture_active_snapshot
from src.query_governance.langfuse import QueryGovernanceObservability
from src.query_governance.models import QuerySpec


class NoResultGovernanceService:
    """无结果词治理核心服务。

    该服务只负责数据契约、动作留痕和状态流转；真实知识图谱 API 由 kg_client 注入。
    """

    DEFAULT_MIN_SEARCH_PV = 5
    QUERY_TOOL_NAMES = {"query_tag", "query_relationship", "query_dimension"}
    WRITE_PLAN_TOOL_NAMES = {
        "add_tag", "update_tag", "blacklist_tag", "unblacklist_tag",
        "add_relationship", "update_relationship", "delete_relationship",
    }
    EXECUTION_TOOL_NAMES = {"execute_pending_action", "approve_manual_action", "rollback_action"}

    def __init__(
        self,
        kg_client: Optional[Any] = None,
        observability: Optional[QueryGovernanceObservability] = None,
    ):
        self._kg_client_injected = kg_client is not None
        self.kg_client = kg_client or self._build_default_kg_client()
        self.observability = observability or QueryGovernanceObservability.from_environment()
        self._paused_query_ids: set[int] = set()

    def _langfuse_config(
        self,
        *,
        query_id: int,
        brand: str,
        query: str,
        node: str,
        model_config_snapshot: NodeConfigSnapshot | None,
    ) -> Dict[str, Any]:
        trace_query_id = f"no-result-{query_id}"
        batch = self.observability.start_batch(
            trace_query_id,
            [QuerySpec(trace_query_id, brand, query)],
            metadata={"operation": node},
            trace_name="no-result",
        )
        return batch.query_config(
            trace_query_id,
            node,
            model_config_version=(model_config_snapshot.version if model_config_snapshot else None),
        )

    @staticmethod
    def _build_default_kg_client() -> Any:
        real_write_enabled = os.getenv("NO_RESULT_KG_REAL_WRITE", "").lower() in {"1", "true", "yes"}
        if real_write_enabled:
            return KnowledgeGraphClient()
        return DryRunKnowledgeGraphClient()

    def get_agent_tool_registry(self) -> List[Dict[str, Any]]:
        """返回暴露给无结果词 Agent Harness 的工具边界说明。"""
        tools = []
        for name in sorted(self.QUERY_TOOL_NAMES):
            tools.append({
                "name": name,
                "layer": "query",
                "description": "只读查询知识图谱上下文",
                "autoCallableInDryRun": True,
                "writeGateRequired": False,
            })
        for name in sorted(self.WRITE_PLAN_TOOL_NAMES):
            tools.append({
                "name": name,
                "layer": "write_plan",
                "description": "仅生成知识图谱写入计划，不直接执行",
                "autoCallableInDryRun": False,
                "writeGateRequired": True,
            })
        for name in sorted(self.EXECUTION_TOOL_NAMES):
            tools.append({
                "name": name,
                "layer": "execution",
                "description": "由系统执行器或人工确认触发，必须经过写操作网关",
                "autoCallableInDryRun": False,
                "writeGateRequired": True,
            })
        return tools

    def normalize_rows(
        self,
        brand: str,
        target_date: datetime.datetime,
        rows: Iterable[Dict[str, Any]],
        excluded_keywords: Optional[List[str]] = None,
        now: Optional[datetime.datetime] = None,
        min_search_pv: Optional[int] = None,
        min_search_uv: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        current_time = now or datetime.datetime.now()
        effective_min_search_pv = self.DEFAULT_MIN_SEARCH_PV if min_search_pv is None else min_search_pv
        excluded = {item.strip() for item in (excluded_keywords or []) if item and item.strip()}
        ignored_queries = self._active_ignored_queries(brand, current_time)
        normalized: List[Dict[str, Any]] = []
        seen = set()
        for row in rows:
            query_text = str(row.get("keyword") or row.get("search_text") or row.get("query") or "").strip()
            if not query_text:
                continue
            if query_text in excluded:
                continue
            if query_text in ignored_queries:
                continue
            if re.fullmatch(r"\d{5,}", query_text):
                continue
            key = (brand, query_text)
            if key in seen:
                continue
            seen.add(key)
            no_result_count = int(row.get("no_result_count") or 0)
            search_pv = int(row.get("search_pv") or row.get("pv") or no_result_count or 0)
            search_uv = row.get("search_uv") or row.get("uv")
            normalized_uv = int(search_uv) if search_uv is not None else None
            if search_pv < effective_min_search_pv:
                continue
            if min_search_uv is not None and normalized_uv is not None and normalized_uv < min_search_uv:
                continue
            normalized.append({
                "brand": brand,
                "query": query_text,
                "data_date": target_date,
                "no_result_count": no_result_count,
                "search_pv": search_pv,
                "search_uv": normalized_uv,
                "before_result_count": 0,
                "source": f"{brand}_no_result_sql",
                "raw_payload": row,
                "status": "pending",
            })
        return normalized

    def create_batch_from_records(
        self,
        brand: str,
        target_date: datetime.datetime,
        records: List[Dict[str, Any]],
    ) -> NoResultGovernanceBatch:
        db = SessionLocal()
        try:
            batch = NoResultGovernanceBatch(
                brand=brand,
                batch_date=target_date,
                status="pending",
                total_queries=len(records),
                started_at=datetime.datetime.now(),
            )
            db.add(batch)
            db.flush()
            for record in records:
                db.add(NoResultQuery(
                    batch_id=batch.id,
                    brand=brand,
                    query_text=record.get("query"),
                    data_date=record.get("data_date") or target_date,
                    no_result_count=record.get("no_result_count") or 0,
                    search_pv=record.get("search_pv") or 0,
                    search_uv=record.get("search_uv"),
                    before_result_count=record.get("before_result_count") or 0,
                    after_result_count=record.get("after_result_count"),
                    source=record.get("source"),
                    raw_payload=json.dumps(record.get("raw_payload") or {}, ensure_ascii=False),
                    status=record.get("status") or "pending",
                    ai_decision=record.get("ai_decision"),
                    ai_reason=record.get("ai_reason"),
                    final_strategy=record.get("final_strategy"),
                    candidate_tags=json.dumps(record.get("candidate_tags") or [], ensure_ascii=False),
                ))
            db.commit()
            db.refresh(batch)
            db.expunge(batch)
            return batch
        finally:
            db.close()

    def execute_agent_plan(self, query_id: int, plan: Dict[str, Any]) -> Dict[str, Any]:
        max_risk_level = plan.get("maxRiskLevel") or plan.get("max_risk_level") or os.getenv("NO_RESULT_AUTO_MAX_RISK", "high")
        execution_mode = self._execution_mode(plan)
        planned_actions = [
            self._normalize_plan_action(action, plan, execution_mode)
            for action in plan.get("actions", [])
            if isinstance(action, dict)
        ]
        real_write_authorized = execution_mode != "real_write" or self._real_write_authorized()
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")

            if any(
                not (execution_mode == "dry_run" and not self._is_query_action(str(
                    action.get("actionType") or action.get("action_type") or ""
                )))
                and real_write_authorized
                and self._risk_allowed(
                    str(action.get("riskLevel") or action.get("risk_level") or "low"),
                    str(max_risk_level),
                )
                for action in planned_actions
            ):
                assert_query_governance_allowed(str(query.brand or ""), str(query.query_text or ""))

            initial_status = str(query.status or "")
            if initial_status not in {"pending", "pending_graph_fix"}:
                planned_fingerprints = {
                    self._action_fingerprint(action)
                    for action in planned_actions
                }
                existing_fingerprints = self._existing_action_fingerprints(db, query.batch_id)
                if planned_fingerprints and planned_fingerprints.issubset(existing_fingerprints):
                    return {"queryId": query_id, "executedActions": 0, "pendingActions": 0}
                raise ValueError(
                    f"No result query is not executable from status: {initial_status}"
                )

            ai_decision = plan.get("decision") or plan.get("outputType") or plan.get("output_type")
            ai_reason = plan.get("reason")
            explicit_strategy = plan.get("finalStrategy") or plan.get("final_strategy")
            if explicit_strategy:
                final_strategy = explicit_strategy
            else:
                final_strategy = json.dumps(plan, ensure_ascii=False)
            current_time = datetime.datetime.now()
            db.rollback()
            claimed = db.query(NoResultQuery).filter(
                NoResultQuery.id == query_id,
                NoResultQuery.status == initial_status,
            ).update({
                NoResultQuery.status: "execution_running",
                NoResultQuery.ai_decision: ai_decision,
                NoResultQuery.ai_reason: ai_reason,
                NoResultQuery.final_strategy: final_strategy,
                NoResultQuery.processed_at: current_time,
                NoResultQuery.updated_at: current_time,
            }, synchronize_session=False)
            if claimed != 1:
                db.rollback()
                db.expire_all()
                current = db.get(NoResultQuery, query_id)
                raise ValueError(
                    f"No result query was claimed concurrently from status: "
                    f"{current.status if current else 'missing'}"
                )
            db.commit()
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")

            executed = 0
            pending = 0
            covered_success = 0
            self._record_agent_event(
                db,
                query,
                stage="write_plan",
                event_type="tool_request",
                summary=f"Agent 生成 {len(planned_actions)} 个图谱写入计划",
                payload={
                    "executionMode": execution_mode,
                    "reason": plan.get("reason"),
                    "attributionType": plan.get("attributionType") or plan.get("attribution_type"),
                    "actions": planned_actions,
                },
            )
            executed_fingerprints: set[str] = set()
            for action in planned_actions:
                db.expire(query)
                current_query_status = db.query(NoResultQuery.status).filter(
                    NoResultQuery.id == query_id,
                ).scalar()
                if current_query_status in {"execution_paused", "execution_unknown"}:
                    break
                fingerprint = self._action_fingerprint(action)
                if fingerprint in executed_fingerprints:
                    continue
                risk_level = action.get("riskLevel") or action.get("risk_level") or "low"
                action_type = action.get("actionType") or action.get("action_type")
                if execution_mode == "dry_run" and not self._is_query_action(str(action_type or "")):
                    db.add(NoResultGovernanceAction(
                        query_id=query.id,
                        batch_id=query.batch_id,
                        brand=query.brand,
                        query_text=query.query_text,
                        action_type=action_type,
                        target_interface=action.get("targetInterface") or action.get("target_interface"),
                        request_payload=json.dumps(action, ensure_ascii=False),
                        response_payload=json.dumps({"message": "dry_run_pending_execute"}, ensure_ascii=False),
                        before_snapshot=json.dumps({}, ensure_ascii=False),
                        rollback_payload=json.dumps({}, ensure_ascii=False),
                        status="pending_execute",
                        rollback_status="not_required",
                        risk_level=risk_level,
                        agent_reason=action.get("reason"),
                        error_message="dry_run: waiting for manual execution",
                    ))
                    pending += 1
                    continue
                if execution_mode == "real_write" and not real_write_authorized:
                    db.add(NoResultGovernanceAction(
                        query_id=query.id,
                        batch_id=query.batch_id,
                        brand=query.brand,
                        query_text=query.query_text,
                        action_type=action_type,
                        target_interface=action.get("targetInterface") or action.get("target_interface"),
                        request_payload=json.dumps(action, ensure_ascii=False),
                        response_payload=json.dumps({"message": "blocked_by_real_write_gate"}, ensure_ascii=False),
                        before_snapshot=json.dumps({}, ensure_ascii=False),
                        rollback_payload=json.dumps({}, ensure_ascii=False),
                        status="pending_execute",
                        rollback_status="not_required",
                        risk_level=risk_level,
                        agent_reason=action.get("reason"),
                        error_message="real write requires backend authorization",
                    ))
                    pending += 1
                    continue
                if not self._risk_allowed(risk_level, max_risk_level):
                    db.add(NoResultGovernanceAction(
                        query_id=query.id,
                        batch_id=query.batch_id,
                        brand=query.brand,
                        query_text=query.query_text,
                        action_type=action_type,
                        target_interface=action.get("targetInterface") or action.get("target_interface"),
                        request_payload=json.dumps(action, ensure_ascii=False),
                        response_payload=json.dumps({"message": "blocked_by_risk_gate"}, ensure_ascii=False),
                        before_snapshot=json.dumps({}, ensure_ascii=False),
                        rollback_payload=json.dumps({}, ensure_ascii=False),
                        status="manual_review",
                        rollback_status="not_required",
                        risk_level=risk_level,
                        agent_reason=action.get("reason"),
                        error_message="risk level requires manual review",
                    ))
                    continue
                if query.batch_id:
                    locked = db.query(NoResultGovernanceBatch).filter(
                        NoResultGovernanceBatch.id == query.batch_id,
                    ).update({
                        NoResultGovernanceBatch.changed_queries:
                            NoResultGovernanceBatch.changed_queries,
                    }, synchronize_session=False)
                    if locked != 1:
                        db.rollback()
                        raise ValueError(f"No result batch not found: {query.batch_id}")
                    occupied_fingerprints = self._existing_action_fingerprints(
                        db,
                        query.batch_id,
                    )
                    if fingerprint in occupied_fingerprints:
                        occupied = self._find_occupied_action(
                            db,
                            query.batch_id,
                            fingerprint,
                        )
                        batch = db.get(NoResultGovernanceBatch, query.batch_id)
                        if occupied and occupied.status == "success":
                            covered_success += 1
                            executed_fingerprints.add(fingerprint)
                            continue
                        query.status = (
                            "manual_required"
                            if occupied and occupied.status == "execution_unknown"
                            else initial_status
                        ) # type: ignore
                        query.updated_at = datetime.datetime.now() # type: ignore
                        db.commit()
                        return {
                            "queryId": query_id,
                            "executedActions": executed,
                            "pendingActions": pending,
                            "deferred": True,
                            "reason": (
                                "batch_action_execution_unknown"
                                if occupied and occupied.status == "execution_unknown"
                                else "batch_action_in_progress"
                                if batch and batch.status in {"execution_running", "executing"}
                                else "batch_action_occupied"
                            ),
                        }
                action_record = NoResultGovernanceAction(
                    query_id=query.id,
                    batch_id=query.batch_id,
                    brand=query.brand,
                    query_text=query.query_text,
                    action_type=action_type,
                    target_interface=action.get("targetInterface") or action.get("target_interface"),
                    request_payload=json.dumps(action, ensure_ascii=False),
                    response_payload="{}",
                    before_snapshot="{}",
                    rollback_payload="{}",
                    status="executing",
                    rollback_status="not_required",
                    risk_level=risk_level,
                    agent_reason=action.get("reason"),
                )
                db.add(action_record)
                batch = db.get(NoResultGovernanceBatch, query.batch_id) if query.batch_id else None
                action, external_request_id = self._begin_action_execution(
                    db,
                    action_record,
                    None,
                    batch,
                    action,
                )
                try:
                    snapshot = self.kg_client.snapshot(action)
                    response = self._execute_kg_write(
                        query.brand,
                        query.query_text,
                        action,
                        external_request_id=external_request_id,
                    )
                    rollback_payload = self.kg_client.build_rollback(action, snapshot, response)
                except Exception as exc:
                    self._finish_action_exception(db, action_record, query, batch, exc)
                    raise
                action_status = "success" if self._response_succeeded(response) else "failed"
                action_record.before_snapshot = json.dumps(snapshot, ensure_ascii=False) # type: ignore
                action_record.response_payload = json.dumps(response, ensure_ascii=False) # type: ignore
                action_record.rollback_payload = json.dumps(rollback_payload, ensure_ascii=False) # type: ignore
                action_record.status = action_status # type: ignore
                action_record.rollback_status = "pending" if action_status == "success" else "not_required" # type: ignore
                action_record.error_message = None if action_status == "success" else response.get("message") # type: ignore
                action_record.updated_at = datetime.datetime.now() # type: ignore
                if action_status == "success":
                    executed += 1
                    executed_fingerprints.add(fingerprint)
                db.flush()
                db.expire(query)
                current_query_status = db.query(NoResultQuery.status).filter(
                    NoResultQuery.id == query_id,
                ).scalar()
                if current_query_status in {"execution_paused", "execution_unknown"}:
                    break

            desired_query_status = (
                "awaiting_retag"
                if executed
                else "pending_execute"
                if pending
                else "awaiting_retag"
                if covered_success
                else "manual_required"
            )
            db.query(NoResultQuery).filter(
                NoResultQuery.id == query_id,
                NoResultQuery.status == "execution_running",
            ).update({
                NoResultQuery.status: desired_query_status,
                NoResultQuery.updated_at: datetime.datetime.now(),
            }, synchronize_session=False)
            if query.batch_id:
                batch = db.get(NoResultGovernanceBatch, query.batch_id)
                if batch:
                    batch.changed_queries = (batch.changed_queries or 0) + (1 if executed else 0) # type: ignore
                    if executed:
                        batch.status = "awaiting_retag" # type: ignore
                    elif not pending and not covered_success:
                        batch.status = "manual_required" # type: ignore
                    batch.updated_at = datetime.datetime.now() # type: ignore
            db.commit()
            return {"queryId": query_id, "executedActions": executed, "pendingActions": pending}
        finally:
            db.close()

    def filter_query_intent(
        self,
        query_id: int,
        agent: Optional[Any] = None,
        now: Optional[datetime.datetime] = None,
        ignore_ttl_days: int = 30,
        model_config_snapshot: Optional[NodeConfigSnapshot] = None,
    ) -> Dict[str, Any]:
        if model_config_snapshot is not None:
            get_snapshot_node_config(
                model_config_snapshot,
                "no_result.intent",
                required_channel="rd_llm",
            )
        current_time = now or datetime.datetime.now()
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")
            if agent is None:
                from src.llm.agents import NoResultQueryIntentAgent
                agent = NoResultQueryIntentAgent(
                    model_config_snapshot=model_config_snapshot,
                    langfuse_config=self._langfuse_config(
                        query_id=query_id,
                        brand=query.brand,
                        query=query.query_text,
                        node="no_result.intent",
                        model_config_snapshot=model_config_snapshot,
                    ),
                )
            result = agent.classify(query.query_text, query.brand)
            self._record_agent_event(
                db,
                query,
                stage="intent_filter",
                event_type="reasoning",
                summary=result.get("reason") or "完成购物意图二筛",
                payload=result,
            )
            query.ai_decision = result.get("decision") # type: ignore
            query.ai_reason = result.get("reason") # type: ignore
            query.status = "ignored" if result.get("decision") == "ignore" else "pending_context" # type: ignore
            query.updated_at = current_time # type: ignore
            if result.get("decision") == "ignore":
                ignore = db.query(NoResultIgnoreRecord).filter(
                    NoResultIgnoreRecord.brand == query.brand,
                    NoResultIgnoreRecord.query_text == query.query_text,
                ).one_or_none()
                if not ignore:
                    ignore = NoResultIgnoreRecord(
                        brand=query.brand,
                        query_text=query.query_text,
                        source="llm_intent_filter",
                    )
                    db.add(ignore)
                ignore.reason = result.get("reason") # type: ignore
                ignore.model_version = result.get("modelVersion") or result.get("model_version") # type: ignore
                ignore.expires_at = current_time + datetime.timedelta(days=ignore_ttl_days) # type: ignore
                ignore.updated_at = current_time # type: ignore
            db.commit()
            return result
        finally:
            db.close()

    def segment_query(self, query_id: int) -> List[str]:
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")
            tags = self.segment_text(query.query_text)
            self._record_agent_event(
                db,
                query,
                stage="segmentation",
                event_type="tool_result",
                tool_name="jieba",
                summary=f"生成 {len(tags)} 个候选标签",
                payload={"candidateTags": tags},
            )
            query.candidate_tags = json.dumps(tags, ensure_ascii=False) # type: ignore
            query.updated_at = datetime.datetime.now() # type: ignore
            db.commit()
            return tags
        finally:
            db.close()

    def fetch_graph_context(self, query_id: int, candidate_tags: Optional[List[str]] = None) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")
            tags = candidate_tags if candidate_tags is not None else self._loads_list(query.candidate_tags)
            planned_tools = [
                {"toolName": "query_tag", "candidate": tag}
                for tag in tags
            ] + [
                {"toolName": "query_relationship", "candidate": tag}
                for tag in tags
            ] + [{"toolName": "query_dimension"}]
            self._record_agent_event(
                db,
                query,
                stage="graph_context_request",
                event_type="tool_request",
                summary=f"准备调用 {len(planned_tools)} 个只读图谱查询",
                payload={"tools": planned_tools},
            )
            context = {"tags": [], "relationships": [], "dimensions": []}
            for tag in tags:
                context["tags"].append({
                    "candidate": tag,
                    "result": self.kg_client.execute({
                        "brand": query.brand,
                        "actionType": "query_tag",
                        "params": {"name": tag, "nameAcc": tag, "pageNum": 1, "pageSize": 30},
                    }),
                })
                context["relationships"].append({
                    "candidate": tag,
                    "result": self.kg_client.execute({
                        "brand": query.brand,
                        "actionType": "query_relationship",
                        "params": {"tagName": tag, "pageNum": 1, "pageSize": 30},
                    }),
                })
            context["dimensions"].append(self.kg_client.execute({
                "brand": query.brand,
                "actionType": "query_dimension",
                "params": {"pageNum": 1, "pageSize": 100},
            }))
            raw_payload = self._loads(query.raw_payload)
            raw_payload["graphContext"] = context
            self._record_agent_event(
                db,
                query,
                stage="graph_context_result",
                event_type="tool_result",
                summary=f"已获取 {len(context['tags'])} 个标签结果、{len(context['relationships'])} 个关系结果",
                payload=self._summarize_graph_context(context),
            )
            query.raw_payload = json.dumps(raw_payload, ensure_ascii=False) # type: ignore
            query.status = "pending_diagnosis" # type: ignore
            query.updated_at = datetime.datetime.now() # type: ignore
            db.commit()
            return context
        finally:
            db.close()

    def diagnose_query(
        self,
        query_id: int,
        graph_context: Optional[Dict[str, Any]] = None,
        agent: Optional[Any] = None,
        max_rounds: Optional[int] = None,
        model_config_snapshot: Optional[NodeConfigSnapshot] = None,
    ) -> Dict[str, Any]:
        if model_config_snapshot is not None:
            get_snapshot_node_config(
                model_config_snapshot,
                "no_result.diagnosis",
                required_channel="rd_llm",
            )
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")
            if agent is None:
                from src.llm.agents import NoResultDiagnosisAgent
                agent = NoResultDiagnosisAgent(
                    model_config_snapshot=model_config_snapshot,
                    langfuse_config=self._langfuse_config(
                        query_id=query_id,
                        brand=query.brand,
                        query=query.query_text,
                        node="no_result.diagnosis",
                        model_config_snapshot=model_config_snapshot,
                    ),
                )
            raw_payload = self._loads(query.raw_payload)
            context = graph_context or raw_payload.get("graphContext") or {}
            candidate_tags = self._loads_list(query.candidate_tags)
            result = agent.diagnose(query.query_text, candidate_tags, context)
            llm_metrics = result.pop("_llm_metrics", None) if isinstance(result, dict) else None
            result = self._normalize_diagnosis_metadata(result)
            if result.get("outputType") == "query_request":
                current_rounds = int(raw_payload.get("noResultDiagnosisRounds") or 0)
                if max_rounds is not None and current_rounds >= max_rounds:
                    result = {
                        "outputType": "manual_required",
                        "reason": f"已达到最大查询轮次 {max_rounds}",
                        "confidence": result.get("confidence", 0),
                        "actions": [],
                        "queryIntents": result.get("queryIntents") or [],
                    }
                else:
                    raw_payload["noResultDiagnosisRounds"] = current_rounds + 1
            query.final_strategy = json.dumps(result, ensure_ascii=False) # type: ignore
            query.raw_payload = json.dumps(raw_payload, ensure_ascii=False) # type: ignore
            diagnosis_stage = {
                "strategy": "diagnosis_strategy",
                "query_request": "diagnosis_query_request",
                "ignore": "diagnosis_ignore",
            }.get(str(result.get("outputType")), "diagnosis_manual_required")
            self._record_agent_event(
                db,
                query,
                stage=diagnosis_stage,
                event_type="final_strategy" if result.get("outputType") == "strategy" else "reasoning",
                summary=result.get("reason") or "完成无结果词诊断",
                payload={
                    **result,
                    "llmMetrics": llm_metrics or {},
                },
            )
            if result.get("outputType") == "strategy":
                query.status = "pending_graph_fix" # type: ignore
            elif result.get("outputType") == "query_request":
                query.status = "pending_context" # type: ignore
            elif result.get("outputType") == "ignore":
                query.status = "ignored" # type: ignore
            else:
                query.status = "manual_required" # type: ignore
            query.updated_at = datetime.datetime.now() # type: ignore
            db.commit()
            return result
        finally:
            db.close()

    def rollback_batch(
        self,
        batch_id: int,
        now: Optional[datetime.datetime] = None,
        cooldown_hours: int = 2,
        operator: str = "",
        reason: str = "",
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        current_time = now or datetime.datetime.now()
        db = SessionLocal()
        try:
            batch = db.get(NoResultGovernanceBatch, batch_id)
            if not batch:
                raise ValueError(f"No result governance batch not found: {batch_id}")
            actions = db.query(NoResultGovernanceAction).filter(
                NoResultGovernanceAction.batch_id == batch_id,
                NoResultGovernanceAction.status == "success",
            ).order_by(NoResultGovernanceAction.created_at.desc(), NoResultGovernanceAction.id.desc()).all()
            self._assert_rollback_actions_allowed(actions)
            batch.rollback_status = "rolling_back" # type: ignore
            batch.updated_at = current_time # type: ignore
            db.commit()

            rolled_back = 0
            failed = 0
            conflicts = 0
            for action in actions:
                if self._rollback_write_not_required(action):
                    continue
                request_payload = self._loads(action.request_payload)
                before_snapshot = self._loads(action.before_snapshot)
                conflict = self._detect_rollback_conflict(request_payload, before_snapshot)
                if conflict.get("hasConflict"):
                    conflicts += 1
                    action.rollback_status = "conflict" # type: ignore
                    action.error_message = conflict.get("reason") or "rollback conflict detected" # type: ignore
                    action.updated_at = current_time # type: ignore
                    continue

                query = db.get(NoResultQuery, action.query_id) if action.query_id else None
                rollback_payload, action_external_request_id = self._begin_rollback_execution(
                    db,
                    action,
                    query,
                    batch,
                    self._loads(action.rollback_payload),
                    now=current_time,
                    external_request_id=(
                        f"{external_request_id}:action:{action.id}"
                        if external_request_id
                        else None
                    ),
                )
                try:
                    response = self._execute_kg_write(
                        action.brand,
                        action.query_text,
                        rollback_payload,
                        external_request_id=action_external_request_id,
                    )
                except Exception as exc:
                    self._finish_rollback_exception(db, action, query, batch, exc, now=current_time)
                    raise
                self._record_rollback_action(db, action, rollback_payload, response, current_time)
                if self._response_succeeded(response):
                    action.rollback_status = "success" # type: ignore
                    rolled_back += 1
                else:
                    action.rollback_status = "failed" # type: ignore
                    action.error_message = response.get("message") or "rollback failed" # type: ignore
                    failed += 1
                action.updated_at = current_time # type: ignore

            if conflicts:
                queries = db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch_id).all()
                for row in queries:
                    row.status = "manual_required" # type: ignore
                    row.updated_at = current_time # type: ignore
                batch.status = "manual_required" # type: ignore
                batch.rollback_status = "conflict_manual_review" # type: ignore
                batch.updated_at = current_time # type: ignore
                db.commit()
                return {
                    "batchId": batch_id,
                    "rolledBackActions": rolled_back,
                    "status": "conflict_manual_review",
                    "conflicts": conflicts,
                    "failedActions": failed,
                }

            queries = db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch_id).all()
            for row in queries:
                row.status = "rolled_back" # type: ignore
                row.updated_at = current_time # type: ignore

            batch.status = "rolled_back" # type: ignore
            batch.rollback_status = "partial_failed" if failed else "success" # type: ignore
            batch.updated_at = current_time # type: ignore
            db.commit()
            retag_result = self.queue_retag_for_batch(batch_id, now=current_time, cooldown_hours=cooldown_hours)
            return {
                "batchId": batch_id,
                "rolledBackActions": rolled_back,
                "status": "rolled_back" if not failed else "partial_failed",
                "failedActions": failed,
                "retag": retag_result,
            }
        finally:
            db.close()

    def rollback_query(
        self,
        query_id: int,
        now: Optional[datetime.datetime] = None,
        cooldown_hours: int = 2,
        operator: str = "",
        reason: str = "",
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        current_time = now or datetime.datetime.now()
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")

            actions = db.query(NoResultGovernanceAction).filter(
                NoResultGovernanceAction.query_id == query_id,
                NoResultGovernanceAction.status == "success",
            ).order_by(NoResultGovernanceAction.created_at.desc(), NoResultGovernanceAction.id.desc()).all()
            self._assert_rollback_actions_allowed(actions)
            batch = db.get(NoResultGovernanceBatch, query.batch_id) if query.batch_id else None

            rolled_back = 0
            failed = 0
            conflicts = 0
            for action in actions:
                if self._rollback_write_not_required(action):
                    continue
                request_payload = self._loads(action.request_payload)
                before_snapshot = self._loads(action.before_snapshot)
                conflict = self._detect_rollback_conflict(request_payload, before_snapshot)
                if conflict.get("hasConflict"):
                    conflicts += 1
                    action.rollback_status = "conflict" # type: ignore
                    action.error_message = conflict.get("reason") or "rollback conflict detected" # type: ignore
                    action.updated_at = current_time # type: ignore
                    continue

                rollback_payload, action_external_request_id = self._begin_rollback_execution(
                    db,
                    action,
                    query,
                    batch,
                    self._loads(action.rollback_payload),
                    now=current_time,
                    external_request_id=(
                        f"{external_request_id}:action:{action.id}"
                        if external_request_id
                        else None
                    ),
                )
                try:
                    response = self._execute_kg_write(
                        action.brand,
                        action.query_text,
                        rollback_payload,
                        external_request_id=action_external_request_id,
                    )
                except Exception as exc:
                    self._finish_rollback_exception(db, action, query, batch, exc, now=current_time)
                    raise
                self._record_rollback_action(db, action, rollback_payload, response, current_time)
                if self._response_succeeded(response):
                    action.rollback_status = "success" # type: ignore
                    rolled_back += 1
                else:
                    action.rollback_status = "failed" # type: ignore
                    action.error_message = response.get("message") or "rollback failed" # type: ignore
                    failed += 1
                action.updated_at = current_time # type: ignore

            if conflicts:
                query.status = "manual_required" # type: ignore
                query.updated_at = current_time # type: ignore
                db.commit()
                return {
                    "queryId": query_id,
                    "batchId": query.batch_id,
                    "rolledBackActions": rolled_back,
                    "status": "conflict_manual_review",
                    "conflicts": conflicts,
                    "failedActions": failed,
                }

            query.status = "rolled_back" # type: ignore
            query.updated_at = current_time # type: ignore
            should_queue_retag = False
            if batch and failed == 0:
                allowed_aggregate_statuses = {"pending", "success", "not_required"}
                db.flush()
                unsettled_action_exists = db.query(NoResultGovernanceAction.id).filter(
                    NoResultGovernanceAction.batch_id == NoResultGovernanceBatch.id,
                    NoResultGovernanceAction.status == "success",
                    or_(
                        NoResultGovernanceAction.rollback_status.is_(None),
                        NoResultGovernanceAction.rollback_status.notin_(allowed_aggregate_statuses),
                    ),
                ).exists()
                claimed = db.query(NoResultGovernanceBatch).filter(
                    NoResultGovernanceBatch.id == batch.id,
                    NoResultGovernanceBatch.status == "rolling_back",
                    NoResultGovernanceBatch.rollback_status == "rolling_back",
                    ~unsettled_action_exists,
                ).update({
                    NoResultGovernanceBatch.status: "awaiting_retag",
                    NoResultGovernanceBatch.rollback_status: "pending",
                    NoResultGovernanceBatch.updated_at: current_time,
                }, synchronize_session=False)
                should_queue_retag = claimed == 1
            db.commit()
            if query.batch_id and should_queue_retag:
                retag_result = self.queue_retag_for_batch(
                    int(query.batch_id),
                    now=current_time,
                    cooldown_hours=cooldown_hours,
                )
            elif query.batch_id:
                retag_result = {
                    "batchId": int(query.batch_id),
                    "queued": False,
                    "reason": "batch_rollback_not_settled",
                }
            else:
                retag_result = {}
            return {
                "queryId": query_id,
                "batchId": query.batch_id,
                "rolledBackActions": rolled_back,
                "status": "rolled_back" if not failed else "partial_failed",
                "failedActions": failed,
                "retag": retag_result,
            }
        finally:
            db.close()

    def rollback_brand_date(
        self,
        brand: str,
        target_date: datetime.date,
        now: Optional[datetime.datetime] = None,
        cooldown_hours: int = 2,
        operator: str = "",
        reason: str = "",
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        start_at = datetime.datetime.combine(target_date, datetime.time.min)
        end_at = start_at + datetime.timedelta(days=1)
        db = SessionLocal()
        try:
            batch_ids = [
                int(row.id) for row in db.query(NoResultGovernanceBatch.id).filter(
                    NoResultGovernanceBatch.brand == brand,
                    NoResultGovernanceBatch.batch_date >= start_at,
                    NoResultGovernanceBatch.batch_date < end_at,
                ).order_by(NoResultGovernanceBatch.id.asc()).all()
            ]
            actions = db.query(NoResultGovernanceAction).filter(
                NoResultGovernanceAction.batch_id.in_(batch_ids),
                NoResultGovernanceAction.status == "success",
            ).all() if batch_ids else []
            self._assert_rollback_actions_allowed(actions)
        finally:
            db.close()

        results = [
            self.rollback_batch(
                batch_id,
                now=now,
                cooldown_hours=cooldown_hours,
                operator=operator,
                reason=reason,
                external_request_id=(
                    f"{external_request_id}:batch:{batch_id}"
                    if external_request_id
                    else None
                ),
            )
            for batch_id in batch_ids
        ]
        return {
            "brand": brand,
            "date": target_date.isoformat(),
            "batchCount": len(batch_ids),
            "rolledBackActions": sum(int(item.get("rolledBackActions") or 0) for item in results),
            "status": "rolled_back" if all(item.get("status") == "rolled_back" for item in results) else "partial_failed",
            "batches": results,
        }

    def approve_manual_action(
        self,
        action_id: int,
        operator: str,
        reason: str = "",
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            action = db.get(NoResultGovernanceAction, action_id)
            if not action:
                raise ValueError(f"No result governance action not found: {action_id}")
            if action.status != "manual_review":
                raise ValueError(f"Action is not waiting for manual review: {action_id}")

            query = db.get(NoResultQuery, action.query_id) if action.query_id else None
            batch = db.get(NoResultGovernanceBatch, action.batch_id) if action.batch_id else None
            assert_query_governance_allowed(str(action.brand or ""), str(action.query_text or ""))
            request_payload, durable_request_id = self._begin_action_execution(
                db,
                action,
                query,
                batch,
                self._loads(action.request_payload),
                external_request_id=external_request_id,
                expected_statuses=("manual_review",),
            )
            try:
                snapshot = self.kg_client.snapshot(request_payload)
                response = self._execute_kg_write(
                    action.brand,
                    action.query_text,
                    request_payload,
                    external_request_id=durable_request_id,
                )
                rollback_payload = self.kg_client.build_rollback(request_payload, snapshot, response)
            except Exception as exc:
                self._finish_action_exception(db, action, query, batch, exc)
                raise
            action_status = "success" if self._response_succeeded(response) else "failed"

            action.before_snapshot = json.dumps(snapshot, ensure_ascii=False) # type: ignore
            action.response_payload = json.dumps(response, ensure_ascii=False) # type: ignore
            action.rollback_payload = json.dumps(rollback_payload, ensure_ascii=False) # type: ignore
            action.status = action_status # type: ignore
            action.rollback_status = "pending" if action_status == "success" else "not_required" # type: ignore
            action.error_message = self._operator_note("approved", operator, reason, None if action_status == "success" else response.get("message")) # type: ignore
            action.updated_at = datetime.datetime.now() # type: ignore

            if query:
                query.status = "awaiting_retag" if action_status == "success" else "manual_required" # type: ignore
                query.updated_at = datetime.datetime.now() # type: ignore
            if batch and action_status == "success":
                batch.changed_queries = (batch.changed_queries or 0) + 1 # type: ignore
                batch.status = "awaiting_retag" # type: ignore
                batch.updated_at = datetime.datetime.now() # type: ignore
            elif batch:
                batch.status = "manual_required" # type: ignore
                batch.updated_at = datetime.datetime.now() # type: ignore

            db.commit()
            return {"actionId": action_id, "status": action_status}
        finally:
            db.close()

    def reject_manual_action(self, action_id: int, operator: str, reason: str) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            action = db.get(NoResultGovernanceAction, action_id)
            if not action:
                raise ValueError(f"No result governance action not found: {action_id}")
            if action.status != "manual_review":
                raise ValueError(f"Action is not waiting for manual review: {action_id}")

            current_time = datetime.datetime.now()
            operator_note = self._operator_note("rejected", operator, reason)
            claimed = db.query(NoResultGovernanceAction).filter(
                NoResultGovernanceAction.id == action_id,
                NoResultGovernanceAction.status == "manual_review",
            ).update({
                NoResultGovernanceAction.status: "rejected",
                NoResultGovernanceAction.rollback_status: "not_required",
                NoResultGovernanceAction.error_message: operator_note,
                NoResultGovernanceAction.updated_at: current_time,
            }, synchronize_session=False)
            if claimed != 1:
                db.rollback()
                db.expire_all()
                current = db.get(NoResultGovernanceAction, action_id)
                raise ValueError(f"Action was claimed concurrently from status: {current.status if current else 'missing'}")

            query = db.get(NoResultQuery, action.query_id) if action.query_id else None
            if query:
                query.status = "manual_required" # type: ignore
                query.updated_at = current_time # type: ignore
            db.commit()
            return {"actionId": action_id, "status": "rejected"}
        finally:
            db.close()

    def execute_pending_action(
        self,
        action_id: int,
        operator: str = "system",
        reason: str = "",
        now: Optional[datetime.datetime] = None,
        cooldown_hours: int = 2,
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        current_time = now or datetime.datetime.now()
        db = SessionLocal()
        try:
            action = db.get(NoResultGovernanceAction, action_id)
            if not action:
                raise ValueError(f"No result governance action not found: {action_id}")
            if action.status != "pending_execute":
                raise ValueError(f"Action is not waiting for execution: {action_id}")

            query = db.get(NoResultQuery, action.query_id) if action.query_id else None
            brand_config = self._get_brand_business_config(str(action.brand or ""))
            request_payload = self._loads(action.request_payload)
            risk_level = action.risk_level or request_payload.get("riskLevel") or request_payload.get("risk_level") or "low"
            max_risk_level = brand_config.get("no_result_auto_max_risk") or "high"
            action_type = request_payload.get("actionType") or request_payload.get("action_type") or action.action_type
            if query:
                self._record_agent_event(
                    db,
                    query,
                    stage="execution_request",
                    event_type="tool_request",
                    tool_name="execute_pending_action",
                    summary=f"请求执行 {action_type}",
                    payload={"actionId": action_id, "action": request_payload, "operator": operator, "reason": reason},
                )
            if not self._risk_allowed(str(risk_level), str(max_risk_level)):
                action.status = "manual_review" # type: ignore
                action.error_message = self._operator_note("execute_blocked", operator, reason, "risk level requires manual review") # type: ignore
                action.updated_at = current_time # type: ignore
                if query:
                    self._record_agent_event(
                        db,
                        query,
                        stage="execution_result",
                        event_type="tool_result",
                        tool_name="execute_pending_action",
                        summary="风险门控阻断执行",
                        payload={"actionId": action_id, "status": "manual_review", "reason": "risk_gate"},
                    )
                db.commit()
                return {"actionId": action_id, "status": "manual_review", "reason": "risk_gate"}
            if not self._real_write_authorized():
                action.error_message = self._operator_note("execute_blocked", operator, reason, "real write requires backend authorization") # type: ignore
                action.updated_at = current_time # type: ignore
                if query:
                    self._record_agent_event(
                        db,
                        query,
                        stage="execution_result",
                        event_type="tool_result",
                        tool_name="execute_pending_action",
                        summary="后端真实写授权未开启",
                        payload={"actionId": action_id, "status": "pending_execute", "reason": "real_write_not_authorized"},
                    )
                db.commit()
                return {"actionId": action_id, "status": "pending_execute", "reason": "real_write_not_authorized"}

            assert_query_governance_allowed(str(action.brand or ""), str(action.query_text or ""))
            batch = db.get(NoResultGovernanceBatch, action.batch_id) if action.batch_id else None
            request_payload, durable_request_id = self._begin_action_execution(
                db,
                action,
                query,
                batch,
                request_payload,
                now=current_time,
                external_request_id=external_request_id,
                expected_statuses=("pending_execute",),
            )
            try:
                snapshot = self.kg_client.snapshot(request_payload)
                response = self._execute_kg_write(
                    action.brand,
                    action.query_text,
                    request_payload,
                    external_request_id=durable_request_id,
                )
                rollback_payload = self.kg_client.build_rollback(request_payload, snapshot, response)
            except Exception as exc:
                self._finish_action_exception(db, action, query, batch, exc, now=current_time)
                raise
            action_status = "success" if self._response_succeeded(response) else "failed"
            action.before_snapshot = json.dumps(snapshot, ensure_ascii=False) # type: ignore
            action.response_payload = json.dumps(response, ensure_ascii=False) # type: ignore
            action.rollback_payload = json.dumps(rollback_payload, ensure_ascii=False) # type: ignore
            action.status = action_status # type: ignore
            action.rollback_status = "pending" if action_status == "success" else "not_required" # type: ignore
            action.error_message = self._operator_note("executed", operator, reason, None if action_status == "success" else response.get("message")) # type: ignore
            action.updated_at = current_time # type: ignore

            if query:
                if query.status != "execution_paused":
                    query.status = "awaiting_retag" if action_status == "success" else "manual_required" # type: ignore
                query.updated_at = current_time # type: ignore
                self._record_agent_event(
                    db,
                    query,
                    stage="execution_result",
                    event_type="tool_result",
                    tool_name="execute_pending_action",
                    summary=f"执行结果: {action_status}",
                    payload={"actionId": action_id, "status": action_status, "response": response},
                )
            if batch and action_status == "success":
                batch.changed_queries = (batch.changed_queries or 0) + 1 # type: ignore
                batch.status = "awaiting_retag" # type: ignore
                batch.updated_at = current_time # type: ignore
            elif batch:
                batch.status = "manual_required" # type: ignore
                batch.updated_at = current_time # type: ignore
            db.flush()
            has_pending_actions = bool(query and self._next_pending_action(db, int(query.id)))
            db.commit()
            if action_status == "success" and action.batch_id and not has_pending_actions:
                self.queue_retag_for_batch(int(action.batch_id), now=current_time, cooldown_hours=cooldown_hours)
            return {"actionId": action_id, "status": action_status}
        finally:
            db.close()

    def start_query_execution(
        self,
        query_id: int,
        operator: str,
        reason: str = "",
        now: Optional[datetime.datetime] = None,
        cooldown_hours: int = 2,
        external_request_id: Optional[str] = None,
        expected_brand: Optional[str] = None,
        expected_query_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._run_query_execution(
            query_id=query_id,
            operator=operator,
            reason=reason,
            start_status="execution_running",
            now=now,
            cooldown_hours=cooldown_hours,
            external_request_id=external_request_id,
            expected_brand=expected_brand,
            expected_query_text=expected_query_text,
        )

    def resume_query_execution(
        self,
        query_id: int,
        operator: str,
        reason: str = "",
        now: Optional[datetime.datetime] = None,
        cooldown_hours: int = 2,
        external_request_id: Optional[str] = None,
        expected_brand: Optional[str] = None,
        expected_query_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._run_query_execution(
            query_id=query_id,
            operator=operator,
            reason=reason,
            start_status="execution_running",
            now=now,
            cooldown_hours=cooldown_hours,
            external_request_id=external_request_id,
            expected_brand=expected_brand,
            expected_query_text=expected_query_text,
        )

    def pause_query_execution(
        self,
        query_id: int,
        operator: str,
        reason: str = "",
        now: Optional[datetime.datetime] = None,
    ) -> Dict[str, Any]:
        current_time = now or datetime.datetime.now()
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")
            self._paused_query_ids.add(query_id)
            query.status = "execution_paused" # type: ignore
            query.ai_reason = self._append_operator_note(query.ai_reason, self._operator_note("execution_paused", operator, reason)) # type: ignore
            query.updated_at = current_time # type: ignore
            db.commit()
            return self.get_query_execution_progress(query_id)
        finally:
            db.close()

    def get_query_execution_progress(self, query_id: int) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")
            actions = self._query_actions(db, query_id)
            pending_count = len([action for action in actions if action.status == "pending_execute"])
            success_count = len([action for action in actions if action.status == "success"])
            failed_count = len([action for action in actions if action.status in {"failed", "manual_review"}])
            return {
                "queryId": query_id,
                "status": self._query_execution_status(str(query.status or ""), pending_count, failed_count),
                "executedActions": success_count,
                "pendingActions": pending_count,
                "actions": [self._action_progress(action) for action in actions],
            }
        finally:
            db.close()

    def _run_query_execution(
        self,
        query_id: int,
        operator: str,
        reason: str,
        start_status: str,
        now: Optional[datetime.datetime],
        cooldown_hours: int,
        external_request_id: Optional[str],
        expected_brand: Optional[str],
        expected_query_text: Optional[str],
    ) -> Dict[str, Any]:
        current_time = now or datetime.datetime.now()
        executed = 0
        blocked = False
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")
            if (
                expected_brand is not None
                and str(query.brand or "").strip() != str(expected_brand).strip()
            ) or (
                expected_query_text is not None
                and str(query.query_text or "").strip() != str(expected_query_text).strip()
            ):
                raise ValueError("No Result query scope changed")
            if query.status == "execution_running":
                return self.get_query_execution_progress(query_id)
            self._paused_query_ids.discard(query_id)
            query.status = start_status # type: ignore
            query.ai_reason = self._append_operator_note( # type: ignore
                query.ai_reason,
                self._operator_note(
                    "execution_started",
                    operator,
                    reason,
                    f"gatewayRequestId={external_request_id}" if external_request_id else None,
                ),
            )
            query.updated_at = current_time # type: ignore
            db.commit()
        finally:
            db.close()

        while True:
            db = SessionLocal()
            try:
                query = db.get(NoResultQuery, query_id)
                if not query:
                    raise ValueError(f"No result query not found: {query_id}")
                if query.status == "execution_paused":
                    break
                action = self._next_pending_action(db, query_id)
                if not action:
                    self._finalize_query_execution(db, query, current_time)
                    db.commit()
                    break
                action_id = int(action.id)
            finally:
                db.close()

            action_external_request_id = external_request_id
            if external_request_id and executed > 0:
                action_external_request_id = f"{external_request_id}:action:{action_id}"
            result = self.execute_pending_action(
                action_id,
                operator=operator,
                reason=reason,
                now=current_time,
                cooldown_hours=cooldown_hours,
                external_request_id=action_external_request_id,
            )
            if result.get("status") == "success":
                executed += 1
                if query_id in self._paused_query_ids:
                    db = SessionLocal()
                    try:
                        query = db.get(NoResultQuery, query_id)
                        if query:
                            query.status = "execution_paused" # type: ignore
                            query.updated_at = current_time # type: ignore
                            db.commit()
                    finally:
                        db.close()
                    break
                continue
            blocked = True
            break

        progress = self.get_query_execution_progress(query_id)
        if blocked and progress["status"] not in {"completed", "paused"}:
            progress["status"] = "blocked"
        progress["executedActions"] = executed
        return progress

    def _finalize_query_execution(self, db: Any, query: NoResultQuery, current_time: datetime.datetime) -> None:
        actions = self._query_actions(db, int(query.id))
        if actions and all(action.status == "success" for action in actions):
            query.status = "awaiting_retag" # type: ignore
        elif any(action.status in {"failed", "manual_review"} for action in actions):
            query.status = "manual_required" # type: ignore
        elif any(action.status == "pending_execute" for action in actions):
            query.status = "pending_execute" # type: ignore
        query.updated_at = current_time # type: ignore

    @staticmethod
    def _append_operator_note(current: Optional[str], note: str) -> str:
        return f"{current}\n{note}" if current else note

    @staticmethod
    def _action_progress(action: NoResultGovernanceAction) -> Dict[str, Any]:
        return {
            "id": action.id,
            "status": action.status,
            "errorMessage": action.error_message,
            "updatedAt": action.updated_at.strftime("%Y-%m-%d %H:%M:%S") if action.updated_at else "",
        }

    @staticmethod
    def _query_execution_status(query_status: str, pending_count: int, failed_count: int) -> str:
        if query_status == "execution_paused":
            return "paused"
        if query_status == "execution_running":
            return "running"
        if pending_count == 0 and failed_count == 0:
            return "completed"
        if failed_count > 0:
            return "blocked"
        return "idle"

    @staticmethod
    def _query_actions(db: Any, query_id: int) -> List[NoResultGovernanceAction]:
        return db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.query_id == query_id,
        ).order_by(NoResultGovernanceAction.id.asc()).all()

    @staticmethod
    def _next_pending_action(db: Any, query_id: int) -> Optional[NoResultGovernanceAction]:
        return db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.query_id == query_id,
            NoResultGovernanceAction.status == "pending_execute",
        ).order_by(NoResultGovernanceAction.id.asc()).first()

    def queue_retag_for_batch(
        self,
        batch_id: int,
        now: Optional[datetime.datetime] = None,
        cooldown_hours: int = 2,
    ) -> Dict[str, Any]:
        current_time = now or datetime.datetime.now()
        db = SessionLocal()
        try:
            batch = db.get(NoResultGovernanceBatch, batch_id)
            if not batch:
                raise ValueError(f"No result governance batch not found: {batch_id}")

            success_actions = db.query(NoResultGovernanceAction).filter(
                NoResultGovernanceAction.batch_id == batch_id,
                NoResultGovernanceAction.status == "success",
            ).count()
            if success_actions == 0:
                batch.retag_status = "skipped" # type: ignore
                db.commit()
                return {"batchId": batch_id, "queued": False, "reason": "no_successful_graph_changes"}

            last_success = db.query(NoResultRetagTask).filter(
                NoResultRetagTask.brand == batch.brand,
                NoResultRetagTask.status == "success",
                NoResultRetagTask.triggered_at.isnot(None),
            ).order_by(NoResultRetagTask.triggered_at.desc()).first()
            earliest_run_at = current_time
            if last_success and last_success.triggered_at:
                cooled_at = last_success.triggered_at + datetime.timedelta(hours=cooldown_hours)
                if cooled_at > earliest_run_at:
                    earliest_run_at = cooled_at

            task = db.query(NoResultRetagTask).filter(
                NoResultRetagTask.brand == batch.brand,
                NoResultRetagTask.status == "pending",
            ).order_by(NoResultRetagTask.earliest_run_at.asc(), NoResultRetagTask.id.asc()).first()

            if task:
                batch_ids = self._loads_list(task.batch_ids)
                if str(batch_id) not in batch_ids:
                    batch_ids.append(str(batch_id))
                task.batch_ids = json.dumps(batch_ids, ensure_ascii=False) # type: ignore
                if task.earliest_run_at and task.earliest_run_at > earliest_run_at:
                    task.earliest_run_at = earliest_run_at # type: ignore
            else:
                task = NoResultRetagTask(
                    brand=batch.brand,
                    batch_ids=json.dumps([str(batch_id)], ensure_ascii=False),
                    status="pending",
                    earliest_run_at=earliest_run_at,
                )
                db.add(task)
                db.flush()

            batch.retag_status = "cooling_down" if earliest_run_at > current_time else "pending" # type: ignore
            batch.updated_at = current_time # type: ignore
            db.commit()
            return {
                "batchId": batch_id,
                "taskId": task.id,
                "queued": True,
                "earliestRunAt": earliest_run_at.isoformat(),
            }
        finally:
            db.close()

    def run_due_retag_tasks(
        self,
        ops_service: Optional[Any] = None,
        now: Optional[datetime.datetime] = None,
    ) -> Dict[str, Any]:
        current_time = now or datetime.datetime.now()
        if ops_service is None:
            from src.integrations.operational_service import OperationalService
            ops_service = OperationalService()

        db = SessionLocal()
        try:
            brands = [row[0] for row in db.query(NoResultRetagTask.brand).filter(
                NoResultRetagTask.status == "pending",
                NoResultRetagTask.earliest_run_at <= current_time,
            ).distinct().order_by(NoResultRetagTask.brand.asc()).all()]

            checked = 0
            success = 0
            failed = 0
            for brand in brands:
                db.rollback()
                guard_row = db.query(NoResultRetagTask.id).filter(
                    NoResultRetagTask.brand == brand,
                    NoResultRetagTask.status == "pending",
                    NoResultRetagTask.earliest_run_at <= current_time,
                ).order_by(NoResultRetagTask.id.asc()).first()
                if guard_row is None:
                    continue
                guard_id = int(guard_row[0])
                guarded = db.query(NoResultRetagTask).filter(
                    NoResultRetagTask.id == guard_id,
                    NoResultRetagTask.status == "pending",
                ).update({
                    NoResultRetagTask.status: NoResultRetagTask.status,
                }, synchronize_session=False)
                if guarded != 1:
                    db.rollback()
                    continue
                db.expire_all()
                if db.query(NoResultRetagTask.id).filter(
                    NoResultRetagTask.brand == brand,
                    NoResultRetagTask.status == "executing",
                ).first():
                    db.rollback()
                    continue
                response_payload, external_request_id = self._payload_with_external_request_id(
                    {}
                )
                encoded_response = json.dumps(response_payload, ensure_ascii=False)
                claimed = db.query(NoResultRetagTask).filter(
                    NoResultRetagTask.brand == brand,
                    NoResultRetagTask.status == "pending",
                    NoResultRetagTask.earliest_run_at <= current_time,
                ).update({
                    NoResultRetagTask.status: "executing",
                    NoResultRetagTask.triggered_at: current_time,
                    NoResultRetagTask.updated_at: current_time,
                    NoResultRetagTask.error_message: None,
                    NoResultRetagTask.response_payload: encoded_response,
                }, synchronize_session=False)
                if claimed == 0:
                    db.rollback()
                    continue
                tasks = db.query(NoResultRetagTask).filter(
                    NoResultRetagTask.brand == brand,
                    NoResultRetagTask.status == "executing",
                    NoResultRetagTask.response_payload == encoded_response,
                ).order_by(NoResultRetagTask.id.asc()).all()
                batch_ids = sorted({
                    int(item)
                    for task in tasks
                    for item in self._loads_list(task.batch_ids)
                    if str(item).isdigit()
                })
                for batch_id in batch_ids:
                    batch = db.get(NoResultGovernanceBatch, batch_id)
                    if batch:
                        batch.retag_status = "running" # type: ignore
                        batch.updated_at = current_time # type: ignore
                db.commit()
                checked += len(tasks)
                try:
                    retag_products = ops_service.retag_products
                    parameters = inspect.signature(retag_products).parameters
                    accepts_external_id = "external_request_id" in parameters or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters.values()
                    )
                    if accepts_external_id:
                        ok = bool(retag_products(
                            brand,
                            external_request_id=external_request_id,
                        ))
                    else:
                        ok = bool(retag_products(brand))
                except Exception as exc:
                    failed += len(tasks)
                    response_payload.update({
                        "success": False,
                        "executionUnknown": True,
                        "error": str(exc),
                    })
                    for task in tasks:
                        task.status = "execution_unknown" # type: ignore
                        task.error_message = str(exc) # type: ignore
                        task.response_payload = json.dumps(response_payload, ensure_ascii=False) # type: ignore
                        task.updated_at = current_time # type: ignore
                    for batch_id in batch_ids:
                        batch = db.get(NoResultGovernanceBatch, batch_id)
                        if batch:
                            batch.retag_status = "execution_unknown" # type: ignore
                            batch.updated_at = current_time # type: ignore
                    db.commit()
                    continue

                if ok:
                    success += len(tasks)
                    response_payload.update({"success": True, "executionUnknown": False})
                    for task in tasks:
                        task.status = "success" # type: ignore
                        task.response_payload = json.dumps(response_payload, ensure_ascii=False) # type: ignore
                        task.updated_at = current_time # type: ignore
                    self._mark_batches_retagged(db, batch_ids, current_time)
                else:
                    failed += len(tasks)
                    response_payload.update({"success": False, "executionUnknown": False})
                    for task in tasks:
                        task.status = "failed" # type: ignore
                        task.response_payload = json.dumps(response_payload, ensure_ascii=False) # type: ignore
                        task.updated_at = current_time # type: ignore
                    self._mark_batches_retag_failed(db, batch_ids, "retag failed", current_time)
                db.commit()

            return {"checked": checked, "success": success, "failed": failed}
        finally:
            db.close()

    def review_query_recall(
        self,
        query_id: int,
        ops_service: Optional[Any] = None,
        top_n: int = 16,
    ) -> Dict[str, Any]:
        if ops_service is None:
            from src.integrations.operational_service import OperationalService
            ops_service = OperationalService()

        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                raise ValueError(f"No result query not found: {query_id}")

            products = ops_service.search_products(query.brand, query.query_text, page_size=top_n)
            top_products = products[:top_n]
            result_count = len(products)
            query.after_result_count = result_count # type: ignore
            query.reviewed_at = datetime.datetime.now() # type: ignore
            if result_count > 0:
                query.status = "completed" # type: ignore
                review_reason = "二次复查已有召回结果"
            else:
                query.status = "manual_required" # type: ignore
                review_reason = "二次复查仍无召回结果"

            raw_payload = self._loads(query.raw_payload)
            raw_payload["review"] = {
                "afterResultCount": result_count,
                "topN": top_n,
                "topProducts": self._summarize_products(top_products),
                "reason": review_reason,
            }
            query.raw_payload = json.dumps(raw_payload, ensure_ascii=False) # type: ignore
            db.commit()
            return {
                "queryId": query_id,
                "status": query.status,
                "afterResultCount": result_count,
                "reason": review_reason,
            }
        finally:
            db.close()

    def run_governance_cycle(
        self,
        intent_agent: Optional[Any] = None,
        diagnosis_agent: Optional[Any] = None,
        ops_service: Optional[Any] = None,
        now: Optional[datetime.datetime] = None,
        review_wait_minutes: Optional[int] = None,
        limit: int = 50,
        model_config_snapshot: Optional[NodeConfigSnapshot] = None,
    ) -> Dict[str, Any]:
        """按 Query 当前状态推进无结果词治理主循环。"""
        current_time = now or datetime.datetime.now()
        stats = {
            "processed": 0,
            "ignored": 0,
            "manualRequired": 0,
            "executedActions": 0,
            "queuedRetagBatches": 0,
            "retagSuccess": 0,
            "retagFailed": 0,
            "reviewed": 0,
            "failed": 0,
        }

        db = SessionLocal()
        try:
            no_result_snapshot = model_config_snapshot or capture_active_snapshot(
                db,
                node_keys=("no_result.intent", "no_result.diagnosis"),
            )
            get_snapshot_node_config(
                no_result_snapshot,
                "no_result.intent",
                required_channel="rd_llm",
            )
            get_snapshot_node_config(
                no_result_snapshot,
                "no_result.diagnosis",
                required_channel="rd_llm",
            )
            query_ids = [
                row.id for row in db.query(NoResultQuery.id).filter(
                    NoResultQuery.status.in_(["pending", "pending_context", "pending_diagnosis", "pending_graph_fix"])
                ).order_by(NoResultQuery.updated_at.asc(), NoResultQuery.created_at.asc()).limit(limit).all()
            ]
        finally:
            db.close()

        touched_batch_ids: set[int] = set()
        for query_id in query_ids:
            stats["processed"] += 1
            try:
                status = self._get_query_status(query_id)
                diagnosis_result: Dict[str, Any] = {}
                brand_config = self._get_query_business_config(query_id)

                if status == "pending":
                    intent = self.filter_query_intent(
                        query_id,
                        agent=intent_agent,
                        now=current_time,
                        ignore_ttl_days=int(brand_config.get("no_result_ignore_ttl_days") or 30),
                        model_config_snapshot=no_result_snapshot,
                    )
                    if intent.get("decision") == "ignore":
                        stats["ignored"] += 1
                        continue
                    status = self._get_query_status(query_id)

                if status == "pending_context":
                    tags = self.segment_query(query_id)
                    graph_context = self.fetch_graph_context(query_id, candidate_tags=tags)
                    diagnosis_result = self.diagnose_query(
                        query_id,
                        graph_context=graph_context,
                        agent=diagnosis_agent,
                        max_rounds=int(brand_config.get("no_result_agent_max_rounds") or 4),
                        model_config_snapshot=no_result_snapshot,
                    )
                    status = self._get_query_status(query_id)

                if status == "pending_diagnosis":
                    diagnosis_result = self.diagnose_query(
                        query_id,
                        agent=diagnosis_agent,
                        max_rounds=int(brand_config.get("no_result_agent_max_rounds") or 4),
                        model_config_snapshot=no_result_snapshot,
                    )
                    status = self._get_query_status(query_id)

                if status == "pending_graph_fix":
                    plan = diagnosis_result or self._get_query_strategy(query_id)
                    if "executionMode" not in plan and "execution_mode" not in plan:
                        plan["executionMode"] = brand_config.get("no_result_execution_mode") or "dry_run"
                    if "maxRiskLevel" not in plan and "max_risk_level" not in plan:
                        plan["maxRiskLevel"] = brand_config.get("no_result_auto_max_risk") or "high"
                    execution = self.execute_agent_plan(query_id, plan)
                    stats["executedActions"] += int(execution.get("executedActions") or 0)
                    batch_id = self._get_query_batch_id(query_id)
                    if batch_id:
                        touched_batch_ids.add(batch_id)

                final_status = self._get_query_status(query_id)
                if final_status == "manual_required":
                    stats["manualRequired"] += 1
            except Exception:
                stats["failed"] += 1

        for batch_id in sorted(touched_batch_ids):
            try:
                batch_config = self._get_batch_business_config(batch_id)
                queued = self.queue_retag_for_batch(
                    batch_id,
                    now=current_time,
                    cooldown_hours=int(batch_config.get("no_result_retag_cooldown_hours") or 2),
                )
                if queued.get("queued"):
                    stats["queuedRetagBatches"] += 1
            except Exception:
                stats["failed"] += 1

        retag_result = self.run_due_retag_tasks(ops_service=ops_service, now=current_time)
        stats["retagSuccess"] = int(retag_result.get("success") or 0)
        stats["retagFailed"] = int(retag_result.get("failed") or 0)

        db = SessionLocal()
        try:
            review_ids = [
                row.id for row in db.query(NoResultQuery.id).filter(
                    NoResultQuery.status == "retagged_pending_review",
                ).order_by(NoResultQuery.updated_at.asc(), NoResultQuery.created_at.asc()).limit(limit).all()
            ]
        finally:
            db.close()

        for query_id in review_ids:
            try:
                brand_config = self._get_query_business_config(query_id)
                wait_minutes = review_wait_minutes if review_wait_minutes is not None else int(brand_config.get("no_result_review_wait_minutes") or 30)
                updated_at = self._get_query_updated_at(query_id)
                if updated_at and updated_at > current_time - datetime.timedelta(minutes=wait_minutes):
                    continue
                self.review_query_recall(
                    query_id,
                    ops_service=ops_service,
                    top_n=int(brand_config.get("no_result_top_n") or 16),
                )
                stats["reviewed"] += 1
            except Exception:
                stats["failed"] += 1

        return stats

    @staticmethod
    def _record_agent_event(
        db: Any,
        query: NoResultQuery,
        stage: str,
        event_type: str,
        summary: str,
        payload: Optional[Dict[str, Any]] = None,
        tool_name: Optional[str] = None,
    ) -> None:
        db.add(NoResultAgentEvent(
            query_id=query.id,
            batch_id=query.batch_id,
            brand=query.brand,
            query_text=query.query_text,
            stage=stage,
            event_type=event_type,
            tool_name=tool_name,
            summary=summary,
            payload=json.dumps(payload or {}, ensure_ascii=False),
        ))

    @staticmethod
    def _summarize_graph_context(context: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "tagCandidates": [
                {
                    "candidate": item.get("candidate"),
                    "resultCount": NoResultGovernanceService._result_count(item.get("result")),
                }
                for item in context.get("tags", [])
                if isinstance(item, dict)
            ],
            "relationshipCandidates": [
                {
                    "candidate": item.get("candidate"),
                    "resultCount": NoResultGovernanceService._result_count(item.get("result")),
                }
                for item in context.get("relationships", [])
                if isinstance(item, dict)
            ],
            "dimensionResultCount": sum(
                NoResultGovernanceService._result_count(item)
                for item in context.get("dimensions", [])
            ),
        }

    @staticmethod
    def _result_count(result: Any) -> int:
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                rows = data.get("rows") or data.get("list")
                if isinstance(rows, list):
                    return len(rows)
                return 1
            if data:
                return 1
        return 0

    @staticmethod
    def _normalize_diagnosis_metadata(result: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(result or {})
        output_type = normalized.get("outputType") or normalized.get("output_type")
        if output_type == "strategy":
            normalized.setdefault("attributionType", "evidence_insufficient")
            normalized.setdefault("evidenceSummary", normalized.get("reason") or "基于候选标签与图谱上下文生成")
        actions = normalized.get("actions")
        if isinstance(actions, list):
            normalized["actions"] = [
                NoResultGovernanceService._normalize_plan_action(action, normalized, "dry_run")
                for action in actions
                if isinstance(action, dict)
            ]
        return normalized

    @staticmethod
    def _normalize_plan_action(action: Dict[str, Any], plan: Dict[str, Any], execution_mode: str) -> Dict[str, Any]:
        normalized = dict(action)
        action_type = str(normalized.get("actionType") or normalized.get("action_type") or "")
        if "actionType" not in normalized and action_type:
            normalized["actionType"] = action_type
        normalized.setdefault("riskLevel", NoResultGovernanceService._default_action_risk(action_type))
        normalized.setdefault("evidence", normalized.get("reason") or plan.get("evidenceSummary") or plan.get("reason") or "Agent 基于图谱上下文判断")
        normalized.setdefault("expectedRecallImprovement", "提升该 Query 对目标商品的召回概率")
        normalized.setdefault("requiresHumanConfirmation", execution_mode == "dry_run" or normalized.get("riskLevel") == "high")
        normalized.setdefault("riskExplanation", NoResultGovernanceService._action_risk_explanation(action_type, str(normalized.get("riskLevel") or "medium")))
        normalized.setdefault("rollbackSuggestion", NoResultGovernanceService._action_rollback_suggestion(action_type))
        return normalized

    @staticmethod
    def _default_action_risk(action_type: str) -> str:
        if action_type in {"delete_relationship", "update_relationship", "update_tag", "blacklist_tag", "unblacklist_tag"}:
            return "high"
        if action_type == "add_relationship":
            return "medium"
        return "low"

    @staticmethod
    def _action_risk_explanation(action_type: str, risk_level: str) -> str:
        if action_type in {"delete_relationship", "update_relationship"}:
            return "关系删改会影响多个 Query 的召回，需要确认上下游影响"
        if action_type in {"update_tag", "blacklist_tag", "unblacklist_tag"}:
            return "标签属性变更会影响关联商品和历史关系"
        if risk_level == "high":
            return "高风险动作需要人工确认"
        return "新增类动作影响范围相对可控"

    @staticmethod
    def _action_rollback_suggestion(action_type: str) -> str:
        if action_type == "add_tag":
            return "如召回异常，可拉黑或删除新增标签影响"
        if action_type == "add_relationship":
            return "如召回异常，可删除新增关系"
        if action_type in {"update_relationship", "delete_relationship"}:
            return "如召回异常，按执行前快照恢复原关系"
        if action_type in {"update_tag", "blacklist_tag", "unblacklist_tag"}:
            return "如召回异常，按执行前快照恢复标签属性"
        return "按回滚 payload 执行补偿"

    @staticmethod
    def _loads(value: Optional[str]) -> Dict[str, Any]:
        if not value:
            return {}
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _payload_with_external_request_id(
        payload: Dict[str, Any],
        external_request_id: Optional[str] = None,
    ) -> tuple[Dict[str, Any], str]:
        normalized = dict(payload)
        durable_request_id = str(
            external_request_id
            or normalized.get("externalRequestId")
            or normalized.get("external_request_id")
            or uuid.uuid4()
        )
        normalized.pop("external_request_id", None)
        normalized["externalRequestId"] = durable_request_id
        return normalized, durable_request_id

    def _begin_action_execution(
        self,
        db: Any,
        action: NoResultGovernanceAction,
        query: Optional[NoResultQuery],
        batch: Optional[NoResultGovernanceBatch],
        payload: Dict[str, Any],
        *,
        now: Optional[datetime.datetime] = None,
        external_request_id: Optional[str] = None,
        expected_statuses: Optional[tuple[str, ...]] = None,
    ) -> tuple[Dict[str, Any], str]:
        current_time = now or datetime.datetime.now()
        durable_payload, durable_request_id = self._payload_with_external_request_id(
            payload,
            external_request_id,
        )
        encoded_payload = json.dumps(durable_payload, ensure_ascii=False)
        if expected_statuses and action.id is not None:
            claimed = db.query(NoResultGovernanceAction).filter(
                NoResultGovernanceAction.id == action.id,
                NoResultGovernanceAction.status.in_(expected_statuses),
            ).update({
                NoResultGovernanceAction.request_payload: encoded_payload,
                NoResultGovernanceAction.status: "executing",
                NoResultGovernanceAction.rollback_status: "not_required",
                NoResultGovernanceAction.error_message: None,
                NoResultGovernanceAction.updated_at: current_time,
            }, synchronize_session=False)
            if claimed != 1:
                db.rollback()
                db.expire_all()
                current = db.get(NoResultGovernanceAction, action.id)
                raise ValueError(f"Action was claimed concurrently from status: {current.status if current else 'missing'}")
        else:
            action.request_payload = encoded_payload # type: ignore
            action.status = "executing" # type: ignore
            action.rollback_status = "not_required" # type: ignore
            action.error_message = None # type: ignore
            action.updated_at = current_time # type: ignore
        if query:
            query.status = "execution_running" # type: ignore
            query.updated_at = current_time # type: ignore
        if batch:
            batch.status = "execution_running" # type: ignore
            batch.updated_at = current_time # type: ignore
        db.commit()
        db.refresh(action)
        return durable_payload, durable_request_id

    def _finish_action_exception(
        self,
        db: Any,
        action: NoResultGovernanceAction,
        query: Optional[NoResultQuery],
        batch: Optional[NoResultGovernanceBatch],
        exc: Exception,
        *,
        now: Optional[datetime.datetime] = None,
    ) -> None:
        current_time = now or datetime.datetime.now()
        unknown = True
        action.status = "execution_unknown" # type: ignore
        action.rollback_status = "not_required" # type: ignore
        action.error_message = str(exc) # type: ignore
        response_payload = self._loads(action.response_payload)
        response_payload.update({"error": str(exc), "executionUnknown": unknown})
        action.response_payload = json.dumps(response_payload, ensure_ascii=False) # type: ignore
        action.updated_at = current_time # type: ignore
        if query:
            query.status = "execution_unknown" # type: ignore
            query.updated_at = current_time # type: ignore
        if batch:
            batch.status = "execution_unknown" # type: ignore
            batch.updated_at = current_time # type: ignore
        db.commit()

    def _begin_rollback_execution(
        self,
        db: Any,
        action: NoResultGovernanceAction,
        query: Optional[NoResultQuery],
        batch: Optional[NoResultGovernanceBatch],
        payload: Dict[str, Any],
        *,
        now: Optional[datetime.datetime] = None,
        external_request_id: Optional[str] = None,
        expected_statuses: tuple[str, ...] = ("pending",),
    ) -> tuple[Dict[str, Any], str]:
        current_time = now or datetime.datetime.now()
        durable_payload, durable_request_id = self._payload_with_external_request_id(
            payload,
            external_request_id,
        )
        encoded_payload = json.dumps(durable_payload, ensure_ascii=False)
        claimed = db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.id == action.id,
            NoResultGovernanceAction.rollback_status.in_(expected_statuses),
        ).update({
            NoResultGovernanceAction.rollback_payload: encoded_payload,
            NoResultGovernanceAction.rollback_status: "rolling_back",
            NoResultGovernanceAction.error_message: None,
            NoResultGovernanceAction.updated_at: current_time,
        }, synchronize_session=False)
        if claimed != 1:
            db.rollback()
            db.expire_all()
            current = db.get(NoResultGovernanceAction, action.id)
            raise ValueError(f"Rollback was claimed concurrently from status: {current.rollback_status if current else 'missing'}")
        if query:
            query.status = "rolling_back" # type: ignore
            query.updated_at = current_time # type: ignore
        if batch:
            batch.status = "rolling_back" # type: ignore
            batch.rollback_status = "rolling_back" # type: ignore
            batch.updated_at = current_time # type: ignore
        db.commit()
        db.refresh(action)
        return durable_payload, durable_request_id

    def _finish_rollback_exception(
        self,
        db: Any,
        action: NoResultGovernanceAction,
        query: Optional[NoResultQuery],
        batch: Optional[NoResultGovernanceBatch],
        exc: Exception,
        *,
        now: Optional[datetime.datetime] = None,
    ) -> None:
        current_time = now or datetime.datetime.now()
        action.rollback_status = "rollback_unknown" # type: ignore
        action.error_message = str(exc) # type: ignore
        action.updated_at = current_time # type: ignore
        if query:
            query.status = "rollback_unknown" # type: ignore
            query.updated_at = current_time # type: ignore
        if batch:
            batch.status = "rollback_unknown" # type: ignore
            batch.rollback_status = "rollback_unknown" # type: ignore
            batch.updated_at = current_time # type: ignore
        db.commit()

    @staticmethod
    def _response_succeeded(response: Dict[str, Any]) -> bool:
        return response.get("code") in {0, 1, 200} or response.get("message") in {
            "success",
            "dry_run_success",
        }

    def _execute_kg_write(
        self,
        brand: object,
        query_text: object,
        payload: Dict[str, Any],
        *,
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        assert_query_governance_allowed(str(brand or ""), str(query_text or ""))
        execute = self.kg_client.execute
        parameters = inspect.signature(execute).parameters
        accepts_external_id = "external_request_id" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if external_request_id and accepts_external_id:
            return execute(payload, external_request_id=external_request_id)
        return execute(payload)

    @staticmethod
    def _rollback_write_not_required(action: NoResultGovernanceAction) -> bool:
        return (
            str(action.action_type or "").startswith("rollback_")
            or str(action.rollback_status or "") in {"success", "not_required"}
        )

    @staticmethod
    def _assert_rollback_actions_allowed(actions: Iterable[NoResultGovernanceAction]) -> None:
        for action in actions:
            if NoResultGovernanceService._rollback_write_not_required(action):
                continue
            assert_query_governance_allowed(str(action.brand or ""), str(action.query_text or ""))

    @staticmethod
    def segment_text(text: str) -> List[str]:
        query = (text or "").strip()
        if not query:
            return []
        try:
            import jieba  # type: ignore
            tags = [item.strip() for item in jieba.lcut(query) if item.strip()]
        except Exception:
            tags = NoResultGovernanceService._fallback_segment(query)
        if query not in tags:
            tags.append(query)
        return list(dict.fromkeys(tags))

    @staticmethod
    def _fallback_segment(text: str) -> List[str]:
        known_terms = [
            "粉红色", "粉色", "红色", "白色", "黑色", "蓝色", "绿色",
            "大衣", "外套", "毛衣", "衬衫", "裤子", "裙子", "背包", "天幕",
        ]
        result = []
        remaining = text
        for term in sorted(known_terms, key=len, reverse=True):
            if term in remaining:
                result.append(term)
                remaining = remaining.replace(term, " ")
        result.extend([part for part in re.split(r"\s+", remaining) if part])
        return result or [text]

    @staticmethod
    def _loads_list(value: Optional[str]) -> List[str]:
        if not value:
            return []
        try:
            loaded = json.loads(value)
            if isinstance(loaded, list):
                return [str(item) for item in loaded]
            return []
        except json.JSONDecodeError:
            return []

    @classmethod
    def _existing_action_fingerprints(cls, db: Any, batch_id: Optional[int]) -> set[str]:
        if not batch_id:
            return set()
        actions = db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.batch_id == batch_id,
            NoResultGovernanceAction.status.in_((
                "success",
                "executing",
                "execution_unknown",
            )),
        ).all()
        fingerprints = set()
        for action in actions:
            payload = cls._loads(action.request_payload)
            if payload:
                fingerprints.add(cls._action_fingerprint(payload))
        return fingerprints

    @classmethod
    def _find_occupied_action(
        cls,
        db: Any,
        batch_id: Optional[int],
        fingerprint: str,
    ) -> Optional[NoResultGovernanceAction]:
        if not batch_id:
            return None
        actions = db.query(NoResultGovernanceAction).filter(
            NoResultGovernanceAction.batch_id == batch_id,
            NoResultGovernanceAction.status.in_((
                "success",
                "executing",
                "execution_unknown",
            )),
        ).all()
        matching = [
            action
            for action in actions
            if cls._action_fingerprint(cls._loads(action.request_payload)) == fingerprint
        ]
        priority = {"execution_unknown": 0, "executing": 1, "success": 2}
        return min(matching, key=lambda action: priority.get(str(action.status), 9), default=None)

    @staticmethod
    def _action_fingerprint(action: Dict[str, Any]) -> str:
        payload = {
            "actionType": action.get("actionType") or action.get("action_type"),
            "targetInterface": action.get("targetInterface") or action.get("target_interface"),
            "target": action.get("target") or {},
            "params": action.get("params") or {},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _risk_allowed(risk_level: str, max_risk_level: str) -> bool:
        order = {"low": 1, "medium": 2, "high": 3}
        return order.get(str(risk_level or "low").lower(), 3) <= order.get(str(max_risk_level or "high").lower(), 3)

    def _execution_mode(self, plan: Dict[str, Any]) -> str:
        explicit = plan.get("executionMode") or plan.get("execution_mode")
        if explicit in {"dry_run", "real_write"}:
            return str(explicit)
        return "real_write" if self._kg_client_injected else "dry_run"

    def _real_write_authorized(self) -> bool:
        return self._kg_client_injected or os.getenv("NO_RESULT_KG_REAL_WRITE", "").lower() in {"1", "true", "yes"}

    @staticmethod
    def _is_query_action(action_type: str) -> bool:
        return action_type in {"query_tag", "query_relationship", "query_dimension"}

    @staticmethod
    def _summarize_products(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        summary = []
        for product in products:
            summary.append({
                "productId": product.get("productId") or product.get("spuId") or product.get("skuId"),
                "skuId": product.get("skuId"),
                "productName": product.get("productName") or product.get("name"),
            })
        return summary

    @staticmethod
    def _operator_note(action: str, operator: str, reason: str = "", error: Optional[str] = None) -> str:
        parts = [f"{action} by {operator or 'unknown'}"]
        if reason:
            parts.append(reason)
        if error:
            parts.append(str(error))
        return " | ".join(parts)

    def _detect_rollback_conflict(
        self,
        action: Dict[str, Any],
        before_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        detector = getattr(self.kg_client, "detect_conflict", None)
        if not callable(detector):
            return {"hasConflict": False}
        try:
            result = detector(action, before_snapshot)
            if isinstance(result, dict):
                return result
            return {"hasConflict": bool(result)}
        except Exception as exc:
            return {"hasConflict": True, "reason": f"rollback conflict check failed: {exc}"}

    @staticmethod
    def _record_rollback_action(
        db: Any,
        source_action: NoResultGovernanceAction,
        rollback_payload: Dict[str, Any],
        response: Dict[str, Any],
        now: datetime.datetime,
    ) -> None:
        action_type = rollback_payload.get("actionType") or f"rollback_{source_action.action_type}"
        ok = response.get("code") in {0, 1, 200} or response.get("message") in {"success", "dry_run_success"}
        db.add(NoResultGovernanceAction(
            query_id=source_action.query_id,
            batch_id=source_action.batch_id,
            brand=source_action.brand,
            query_text=source_action.query_text,
            action_type=action_type,
            target_interface=str(rollback_payload.get("targetInterface") or action_type),
            request_payload=json.dumps(rollback_payload, ensure_ascii=False),
            response_payload=json.dumps(response, ensure_ascii=False),
            before_snapshot=source_action.before_snapshot,
            rollback_payload="{}",
            status="success" if ok else "failed",
            rollback_status="not_required",
            risk_level=source_action.risk_level,
            agent_reason=f"rollback for action {source_action.id}",
            error_message=None if ok else response.get("message") or "rollback failed",
            created_at=now,
            updated_at=now,
        ))

    @staticmethod
    def _active_ignored_queries(brand: str, now: datetime.datetime) -> set[str]:
        db = SessionLocal()
        try:
            rows = db.query(NoResultIgnoreRecord.query_text).filter(
                NoResultIgnoreRecord.brand == brand,
                NoResultIgnoreRecord.expires_at > now,
            ).all()
            return {str(row.query_text) for row in rows if row.query_text}
        finally:
            db.close()

    @staticmethod
    def _get_query_status(query_id: int) -> str:
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            return str(query.status) if query else ""
        finally:
            db.close()

    @staticmethod
    def _get_query_batch_id(query_id: int) -> Optional[int]:
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            return int(query.batch_id) if query and query.batch_id else None
        finally:
            db.close()

    @staticmethod
    def _get_query_updated_at(query_id: int) -> Optional[datetime.datetime]:
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            return query.updated_at if query else None
        finally:
            db.close()

    @staticmethod
    def _get_query_business_config(query_id: int) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            brand = query.brand if query else ""
        finally:
            db.close()
        if not brand:
            return {}
        from src.config import get_brand_business_config
        return get_brand_business_config(brand)

    @staticmethod
    def _get_batch_business_config(batch_id: int) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            batch = db.get(NoResultGovernanceBatch, batch_id)
            brand = batch.brand if batch else ""
        finally:
            db.close()
        if not brand:
            return {}
        from src.config import get_brand_business_config
        return get_brand_business_config(brand)

    @staticmethod
    def _get_brand_business_config(brand: str) -> Dict[str, Any]:
        if not brand:
            return {}
        from src.config import get_brand_business_config
        return get_brand_business_config(brand)

    @classmethod
    def _get_query_strategy(cls, query_id: int) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            query = db.get(NoResultQuery, query_id)
            if not query:
                return {}
            return cls._loads(query.final_strategy)
        finally:
            db.close()

    @staticmethod
    def _mark_batches_retagged(db: Any, batch_ids: List[int], now: datetime.datetime) -> None:
        for batch_id in batch_ids:
            batch = db.get(NoResultGovernanceBatch, batch_id)
            if batch:
                batch.retag_status = "success" # type: ignore
                batch.status = "retagged_pending_review" # type: ignore
                batch.updated_at = now # type: ignore
            queries = db.query(NoResultQuery).filter(NoResultQuery.batch_id == batch_id).all()
            for query in queries:
                if query.status in {"awaiting_retag", "pending"}:
                    query.status = "retagged_pending_review" # type: ignore
                    query.updated_at = now # type: ignore

    @staticmethod
    def _mark_batches_retag_failed(
        db: Any,
        batch_ids: List[int],
        reason: str,
        now: datetime.datetime,
    ) -> None:
        for batch_id in batch_ids:
            batch = db.get(NoResultGovernanceBatch, batch_id)
            if batch:
                batch.retag_status = "failed" # type: ignore
                batch.summary_data = json.dumps({"retagError": reason}, ensure_ascii=False) # type: ignore
                batch.updated_at = now # type: ignore
