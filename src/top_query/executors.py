import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from .policy import ActionPolicy
from .schemas import (
    ActionCandidate,
    ExecutorResult,
    WORLD_KNOWLEDGE_FACTOR_TYPES,
    WorldKnowledgeDecision,
)
from src.llm.node_config import (
    NodeConfigSnapshot,
    ResolvedNodeConfig,
    get_snapshot_node_config,
    model_audit_fields,
)
from src.llm.rd_client import FallbackChatModel, build_rd_chat_model, build_rd_chat_with_fallback
from .recall_scoring import build_recall_assessment


def _build_snapshot_rd_llm(
    config: ResolvedNodeConfig,
    *,
    timeout: float,
    temperature: float,
) -> Optional[Any]:
    primary = build_rd_chat_model(
        timeout=timeout,
        default_model=config.primary_model,
        temperature=temperature,
        max_retries=0,
    )
    if primary is None:
        return None
    fallback = None
    if config.fallback_model:
        fallback = build_rd_chat_model(
            timeout=timeout,
            default_model=config.fallback_model,
            temperature=temperature,
            max_retries=0,
        )
    return FallbackChatModel(primary=primary, fallback=fallback)


class RecallExecutor:
    def __init__(self, low_result_threshold: int = 4, drop_ratio_threshold: float = 0.4):
        self.low_result_threshold = low_result_threshold
        self.drop_ratio_threshold = drop_ratio_threshold

    def execute(self, context: Dict[str, Any]) -> ExecutorResult:
        context = _normalize_context(context)
        raw_result_count = context.get("result_count")
        result_count_known = context.get("result_count_known")
        if result_count_known is None:
            result_count_known = raw_result_count is not None
        if result_count_known is False or raw_result_count is None:
            return ExecutorResult(
                check_item="recall",
                status="check_failed",
                root_cause="search_evidence_unavailable",
                recommended_action="cannot_evaluate",
                actions=[],
                kg_action_intents=[],
                evidence={
                    "result_count": None,
                    "result_count_known": False,
                    "failure_reason": context.get("search_failure_reason"),
                },
                conversion_hypothesis="召回证据缺失时不能判断转化影响",
                expected_impact="等待可信搜索证据后重新诊断",
                negative_impact="把采集失败误判为零召回会生成错误治理动作",
                confidence=0.0,
            )
        result_count = int(raw_result_count)
        previous = context.get("previous_result_count")
        seven_day_average = context.get("seven_day_average")
        history_sample_count = int(context.get("history_sample_count") or 0)
        assessment = build_recall_assessment(
            result_count=result_count,
            low_result_threshold=self.low_result_threshold,
            drop_ratio_threshold=self.drop_ratio_threshold,
            seven_day_average=seven_day_average,
            history_sample_count=history_sample_count,
        )
        reasons = list(assessment["reasons"])

        intents = []
        if reasons:
            kg_suggestions = context.get("kg_action_suggestions") or context.get("kg_actions") or []
            kg_action = kg_suggestions[0] if isinstance(kg_suggestions, list) and kg_suggestions else None
            intents.append(
                {
                    "intent_type": "expand_recall",
                    "query": context.get("query"),
                    "reason": ",".join(reasons),
                    "directly_executable": False,
                    **({"kg_action": kg_action} if kg_action else {}),
                }
            )

        return ExecutorResult(
            check_item="recall",
            status="warning" if intents else "pass",
            root_cause="kg_issue" if intents else "uncertain",
            recommended_action="kg_review" if intents else "observe",
            actions=[],
            kg_action_intents=intents,
            evidence={
                "result_count": result_count,
                "previous_result_count": previous,
                "sevenDayAverage": float(seven_day_average) if seven_day_average is not None else None,
                "historySampleCount": history_sample_count,
                "dropRatio": assessment["drop_ratio"],
                "reasons": reasons,
                "recallScore": assessment["score"],
                "recallStatus": assessment["status"],
                "recallSummary": assessment["summary"],
                "scoringTrace": assessment["scoring_trace"],
            },
            conversion_hypothesis="召回不足会直接限制商品曝光和转化机会",
            expected_impact="补足 Query 可售相关商品召回",
            negative_impact="过宽召回可能引入相关性噪音",
            confidence=0.8 if intents else 0.4,
        )


class WorldKnowledgeAgentInvocationError(RuntimeError):
    def __init__(self, original_error: Exception, agent_metrics: Dict[str, Any]):
        super().__init__(str(original_error))
        self.original_error = original_error
        self.agent_metrics = agent_metrics


class WorldKnowledgeDecisionAgent:
    def __init__(
        self,
        llm: Optional[Any] = None,
        model_config_snapshot: Optional[NodeConfigSnapshot] = None,
    ):
        self.disabled_explicitly = False
        self.initialization_failure: Optional[Dict[str, str]] = None
        self.model_config = (
            get_snapshot_node_config(
                model_config_snapshot,
                "top_query.world_knowledge",
                required_channel="rd_llm",
            )
            if model_config_snapshot is not None
            else None
        )
        self.llm = llm if llm is not None else self._default_llm()

    def decide(self, context: Dict[str, Any]) -> Dict[str, Any]:
        if not self.llm:
            reason = "world knowledge decision llm is not configured"
            error_type = "disabled_explicitly" if self.disabled_explicitly else "initialization_failure"
            if self.initialization_failure:
                reason = self.initialization_failure.get("reason") or reason
            exc = RuntimeError(reason)
            _safe_attach_world_knowledge_agent_metrics(exc, {
                "attempts": [],
                "attemptCount": 0,
                "timeoutCount": 0,
                "totalDurationSeconds": 0.0,
                "errorType": error_type,
            })
            raise exc
        timeout_seconds = float(os.getenv("TOP_QUERY_WORLD_KNOWLEDGE_TIMEOUT_SECONDS", "120"))
        messages = self._messages_for_context(context)
        primary_invoke = getattr(self.llm, "invoke_primary", None) or self.llm.invoke
        fallback_invoke = getattr(self.llm, "invoke_fallback", None)
        attempts: List[Dict[str, Any]] = []
        last_error: Optional[Exception] = None
        try:
            return self._invoke_attempt(
                primary_invoke, messages, timeout_seconds, "primary", False, attempts,
            )
        except Exception as exc:
            last_error = exc
        if fallback_invoke is not None:
            try:
                return self._invoke_attempt(
                    fallback_invoke, messages, timeout_seconds, "fallback", False, attempts,
                )
            except Exception as exc:
                last_error = exc

        compact_messages = self._messages_for_context(self._compact_context(context), compact_mode=True)
        try:
            return self._invoke_attempt(
                primary_invoke, compact_messages, timeout_seconds, "primary", True, attempts,
            )
        except Exception as exc:
            last_error = exc
        if fallback_invoke is not None:
            try:
                return self._invoke_attempt(
                    fallback_invoke, compact_messages, timeout_seconds, "fallback", True, attempts,
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            agent_metrics = _world_knowledge_agent_metrics(attempts)
            if _safe_attach_world_knowledge_agent_metrics(last_error, agent_metrics):
                raise last_error
            raise WorldKnowledgeAgentInvocationError(last_error, agent_metrics) from last_error
        raise RuntimeError("world knowledge decision llm returned no result")

    def _invoke_attempt(
        self,
        invoke_fn: Any,
        messages: List[Dict[str, Any]],
        timeout_seconds: float,
        attempt_type: str,
        compact: bool,
        attempts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        started_at = time.perf_counter()
        response = None
        try:
            response = _invoke_with_timeout(invoke_fn, messages, timeout_seconds)
            payload = self._parse_decision_response(response)
        except Exception as exc:
            attempts.append(_world_knowledge_attempt_metrics(
                llm=self.llm,
                attempt_type=attempt_type,
                compact=compact,
                timeout_seconds=timeout_seconds,
                duration_seconds=time.perf_counter() - started_at,
                messages=messages,
                response=response,
                error=exc,
                model_config=self.model_config,
            ))
            _safe_attach_world_knowledge_agent_metrics(exc, _world_knowledge_agent_metrics(attempts))
            raise
        attempts.append(_world_knowledge_attempt_metrics(
            llm=self.llm,
            attempt_type=attempt_type,
            compact=compact,
            timeout_seconds=timeout_seconds,
            duration_seconds=time.perf_counter() - started_at,
            messages=messages,
            response=response,
            error=None,
            model_config=self.model_config,
        ))
        payload["agent_metrics"] = _world_knowledge_agent_metrics(attempts)
        return payload

    def _messages_for_context(self, context: Dict[str, Any], compact_mode: bool = False) -> List[Dict[str, str]]:
        instruction = (
            "你是电商搜索运营的通用世界知识治理 Agent。"
            "你必须逐项综合已配置的季节、天气、节日/节气、社媒趋势，以及 Query 属性、Top N 商品、标签事实和已有标签权重配置。"
            "considered_factors 和 factor_assessments.type 只能使用 season、weather、festival、social_trend 四类标准信号；"
            "Query 语义、Top N 商品、标签目录和已有配置只是辅助证据，不得作为额外 type 输出。"
            "只对 configured_factors 中的来源输出是否有效、与 Query 的相关性、证据、预期转化影响和候选标签；未配置来源不得作为前置条件。"
            "当同时存在多个世界知识信号（且已配置）时，不得丢失任何一类；必须识别信号叠加、信号冲突和时效强弱，并说明最终取舍。"
            "世界知识不是直接执行标签的理由；必须先判断它是否会改变该 Query 用户当下的购买偏好，再判断 Top N 商品是否已符合这些偏好。"
            "你必须优先阅读 query_profile、open_factor_context、products、product_evidence_summary、"
            "query_semantic_tag_validation、tag_catalog_validation、tag_bucket_current_config 和 tag_bucket_config_hint。"
            "如果 Top N 已吸收有效世界知识偏好，输出 observe；如果信号与 Query 弱相关或会污染 Query 语义，输出 skip；"
            "如果 Top N 不符合有效偏好且存在可验证标签，输出 execute；事实不稳定时输出 manual_review，无可执行标签时输出 cannot_execute。"
            "夏季连衣裙偏轻薄、透气、无袖只是季节信号的示例，不得把夏季服饰规则当作主逻辑或套用到其他 Query。"
            "不要机械沿用候选提示标签；只有在它们确实适合当前 Query 时才可选择。"
            "如果 query_semantic_tag_validation 显示 keyword 自身就是更精确的可用标签语义，且 Top 商品已自然覆盖该语义，优先 observe 或 skip。"
            "如果建议执行，config_plan 必须遵循标签权重接口语义：组合标签优先，同时补单标签兜底。"
            "例如建议提升无袖和轻薄时，config_plan 应至少包含"
            "[{\"tags\":\"无袖,轻薄\",\"afterBucket\":\"top\"},{\"tags\":\"无袖\",\"afterBucket\":\"top\"},{\"tags\":\"轻薄\",\"afterBucket\":\"top\"}]。"
            "如果 selected_tags 中存在互斥标签（如无袖、短袖），不要把它们放进同一个组合；应拆成各自组合和单标签兜底。"
            "如果证据不足，请输出 observe 或 manual_review，而不是勉强 execute。"
        )
        if compact_mode:
            instruction += "当前是压缩上下文重试，你必须优先利用摘要字段完成判断，不要要求更多上下文。"
        request_payload = {
            "task": "decide_top_query_world_knowledge_tag_bucket",
            "instruction": instruction + "你必须只输出 JSON。",
            "output_schema": {
                "decision": "execute|observe|skip|manual_review|cannot_execute",
                "should_adjust": "boolean",
                "decision_reason": "string",
                "considered_factors": "list[string]",
                "factor_assessments": "list[{type:string,status:string,query_relevance:string,evidence:list|string,expected_impact:string,candidate_tags:list[string]}]",
                "candidate_tags": "list[string]",
                "selected_tags": "list[string]",
                "config_plan": "list[{tags:string, afterBucket:string}]",
                "rejected_tags": "list[{tag:string, reason:string}]",
                "after_bucket": "string",
                "expected_impact": "string",
                "risk_level": "low|medium|high",
                "verify_plan": "object",
            },
            "context": context,
        }
        return [
            {"role": "system", "content": "只输出 JSON，不要输出 Markdown。"},
            {"role": "user", "content": json.dumps(request_payload, ensure_ascii=False, default=str)},
        ]

    @staticmethod
    def _compact_context(context: Dict[str, Any]) -> Dict[str, Any]:
        compact = dict(context or {})
        compact.pop("products", None)
        compact["compact_mode"] = True
        return compact

    @staticmethod
    def _parse_decision_response(response: Any) -> Dict[str, Any]:
        payload = json.loads(_strip_json_fence(getattr(response, "content", response)))
        if not isinstance(payload, dict):
            raise ValueError("WorldKnowledgeDecisionAgent response must be a JSON object")
        decision = str(payload.get("decision") or "").strip()
        if decision not in {"execute", "observe", "skip", "manual_review", "cannot_execute"}:
            raise ValueError("WorldKnowledgeDecisionAgent decision is invalid")
        return payload

    def _default_llm(self) -> Optional[Any]:
        if os.getenv("ENV", "test").lower() == "test" and os.getenv("TOP_QUERY_WORLD_KNOWLEDGE_LLM_IN_TEST", "true").lower() == "false":
            self.disabled_explicitly = True
            return None
        try:
            timeout = float(os.getenv("TOP_QUERY_WORLD_KNOWLEDGE_TIMEOUT_SECONDS", "120"))
            if getattr(self, "model_config", None) is not None:
                return _build_snapshot_rd_llm(
                    self.model_config,
                    timeout=timeout,
                    temperature=0.1,
                )
            return build_rd_chat_with_fallback(
                timeout=timeout,
                primary_env="TOP_QUERY_WORLD_KNOWLEDGE_MODEL",
                fallback_env="TOP_QUERY_WORLD_KNOWLEDGE_FALLBACK_MODEL",
                default_primary_model="qwen3.6-plus",
                default_fallback_model="qwen3.7-max",
                temperature=0.1,
                max_retries=0,
            )
        except Exception as exc:
            self.initialization_failure = {
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }
            return None


class OpenFactorExecutor:
    def __init__(
        self,
        tag_bucket_api_configured: bool = False,
        policy: Optional[ActionPolicy] = None,
        world_knowledge_agent: Optional[Any] = None,
        operational_service: Optional[Any] = None,
        allow_deterministic_fallback: bool = False,
    ):
        self.tag_bucket_api_configured = tag_bucket_api_configured
        self.policy = policy or ActionPolicy()
        self.require_agent_availability = not allow_deterministic_fallback
        self.world_knowledge_agent = world_knowledge_agent if world_knowledge_agent is not None else WorldKnowledgeDecisionAgent()
        self.operational_service = operational_service

    def execute(self, context: Dict[str, Any]) -> ExecutorResult:
        context = _normalize_context(context)
        open_factor_context = context.get("open_factor_context") or {}
        tag_bucket_payload = _tag_bucket_config_payload(context)
        tag_bucket_seed = tag_bucket_payload or _tag_bucket_target_seed(context)
        tag_bucket_api_ready = (self.tag_bucket_api_configured or bool(context.get("tag_bucket_api_configured"))) and context.get("tag_bucket_api_supported", True) is not False
        world_knowledge_decision = _build_world_knowledge_decision(
            context,
            open_factor_context,
            tag_bucket_seed,
            world_knowledge_agent=self.world_knowledge_agent,
            require_agent_availability=self.require_agent_availability,
            operational_service=self.operational_service,
        )
        agent_metrics = _normalized_executor_llm_metrics(
            world_knowledge_decision.get("agent_metrics")
            or {}
        )
        if "agent_metrics" in world_knowledge_decision:
            world_knowledge_decision = {
                **world_knowledge_decision,
                "agent_metrics": agent_metrics,
            }
        evidence = {
            **open_factor_context,
            "world_knowledge_decision": world_knowledge_decision,
        }
        if isinstance(world_knowledge_decision.get("agent_metrics"), dict):
            evidence["agent_metrics"] = agent_metrics
        if isinstance(world_knowledge_decision.get("agent_failure"), dict):
            evidence["agent_failure"] = world_knowledge_decision.get("agent_failure")
        telemetry = {
            "token_usage": _executor_token_usage(agent_metrics),
            "llm_metrics": agent_metrics,
        }
        if world_knowledge_decision.get("decision") == "observe":
            return ExecutorResult(
                check_item="open_factor",
                status="improved",
                root_cause="external_factor",
                recommended_action="observe",
                actions=[],
                kg_action_intents=[],
                evidence=evidence,
                conversion_hypothesis="世界知识信号已被当前配置吸收",
                expected_impact=world_knowledge_decision.get("expected_impact") or "继续观察后续流量表现",
                negative_impact="",
                confidence=float(world_knowledge_decision.get("confidence") or 0.75),
                **telemetry,
            )
        if world_knowledge_decision.get("decision") == "skip":
            return ExecutorResult(
                check_item="open_factor",
                status="pass",
                root_cause="external_factor",
                recommended_action="skip",
                actions=[],
                kg_action_intents=[],
                evidence=evidence,
                conversion_hypothesis="当前结果已自然吸收世界知识意图，无需追加标签权重调整",
                expected_impact=world_knowledge_decision.get("expected_impact") or "保持当前结果集并继续观察",
                negative_impact="",
                confidence=float(world_knowledge_decision.get("confidence") or 0.7),
                **telemetry,
            )
        if world_knowledge_decision.get("decision") == "manual_review":
            return ExecutorResult(
                check_item="open_factor",
                status="warning",
                root_cause="external_factor",
                recommended_action="manual_review",
                actions=[],
                kg_action_intents=[],
                evidence=evidence,
                conversion_hypothesis="世界知识信号存在，但只读查询结果不稳定，自动调整风险较高",
                expected_impact=world_knowledge_decision.get("expected_impact") or "待人工复核后再决定是否执行标签权重调整",
                negative_impact="查询异常时盲目执行可能覆盖正确配置",
                confidence=float(world_knowledge_decision.get("confidence") or 0.35),
                **telemetry,
            )
        if world_knowledge_decision.get("decision") == "cannot_execute":
            return ExecutorResult(
                check_item="open_factor",
                status="check_failed",
                root_cause="external_factor",
                recommended_action="cannot_execute",
                actions=[],
                kg_action_intents=[],
                evidence=evidence,
                conversion_hypothesis="世界知识会改变用户即时偏好",
                expected_impact=world_knowledge_decision.get("expected_impact") or "建议标签未通过验证，暂时无法生成可执行配置",
                negative_impact="标签事实不完整时不应自动写入生产",
                confidence=float(world_knowledge_decision.get("confidence") or 0.2),
                **telemetry,
            )
        configured = tag_bucket_api_ready
        resolved_tag_bucket_payload = _tag_bucket_payload_from_world_knowledge_decision(
            world_knowledge_decision,
            tag_bucket_seed,
            context.get("query"),
        )
        if configured:
            if not resolved_tag_bucket_payload:
                failure_reason = _open_factor_failure_reason(open_factor_context)
                return ExecutorResult(
                    check_item="open_factor",
                    status="check_failed",
                    root_cause="external_factor",
                    recommended_action="cannot_execute",
                    actions=[],
                    kg_action_intents=[],
                    evidence={**evidence, "missing_tag_bucket_payload": True, "failure_reason": failure_reason},
                    conversion_hypothesis="世界知识会改变用户即时偏好",
                    expected_impact=f"无法执行标签权重调整：{failure_reason}",
                    negative_impact="错误或空配置不应自动写入生产",
                    confidence=0.2,
                    **telemetry,
                )
            action = ActionCandidate(
                check_item="open_factor",
                action_type="tag_bucket_config",
                reason="世界知识命中且 tag bucket API 已配置",
                execution_mode=self.policy.resolve("open_factor", "tag_bucket_config"),
                payload={**resolved_tag_bucket_payload, "brand": context.get("brand"), "check_key": "open_factor", "action_type": "tag_bucket_config", "open_factor_context": open_factor_context, "world_knowledge_decision": world_knowledge_decision},
                expected_impact="顺应季节、天气或趋势提升曝光效率",
                negative_impact="世界知识判断错误会扰动稳定排序",
                confidence=float(world_knowledge_decision.get("confidence") or 0.6),
            )
        else:
            action = ActionCandidate(
                check_item="open_factor",
                action_type="suggestion",
                reason="tag bucket API 未配置，仅 dry-run 建议",
                execution_mode="dry_run",
                payload={"query": context.get("query"), "open_factor_context": open_factor_context, "world_knowledge_decision": world_knowledge_decision},
                confidence=0.45,
            )
        return ExecutorResult(
            check_item="open_factor",
            status="fixed" if configured else "warning",
            root_cause="external_factor",
            recommended_action=action.action_type,
            actions=[action],
            kg_action_intents=[],
            evidence=evidence,
            conversion_hypothesis="世界知识会改变用户即时偏好",
            expected_impact=action.expected_impact,
            negative_impact=action.negative_impact,
            confidence=action.confidence,
            **telemetry,
        )


class KnowledgeGraphExecutor:
    def __init__(self, kg_planner: Optional[Any] = None):
        self.kg_planner = kg_planner

    def execute(self, kg_action_intents: List[Dict[str, Any]], context: Optional[Dict[str, Any]] = None) -> ExecutorResult:
        actions = []
        missing_payload_count = 0
        diagnostics = []
        for index, intent in enumerate(kg_action_intents, start=1):
            payloads, intent_diagnostics = self._payloads_for_intent(intent, context or {})
            if intent_diagnostics:
                diagnostics.append(intent_diagnostics)
            if not payloads:
                missing_payload_count += 1
                continue
            for offset, kg_payload in enumerate(payloads):
                action_type = _kg_wrapper_action_type(intent, kg_payload)
                check_item = "recall" if action_type == "kg_tag" else "relevance"
                actions.append(
                    ActionCandidate(
                        check_item=check_item,
                        action_type=action_type,
                        reason=kg_payload.get("reason") or intent.get("reason", "KG intent requires human approval"),
                        execution_mode="human_approval",
                        payload={**kg_payload, "query": intent.get("query"), "check_key": check_item, "action_type": action_type},
                        priority=index + offset,
                        expected_impact=kg_payload.get("expectedImpact") or "修正知识图谱召回或关系表达",
                        negative_impact=kg_payload.get("negativeImpact") or "错误 KG 变更会影响多个 Query",
                        confidence=0.65,
                    )
                )
        return ExecutorResult(
            check_item="knowledge_graph",
            status="waiting_human_approval" if actions else "check_failed" if missing_payload_count else "pass",
            root_cause="kg_issue" if actions or missing_payload_count else "uncertain",
            recommended_action="human_approval" if actions else "cannot_execute" if missing_payload_count else "observe",
            actions=actions,
            evidence={"intent_count": len(kg_action_intents), "missing_executable_payload_count": missing_payload_count, "diagnostics": diagnostics},
            conversion_hypothesis="KG 修正需要人工审批后再执行",
            expected_impact="形成有序 KG action plan" if actions else "无法生成 KG 动作：缺少 actionType/params",
            negative_impact="避免未审批 KG 写入",
            confidence=0.65 if actions else 0.2 if missing_payload_count else 0.4,
        )

    def _payloads_for_intent(
        self,
        intent: Dict[str, Any],
        context: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        kg_payload = _kg_action_payload(intent)
        if kg_payload:
            return [kg_payload], {}
        if not self.kg_planner:
            return [], {"missingRequiredFields": ["actionType", "params"], "skippedReasons": ["kg_planner_unavailable"]}
        planned_actions, diagnostics = self.kg_planner.plan(intent, context)
        payloads = []
        for action in planned_actions:
            kg_payload = _kg_action_payload(action)
            if kg_payload:
                payloads.append({**action, "diagnostics": diagnostics})
        return payloads, diagnostics if isinstance(diagnostics, dict) else {}


class VerificationExecutor:
    def __init__(self, search_service: Any, page_size: int = 16):
        self.search_service = search_service
        self.page_size = page_size

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        context = _normalize_context(context)
        brand = context.get("brand")
        query = context.get("query")
        search_with_total = getattr(self.search_service, "search_products_with_total", None)
        search_exception = None
        if callable(search_with_total):
            try:
                resolved_search_result = search_with_total(brand, query, page_size=self.page_size)
            except Exception as exc:
                resolved_search_result = None
                search_exception = exc
        else:
            resolved_search_result = None
        structured_products = (
            resolved_search_result.get("products")
            if isinstance(resolved_search_result, dict) and "products" in resolved_search_result
            else None
        )
        if search_exception is not None:
            after_products = []
            after_result_count = None
            query_succeeded = False
            result_complete = False
            failure_reason = f"search_exception:{type(search_exception).__name__}"
        elif (
            isinstance(structured_products, list)
            and all(isinstance(product, dict) for product in structured_products)
        ):
            after_products = list(structured_products)
            raw_total_count = resolved_search_result.get("total_count")
            raw_query_succeeded = resolved_search_result.get("success")
            raw_result_complete = resolved_search_result.get("result_complete", False)
            total_is_valid = (
                raw_total_count is None
                or (
                    isinstance(raw_total_count, int)
                    and not isinstance(raw_total_count, bool)
                    and raw_total_count >= 0
                )
            )
            total_matches_products = (
                total_is_valid
                and (
                    raw_total_count is None
                    or raw_total_count >= len(after_products)
                )
            )
            structured_evidence_is_valid = (
                isinstance(raw_query_succeeded, bool)
                and isinstance(raw_result_complete, bool)
                and total_is_valid
                and total_matches_products
                and not (
                    raw_result_complete
                    and (
                        not raw_query_succeeded
                        or raw_total_count is None
                        or raw_total_count != len(after_products)
                    )
                )
            )
            if structured_evidence_is_valid:
                query_succeeded = raw_query_succeeded
                if query_succeeded:
                    after_result_count = raw_total_count
                    result_complete = (
                        raw_total_count is not None
                        and raw_result_complete
                    )
                else:
                    after_products = []
                    after_result_count = None
                    result_complete = False
                failure_reason = resolved_search_result.get("error_reason")
            else:
                after_result_count = None
                query_succeeded = False
                result_complete = False
                failure_reason = "invalid_search_response"
        elif callable(search_with_total):
            after_products = []
            after_result_count = None
            query_succeeded = False
            result_complete = False
            failure_reason = "invalid_search_response"
        else:
            try:
                legacy_products = self.search_service.search_products(brand, query, page_size=self.page_size)
            except Exception as exc:
                after_products = []
                after_result_count = None
                query_succeeded = False
                result_complete = False
                failure_reason = f"search_exception:{type(exc).__name__}"
            else:
                if isinstance(legacy_products, list) and all(isinstance(product, dict) for product in legacy_products):
                    after_products = legacy_products
                    query_succeeded = True
                    failure_reason = None
                else:
                    after_products = []
                    query_succeeded = False
                    failure_reason = "invalid_search_response"
                after_result_count = None
                result_complete = False
        summarized_after = _summarize_products(after_products or [])
        after_diagnostics = _build_relevance_diagnostics(query, summarized_after)
        world_knowledge_summary = build_world_knowledge_verification_summary(context, summarized_after)
        raw_before_result_count = context.get("result_count")
        before_result_count_known = (
            context.get("result_count_known") is not False
            and isinstance(raw_before_result_count, int)
            and not isinstance(raw_before_result_count, bool)
            and raw_before_result_count >= 0
        )
        before_result_count = (
            None
            if not before_result_count_known
            else raw_before_result_count
        )
        after_result_count_known = (
            query_succeeded is True
            and isinstance(after_result_count, int)
            and not isinstance(after_result_count, bool)
            and after_result_count >= 0
        )
        return {
            "before": {
                "result_count": before_result_count,
                "result_count_known": before_result_count_known,
                "products": _summarize_products(context.get("products") or []),
                "relevance_diagnostics": _relevance_diagnostics(context),
            },
            "after": {
                "query_succeeded": query_succeeded,
                "result_complete": result_complete,
                "result_count": after_result_count,
                "result_count_known": after_result_count_known,
                "failure_reason": failure_reason,
                "products": summarized_after,
                "relevance_diagnostics": after_diagnostics,
                **world_knowledge_summary,
            },
        }


def _relevance_diagnostics(context: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = context.get("relevance_diagnostics")
    if isinstance(diagnostics, dict):
        return diagnostics
    return _build_relevance_diagnostics(context.get("query"), context.get("products") or [])


def _build_relevance_diagnostics(query: Any, products: List[Dict[str, Any]]) -> Dict[str, Any]:
    items = [_relevance_diagnostic_item(product) for product in products if "is_relevant" in product]
    mismatch_count = sum(1 for item in items if item.get("is_relevant") is False)
    check_failed_count = sum(1 for item in items if item.get("relevance_check_status") == "check_failed")
    return {
        "query": query,
        "items": items,
        "checked_count": len(items),
        "mismatch_count": mismatch_count,
        "check_failed_count": check_failed_count,
        "mismatch_ratio": round(mismatch_count / len(items), 4) if items else 0.0,
    }


def _relevance_diagnostic_item(product: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "product_id": _product_id(product),
        "rank": product.get("rank"),
        "name": product.get("productName") or product.get("name"),
        "tag_name": product.get("tag_name") or product.get("tagName"),
        "is_relevant": product.get("is_relevant"),
        "relevance_reason": product.get("relevance_reason"),
        "relevance_source": product.get("relevance_source"),
        "relevance_check_status": product.get("relevance_check_status"),
    }


def _normalize_context(context: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(context)
    task = normalized.get("task") or {}
    search = normalized.get("search") or {}
    if "query" not in normalized and task.get("query_text"):
        normalized["query"] = task.get("query_text")
    if "brand" not in normalized and task.get("brand"):
        normalized["brand"] = task.get("brand")
    if "result_count" not in normalized and "result_count" in search:
        normalized["result_count"] = search.get("result_count")
    if "result_count_known" not in normalized and "result_count_known" in search:
        normalized["result_count_known"] = search.get("result_count_known")
    if "search_failure_reason" not in normalized:
        normalized["search_failure_reason"] = search.get("failure_reason") or search.get("error_reason")
    if "metrics" not in normalized:
        normalized["metrics"] = {
            "ctr": task.get("ctr"),
            "cvr": task.get("cvr"),
            "avg_ctr": task.get("avg_ctr"),
            "avg_cvr": task.get("avg_cvr"),
        }
    products = []
    for product in normalized.get("products") or []:
        item = dict(product)
        size_inventory = item.get("size_inventory") if isinstance(item.get("size_inventory"), dict) else {}
        if "broken_rate" not in item and size_inventory.get("broken_rate") is not None:
            item["broken_rate"] = size_inventory.get("broken_rate")
        if "broken_rate" not in item and "broken_size_rate" in item:
            item["broken_rate"] = item.get("broken_size_rate")
        if "productId" not in item and item.get("product_id"):
            item["productId"] = item.get("product_id")
        if "productName" not in item and item.get("name"):
            item["productName"] = item.get("name")
        if "bucket" not in item and isinstance(item.get("ext"), dict):
            item["bucket"] = item["ext"].get("bucket")
        products.append(item)
    normalized["products"] = products
    if "open_factor_context" not in normalized and normalized.get("open_factors"):
        normalized["open_factor_context"] = normalized.get("open_factors")
    return normalized


def _product_id(product: Dict[str, Any]) -> str:
    return str(
        product.get("productId")
        or product.get("product_id")
        or product.get("skuId")
        or product.get("sku_id")
        or product.get("spu")
        or ""
    ).strip()


def _tag_bucket_config_payload(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = context.get("tag_bucket_config") or context.get("tagBucketConfig")
    if not isinstance(payload, dict):
        return None
    keyword = str(payload.get("keyword") or payload.get("query") or context.get("query") or "").strip()
    filters = payload.get("filters")
    configs = payload.get("configs")
    if not keyword or not isinstance(filters, list) or not isinstance(configs, list) or not configs:
        return None
    return {"keyword": keyword, "filters": filters, "configs": configs}


def _tag_bucket_target_seed(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    open_factor_context = context.get("open_factor_context") if isinstance(context.get("open_factor_context"), dict) else {}
    target = context.get("tag_bucket_target") or open_factor_context.get("tag_bucket_target")
    if not isinstance(target, dict):
        return None
    keyword = str(target.get("keyword") or target.get("query") or context.get("query") or "").strip()
    filters = target.get("filters")
    if not keyword or not isinstance(filters, list):
        return None
    return {
        "keyword": keyword,
        "filters": filters,
        "default_after_bucket": str(target.get("default_after_bucket") or target.get("defaultAfterBucket") or "top").strip() or "top",
    }


def _has_confirmed_open_factor_signal(open_factor_context: Dict[str, Any]) -> bool:
    if open_factor_context.get("matched_factors"):
        return True
    if open_factor_context.get("season_mismatch"):
        return True
    return False


def _has_world_knowledge_facts(open_factor_context: Dict[str, Any]) -> bool:
    if _has_confirmed_open_factor_signal(open_factor_context):
        return True
    if str(open_factor_context.get("season") or "").strip():
        return True
    for key in ["social_trend", "weather", "festival"]:
        value = open_factor_context.get(key)
        if not isinstance(value, dict):
            continue
        status = str(value.get("status") or "").strip().lower()
        if status and status != "unavailable":
            return True
        if value.get("matched") is True or value.get("triggered") is True:
            return True
        if value.get("items") or value.get("cities"):
            return True
    return False


def _guard_world_knowledge_decision(
    decision: Dict[str, Any],
    open_factor_context: Dict[str, Any],
) -> Dict[str, Any]:
    if _has_world_knowledge_facts(open_factor_context):
        return decision
    decision_name = str(decision.get("decision") or "")
    if decision.get("agent_failure") or decision_name in {"manual_review", "cannot_execute"}:
        return decision
    has_stale_execution_payload = bool(
        decision.get("selected_tags")
        or decision.get("config_plan")
        or decision.get("should_adjust") is True
    )
    if decision_name != "execute" and not has_stale_execution_payload and "should_adjust" in decision:
        return {
            **decision,
            "audit_status": "no_signal",
            "audit_issues": [],
        }
    return _world_knowledge_decision_payload(WorldKnowledgeDecision(
        decision="skip",
        should_adjust=False,
        confidence=0.9,
        decision_reason="当前 Query 未命中有效季节、天气、节日或社媒趋势信号，不执行标签权重调整",
        audit_status="no_signal",
        candidate_tags=[],
        selected_tags=[],
        config_plan=[],
        rejected_tags=[],
        existing_config_hit=False,
        config_mode="skip",
        after_bucket="top",
        payload_preview={},
        expected_impact="保持当前结果集",
        risk_level="low",
        verify_plan={"expect_result_set_change": False, "guard": "no_effective_signal"},
    ))


def _open_factor_failure_reason(open_factor_context: Dict[str, Any]) -> str:
    if open_factor_context.get("matched_factors") and not open_factor_context.get("suggested_tags"):
        return "世界知识命中但未生成推荐标签，无法生成标签权重调整配置"
    if open_factor_context.get("suggested_tags"):
        return "世界知识已生成推荐标签，但缺少 keyword/filters/configs，无法调用标签权重调整接口"
    return "世界知识信号不完整，无法生成标签权重调整配置"


def _build_world_knowledge_decision(
    context: Dict[str, Any],
    open_factor_context: Dict[str, Any],
    tag_bucket_payload: Optional[Dict[str, Any]],
    world_knowledge_agent: Optional[Any] = None,
    require_agent_availability: bool = False,
    operational_service: Optional[Any] = None,
) -> Dict[str, Any]:
    agent_payload = None
    agent_failure = None
    agent_metrics = None
    strict_agent_failure = False
    if world_knowledge_agent is not None:
        strict_agent_failure = require_agent_availability or not bool(getattr(world_knowledge_agent, "disabled_explicitly", False))
        try:
            agent_payload = world_knowledge_agent.decide({
                "brand": context.get("brand"),
                "query": context.get("query"),
                "query_profile": _world_knowledge_query_profile(context, tag_bucket_payload),
                "open_factor_context": _world_knowledge_open_factor_for_agent(open_factor_context),
                "products": _world_knowledge_products_for_agent(context.get("products") or []),
                "product_evidence_summary": _world_knowledge_product_evidence_summary(
                    context.get("products") or [],
                    open_factor_context.get("suggested_tags") or [],
                ),
                "seasonal_fit_summary": _world_knowledge_seasonal_fit_summary(
                    query=str(context.get("query") or ""),
                    products=context.get("products") or [],
                    season=str(open_factor_context.get("season") or ""),
                    candidate_tags=open_factor_context.get("suggested_tags") or [],
                ),
                "seasonal_query_category": _world_knowledge_query_category(
                    str(_world_knowledge_query_profile(context, tag_bucket_payload).get("keyword") or "")
                ),
                "query_semantic_tag_validation": _world_knowledge_tag_validation_for_agent(
                    context.get("query_semantic_tag_validation") or {},
                ),
                "tag_catalog_validation": _world_knowledge_tag_validation_for_agent(context.get("tag_catalog_validation") or {}),
                "tag_bucket_current_config": _world_knowledge_current_config_for_agent(context.get("tag_bucket_current_config") or {}),
                "tag_bucket_config_hint": tag_bucket_payload or {},
            })
        except Exception as exc:
            agent_payload = None
            agent_failure = _sanitize_agent_failure_reason(str(exc))
            agent_metrics = getattr(exc, "agent_metrics", None)
    if agent_payload:
        decision = _normalize_agent_world_knowledge_decision(
            agent_payload,
            context,
            open_factor_context,
            tag_bucket_payload,
            operational_service=operational_service,
        )
        if isinstance(agent_payload.get("agent_metrics"), dict):
            decision["agent_metrics"] = agent_payload["agent_metrics"]
    elif world_knowledge_agent is not None and strict_agent_failure and agent_failure:
        decision = _world_knowledge_agent_failure_decision(
            context,
            open_factor_context,
            tag_bucket_payload,
            agent_failure,
            agent_metrics=agent_metrics,
        )
    else:
        decision = _fallback_world_knowledge_decision(context, open_factor_context, tag_bucket_payload)
    return _guard_world_knowledge_decision(decision, open_factor_context)


def _fallback_world_knowledge_decision(
    context: Dict[str, Any],
    open_factor_context: Dict[str, Any],
    tag_bucket_payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    normalized_payload_preview = _normalized_tag_bucket_payload_preview(tag_bucket_payload)
    candidate_tags = [str(tag).strip() for tag in open_factor_context.get("suggested_tags") or [] if str(tag).strip()]
    payload_tags = _payload_selected_tags(tag_bucket_payload)
    validation = context.get("tag_catalog_validation") if isinstance(context.get("tag_catalog_validation"), dict) else {}
    current_config_record = context.get("tag_bucket_current_config") if isinstance(context.get("tag_bucket_current_config"), dict) else {}
    existing_config = current_config_record.get("config") if isinstance(current_config_record.get("config"), dict) else {}
    selected_tags = _validated_candidate_tags(validation, candidate_tags or payload_tags)
    after_bucket = _resolve_world_knowledge_after_bucket(tag_bucket_payload)
    config_plan = _normalize_world_knowledge_config_plan(selected_tags, after_bucket)
    payload_preview = _apply_world_knowledge_config_plan(normalized_payload_preview, config_plan, after_bucket)
    validation_status = str(validation.get("status") or "").lower() if isinstance(validation, dict) else ""
    has_effective_signal = _has_confirmed_open_factor_signal(open_factor_context)
    rejected_tags = [
        {
            "tag": str(item.get("normalized_name") or item.get("name") or "").strip(),
            "reason": "标签查询接口不存在",
        }
        for item in validation.get("missing_tags") or []
        if isinstance(item, dict) and str(item.get("normalized_name") or item.get("name") or "").strip()
    ]
    if _tag_queries_are_unstable(current_config_record, validation):
        return _world_knowledge_decision_payload(WorldKnowledgeDecision(
            decision="manual_review",
            should_adjust=False,
            confidence=0.35,
            decision_reason="世界知识相关只读查询结果不稳定，需要人工复核后再决定是否调整标签权重",
            candidate_tags=candidate_tags,
            selected_tags=selected_tags,
            config_plan=config_plan,
            rejected_tags=rejected_tags,
            existing_config_hit=False,
            config_mode="review",
            after_bucket=after_bucket,
            payload_preview=payload_preview,
            expected_impact="待人工复核只读查询结果后再决定是否执行标签权重调整",
            risk_level="medium",
            verify_plan={"expect_result_set_change": False},
        ))
    if _existing_config_covers_selected_tags(existing_config, selected_tags):
        return _world_knowledge_decision_payload(WorldKnowledgeDecision(
            decision="observe",
            should_adjust=False,
            confidence=0.75,
            decision_reason="现有 Query 标签权重配置已覆盖本次世界知识建议标签",
            candidate_tags=candidate_tags,
            selected_tags=selected_tags,
            config_plan=config_plan,
            rejected_tags=rejected_tags,
            existing_config_hit=True,
            config_mode="skip",
            after_bucket=after_bucket,
            payload_preview=payload_preview,
            expected_impact="保持当前配置并继续观察后续指标变化",
            risk_level="low",
            verify_plan={"expect_result_set_change": False},
        ))
    semantic_guard = _world_knowledge_semantic_guard_decision(
        context,
        selected_tags,
        tag_bucket_payload,
        existing_config,
        validation,
    )
    if semantic_guard:
        return semantic_guard
    if (
        not tag_bucket_payload
        and _world_knowledge_query_has_precise_existing_tag(context, tag_bucket_payload)
        and _is_already_covered_by_products(context.get("products") or [], [str(_world_knowledge_query_profile(context, tag_bucket_payload).get("keyword") or "").strip()])
    ):
        return _world_knowledge_decision_payload(WorldKnowledgeDecision(
            decision="skip",
            should_adjust=False,
            confidence=0.68,
            decision_reason="当前 Query 已有更精确的标签语义，且 Top 商品已自然覆盖，无需追加通用世界知识标签",
            candidate_tags=candidate_tags,
            selected_tags=selected_tags,
            config_plan=config_plan,
            rejected_tags=rejected_tags,
            existing_config_hit=False,
            config_mode="skip",
            after_bucket=after_bucket,
            payload_preview=payload_preview,
            expected_impact="保持当前结果集并继续观察后续指标变化",
            risk_level="low",
            verify_plan={"expect_result_set_change": False},
        ))
    if not selected_tags:
        validation_blocks_candidates = bool(candidate_tags) and validation_status not in {"", "not_found"}
        if not validation_blocks_candidates and payload_tags:
            return _world_knowledge_decision_payload(WorldKnowledgeDecision(
                decision="execute",
                should_adjust=True,
                confidence=0.62,
                decision_reason="世界知识信号已命中，当前存在候选配置，可尝试执行标签权重调整",
                candidate_tags=candidate_tags or payload_tags,
                selected_tags=payload_tags,
                config_plan=_normalize_world_knowledge_config_plan(payload_tags, after_bucket),
                rejected_tags=rejected_tags,
                existing_config_hit=bool(existing_config),
                config_mode="merge_update" if existing_config else "add",
                after_bucket=after_bucket,
                payload_preview=_apply_world_knowledge_config_plan(normalized_payload_preview, _normalize_world_knowledge_config_plan(payload_tags, after_bucket), after_bucket),
                expected_impact="顺应季节、天气或趋势提升曝光效率",
                risk_level="medium",
                verify_plan={"expect_result_set_change": True},
            ))
        cannot_execute = bool(candidate_tags) or has_effective_signal
        return _world_knowledge_decision_payload(WorldKnowledgeDecision(
            decision="cannot_execute" if cannot_execute else "skip",
            should_adjust=False,
            confidence=0.2 if cannot_execute else 0.5,
            decision_reason=(
                "建议标签未通过查询验证，暂时无法形成可执行配置"
                if candidate_tags
                else "世界知识信号已命中，但未产出可验证的候选标签"
                if has_effective_signal
                else "世界知识未产出有效候选标签"
            ),
            candidate_tags=candidate_tags,
            selected_tags=[],
            config_plan=[],
            rejected_tags=rejected_tags,
            existing_config_hit=False,
            config_mode="skip",
            after_bucket=after_bucket,
            payload_preview=payload_preview,
            expected_impact="补齐标签事实后再尝试生成配置" if cannot_execute else "保持当前结果集",
            risk_level="medium",
            verify_plan={"expect_result_set_change": False},
        ))
    return _world_knowledge_decision_payload(WorldKnowledgeDecision(
        decision="execute",
        should_adjust=True,
        confidence=0.82,
        decision_reason="世界知识命中且存在可执行标签候选，现有配置未完整覆盖这些意图",
        candidate_tags=candidate_tags,
        selected_tags=selected_tags,
        config_plan=config_plan,
        rejected_tags=rejected_tags,
        existing_config_hit=bool(existing_config),
        config_mode="merge_update" if existing_config else "add",
        after_bucket=after_bucket,
        payload_preview=payload_preview,
        expected_impact="顺应季节、天气或趋势提升曝光效率",
        risk_level="low",
        verify_plan={"expect_result_set_change": True},
    ))


def _normalize_agent_world_knowledge_decision(
    agent_payload: Dict[str, Any],
    context: Dict[str, Any],
    open_factor_context: Dict[str, Any],
    tag_bucket_payload: Optional[Dict[str, Any]],
    operational_service: Optional[Any] = None,
) -> Dict[str, Any]:
    normalized_payload_preview = _normalized_tag_bucket_payload_preview(tag_bucket_payload)
    fallback_candidate_tags = [str(tag).strip() for tag in open_factor_context.get("suggested_tags") or [] if str(tag).strip()]
    payload_schema_issues: List[str] = []
    candidate_source = "agent" if "candidate_tags" in agent_payload or "candidateTags" in agent_payload else "open_factor_context"
    raw_candidate_tags = agent_payload.get("candidate_tags") if "candidate_tags" in agent_payload else agent_payload.get("candidateTags")
    if raw_candidate_tags is None:
        raw_candidate_tags = fallback_candidate_tags
    elif not isinstance(raw_candidate_tags, list):
        payload_schema_issues.append("candidate_tags 必须是列表")
        raw_candidate_tags = []
    candidate_tags = [
        str(tag).strip()
        for tag in raw_candidate_tags
        if str(tag).strip()
    ]
    raw_selected_tags = agent_payload.get("selected_tags") if "selected_tags" in agent_payload else agent_payload.get("selectedTags")
    if raw_selected_tags is None:
        raw_selected_tags = []
    elif not isinstance(raw_selected_tags, list):
        payload_schema_issues.append("selected_tags 必须是列表")
        raw_selected_tags = []
    requested_selected_tags = [
        str(tag).strip()
        for tag in raw_selected_tags
        if str(tag).strip()
    ]
    current_config_record = context.get("tag_bucket_current_config") if isinstance(context.get("tag_bucket_current_config"), dict) else {}
    existing_config = current_config_record.get("config") if isinstance(current_config_record.get("config"), dict) else {}

    validation = None
    catalog_queried = False
    if not candidate_tags:
        validation = {
            "status": "no_candidate_tags",
            "existing_tags": [],
            "missing_tags": [],
        }
    elif operational_service:
        try:
            catalog_queried = True
            validation = operational_service.query_available_tags(context.get("brand"), candidate_tags)
        except Exception:
            validation = {"status": "error", "existing_tags": [], "missing_tags": []}

    selected_tags = _validated_agent_selected_tags(validation, requested_selected_tags)
    candidate_tag_trace = _world_knowledge_candidate_tag_trace(
        source=candidate_source,
        candidates=candidate_tags,
        queried=catalog_queried,
        validation=validation,
    )
    raw_rejected_tags = agent_payload.get("rejected_tags") if "rejected_tags" in agent_payload else agent_payload.get("rejectedTags")
    if raw_rejected_tags is None:
        raw_rejected_tags = []
    elif not isinstance(raw_rejected_tags, list):
        payload_schema_issues.append("rejected_tags 必须是列表")
        raw_rejected_tags = []
    rejected_tags = [
        item for item in raw_rejected_tags
        if isinstance(item, dict)
    ]
    if isinstance(validation, dict):
        for item in validation.get("missing_tags") or []:
            if isinstance(item, dict):
                rejected_tags.append({
                    "tag": str(item.get("normalized_name") or item.get("name") or "").strip(),
                    "reason": "标签查询接口不存在",
                })
    rejected_tags = [item for item in rejected_tags if item.get("tag")]

    decision = str(agent_payload.get("decision") or "").strip()
    decision_reason = str(agent_payload.get("decision_reason") or agent_payload.get("decisionReason") or "").strip()
    expected_impact = str(agent_payload.get("expected_impact") or agent_payload.get("expectedImpact") or "").strip()
    risk_level = str(agent_payload.get("risk_level") or agent_payload.get("riskLevel") or "medium").strip()
    try:
        confidence = float(agent_payload.get("confidence") or 0)
        if confidence < 0 or confidence > 1:
            raise ValueError("confidence out of range")
    except (TypeError, ValueError):
        confidence = 0.0
        payload_schema_issues.append("confidence 必须是 0-1 之间的数值")
    after_bucket = str(agent_payload.get("after_bucket") or agent_payload.get("afterBucket") or "top").strip() or "top"
    verify_plan = agent_payload.get("verify_plan") if isinstance(agent_payload.get("verify_plan"), dict) else agent_payload.get("verifyPlan") if isinstance(agent_payload.get("verifyPlan"), dict) else {}
    configured_factors = open_factor_context.get("configured_factors")
    normalized_configured_factors = (
        _configured_world_knowledge_factors(open_factor_context)
        if isinstance(configured_factors, list)
        else None
    )
    unavailable_factors = (
        _runtime_unavailable_world_knowledge_factors(open_factor_context, normalized_configured_factors)
        if normalized_configured_factors is not None
        else []
    )
    available_factors = (
        [factor for factor in normalized_configured_factors if factor not in unavailable_factors]
        if normalized_configured_factors is not None
        else None
    )
    considered_factors, factor_assessments, factor_audit_issues = _normalize_world_knowledge_factor_audit(
        agent_payload,
        configured_factors=available_factors,
    )
    if normalized_configured_factors is not None:
        assessment_by_type = {
            str(item.get("type") or ""): item
            for item in factor_assessments
            if isinstance(item, dict)
        }
        for factor in unavailable_factors:
            assessment_by_type[factor] = {
                "type": factor,
                "status": "unavailable",
                "query_relevance": "unknown",
                "evidence": [f"{factor} 来源运行时不可用，未纳入本次判断"],
                "expected_impact": "该来源未参与本次标签权重判断",
                "candidate_tags": [],
            }
        factor_assessments = [
            assessment_by_type[factor]
            for factor in normalized_configured_factors
            if factor in assessment_by_type
        ]
    if "config_plan" in agent_payload:
        requested_config_plan = agent_payload.get("config_plan")
    elif "configPlan" in agent_payload:
        requested_config_plan = agent_payload.get("configPlan")
    else:
        requested_config_plan = []
    if not isinstance(requested_config_plan, list):
        payload_schema_issues.append("config_plan 必须是列表")
        requested_config_plan = []

    audit_issues = [*payload_schema_issues, *factor_audit_issues]
    all_configured_factors_unavailable = bool(normalized_configured_factors) and not available_factors
    if audit_issues:
        decision = "manual_review"
        selected_tags = []
        decision_reason = "世界知识评估信息不完整，需人工复核后再决定是否执行标签权重调整"
    elif all_configured_factors_unavailable:
        decision = "manual_review"
        selected_tags = []
        decision_reason = "已配置的世界知识来源暂不可用，需人工复核后再决定是否执行标签权重调整"
    elif _tag_queries_are_unstable(current_config_record, validation or {}):
        decision = "manual_review"
        decision_reason = "世界知识相关只读查询结果不稳定，需要人工复核后再决定是否调整标签权重"
    elif decision == "execute" and not selected_tags:
        decision = "cannot_execute"
        decision_reason = "Agent 未给出可验证的执行标签或配置，暂时无法调整标签权重"
    elif _existing_config_covers_selected_tags(existing_config, selected_tags):
        decision = "observe"
        decision_reason = "现有 Query 标签权重配置已覆盖本次世界知识建议标签"
    elif requested_selected_tags and not selected_tags:
        decision = "cannot_execute"
        decision_reason = "Agent 推荐标签未通过查询验证，暂时无法形成可执行配置"

    if not audit_issues:
        semantic_guard = _world_knowledge_semantic_guard_decision(
            context,
            selected_tags,
            tag_bucket_payload,
            existing_config,
            validation or {},
        )
        if semantic_guard:
            decision = str(semantic_guard.get("decision") or decision)
            decision_reason = str(semantic_guard.get("decision_reason") or decision_reason)
            expected_impact = str(semantic_guard.get("expected_impact") or expected_impact)
            confidence = float(semantic_guard.get("confidence") or confidence or 0.35)
            risk_level = str(semantic_guard.get("risk_level") or risk_level or "medium")
        elif (
            not tag_bucket_payload
            and _world_knowledge_query_has_precise_existing_tag(context, tag_bucket_payload)
            and _is_already_covered_by_products(context.get("products") or [], [str(_world_knowledge_query_profile(context, tag_bucket_payload).get("keyword") or "").strip()])
        ):
            decision = "skip"
            decision_reason = "当前 Query 已有更精确的标签语义，且 Top 商品已自然覆盖，无需追加通用世界知识标签"
    if not selected_tags:
        requested_config_plan = []
    config_plan = _normalize_world_knowledge_config_plan(
        selected_tags,
        after_bucket,
        raw_plan=requested_config_plan if isinstance(requested_config_plan, list) else [],
    )
    payload_preview = _apply_world_knowledge_config_plan(normalized_payload_preview, config_plan, after_bucket)

    normalized_decision = _world_knowledge_decision_payload(WorldKnowledgeDecision(
        decision=decision or "manual_review",
        should_adjust=decision == "execute",
        confidence=confidence or 0.35,
        decision_reason=decision_reason or "世界知识标签权重决策信息不足，需人工复核",
        considered_factors=considered_factors,
        factor_assessments=factor_assessments,
        audit_status="invalid" if audit_issues else "unavailable" if all_configured_factors_unavailable else "complete",
        audit_issues=list(dict.fromkeys(audit_issues)),
        candidate_tags=candidate_tags,
        selected_tags=selected_tags,
        config_plan=config_plan,
        rejected_tags=rejected_tags,
        existing_config_hit=_existing_config_covers_selected_tags(existing_config, selected_tags),
        config_mode="merge_update" if decision == "execute" and existing_config else "add" if decision == "execute" else "skip",
        after_bucket=after_bucket,
        payload_preview=payload_preview,
        expected_impact=expected_impact or "待进一步验证世界知识标签权重调整对结果集的影响",
        risk_level=risk_level or "medium",
        verify_plan=verify_plan or {"expect_result_set_change": decision == "execute"},
    ))
    normalized_decision["unavailable_factors"] = unavailable_factors
    normalized_decision["candidate_tag_trace"] = candidate_tag_trace
    return normalized_decision


def _validated_agent_selected_tags(
    validation: Optional[Dict[str, Any]],
    requested_tags: List[str],
) -> List[str]:
    requested = [
        tag for tag in dict.fromkeys(str(tag).strip() for tag in requested_tags or [])
        if tag
    ]
    if not requested or not isinstance(validation, dict):
        return []
    if str(validation.get("status") or "").strip().lower() != "available":
        return []
    explicitly_existing = {
        str(item.get("normalized_name") or item.get("name") or "").strip()
        for item in validation.get("existing_tags") or []
        if isinstance(item, dict)
        and item.get("available") is not False
        and str(item.get("normalized_name") or item.get("name") or "").strip()
    }
    return [tag for tag in requested if tag in explicitly_existing]


def _world_knowledge_candidate_tag_trace(
    *,
    source: str,
    candidates: List[str],
    queried: bool,
    validation: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    existing_tags = []
    missing_tags = []
    if isinstance(validation, dict):
        existing_tags = [
            str(item.get("normalized_name") or item.get("name") or "").strip()
            for item in validation.get("existing_tags") or []
            if isinstance(item, dict) and str(item.get("normalized_name") or item.get("name") or "").strip()
        ]
        missing_tags = [
            str(item.get("normalized_name") or item.get("name") or "").strip()
            for item in validation.get("missing_tags") or []
            if isinstance(item, dict) and str(item.get("normalized_name") or item.get("name") or "").strip()
        ]
        status = str(validation.get("status") or "unknown").strip() or "unknown"
    else:
        status = "not_queried"
    return {
        "source": source,
        "candidates": list(dict.fromkeys(candidates)),
        "queried": queried,
        "status": status,
        "existing_tags": list(dict.fromkeys(existing_tags)),
        "missing_tags": list(dict.fromkeys(missing_tags)),
    }


def _normalize_world_knowledge_factor_audit(
    agent_payload: Dict[str, Any],
    configured_factors: Optional[List[str]] = None,
) -> tuple[List[str], List[Dict[str, Any]], List[str]]:
    allowed = set(WORLD_KNOWLEDGE_FACTOR_TYPES)
    factor_source = WORLD_KNOWLEDGE_FACTOR_TYPES if configured_factors is None else configured_factors
    required = [
        str(item).strip()
        for item in factor_source
        if str(item).strip() in allowed
    ]
    required = list(dict.fromkeys(required))
    raw_considered = agent_payload.get("considered_factors") or agent_payload.get("consideredFactors") or []
    considered = [str(item).strip() for item in raw_considered if str(item).strip()] if isinstance(raw_considered, list) else []
    issues: List[str] = []
    if len(considered) != len(required) or set(considered) != set(required):
        issues.append("考虑信号必须完整包含已配置的世界知识来源")

    raw_assessments = agent_payload.get("factor_assessments") or agent_payload.get("factorAssessments") or []
    if not isinstance(raw_assessments, list):
        raw_assessments = []
        issues.append("factor_assessments 必须是列表")
    by_type: Dict[str, Dict[str, Any]] = {}
    duplicate_types = set()
    for item in raw_assessments:
        if not isinstance(item, dict):
            issues.append("factor_assessments 包含非对象项")
            continue
        factor_type = str(item.get("type") or "").strip()
        if factor_type not in required:
            continue
        if factor_type in by_type:
            duplicate_types.add(factor_type)
        elif factor_type:
            by_type[factor_type] = item
    if duplicate_types:
        issues.append("信号评估 type 重复: " + ",".join(sorted(duplicate_types)))
    normalized: List[Dict[str, Any]] = []
    for factor_type in required:
        item = by_type.get(factor_type)
        item_issues: List[str] = []
        if item is None:
            item_issues.append(f"缺少 {factor_type} 评估")
            item = {}
        status = str(item.get("status") or "").strip()
        query_relevance = str(item.get("query_relevance") or item.get("queryRelevance") or "").strip()
        expected_impact = str(item.get("expected_impact") or item.get("expectedImpact") or "").strip()
        evidence = item.get("evidence")
        candidate_tags = item.get("candidate_tags") if "candidate_tags" in item else item.get("candidateTags")
        if not status:
            item_issues.append(f"{factor_type}.status 缺失")
        if not query_relevance:
            item_issues.append(f"{factor_type}.query_relevance 缺失")
        if not isinstance(evidence, (list, str)):
            item_issues.append(f"{factor_type}.evidence 必须是列表或字符串")
        if not expected_impact:
            item_issues.append(f"{factor_type}.expected_impact 缺失")
        if not isinstance(candidate_tags, list):
            item_issues.append(f"{factor_type}.candidate_tags 必须是列表")
        issues.extend(item_issues)
        normalized.append({
            "type": factor_type,
            "status": status or "not_assessed",
            "query_relevance": query_relevance or "unknown",
            "evidence": evidence if isinstance(evidence, (list, str)) else item_issues,
            "expected_impact": expected_impact or "unknown",
            "candidate_tags": [str(tag).strip() for tag in candidate_tags or [] if str(tag).strip()] if isinstance(candidate_tags, list) else [],
        })
    return required, normalized, list(dict.fromkeys(issues))


def _normalized_tag_bucket_payload_preview(tag_bucket_payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(tag_bucket_payload, dict):
        return {}
    payload = dict(tag_bucket_payload)
    keyword = str(payload.get("keyword") or "").strip()
    parts = keyword.split()
    gender_map = {"女士": "WOMAN", "男士": "MAN", "儿童": "KID", "家居": "HOME"}
    filters = payload.get("filters") if isinstance(payload.get("filters"), list) else []
    normalized_filters = []
    seen = set()
    for item in filters:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "")
        value = str(item.get("value") or "")
        if not field or not value:
            continue
        key = (field, value)
        if key in seen:
            continue
        seen.add(key)
        normalized_filters.append({"field": field, "value": value})
    if parts and parts[-1] in gender_map:
        refined_keyword = " ".join(parts[:-1]).strip()
        if refined_keyword:
            payload["keyword"] = refined_keyword
        derived = ("gender", gender_map[parts[-1]])
        if derived not in seen:
            normalized_filters.append({"field": derived[0], "value": derived[1]})
    payload["filters"] = normalized_filters
    return payload


def _invoke_with_timeout(invoke_fn: Any, messages: List[Dict[str, Any]], timeout_seconds: float) -> Any:
    try:
        return invoke_fn(messages, timeout=timeout_seconds)
    except TypeError as exc:
        message = str(exc)
        if "unexpected keyword argument" in message or "positional arguments" in message:
            return invoke_fn(messages)
        raise


def _sanitize_agent_failure_reason(value: str, max_length: int = 500) -> str:
    sanitized = re.sub(r"https?://[^\s]+", "[REDACTED]", value, flags=re.IGNORECASE)
    sanitized = re.sub(
        r"\bBearer\s+[^\s,;]+",
        "Bearer [REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"([\"']?(?:token|api[_-]?key|password|secret)[\"']?\s*[:=]\s*)([\"'])(.*?)\2",
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(2)}",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"([\"']?(?:token|api[_-]?key|password|secret)[\"']?\s*[:=]\s*)(?![\"'])[^\s,;}\]]+",
        lambda match: f"{match.group(1)}[REDACTED]",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized[:max_length]


def _normalized_executor_llm_metrics(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    allowed_top_level = {
        "llmUsed", "fallbackUsed", "tokenUsage", "totalTokenUsage", "totalTokens",
        "attemptCount", "timeoutCount", "totalDurationSeconds", "errorType",
        "fallbackReason", "model", "modelName", "attempts",
    }
    allowed_attempt_fields = {
        "attempt", "attemptType", "model", "modelName", "modelRole", "nodeKey", "channel",
        "configSource", "timeoutSeconds",
        "duration", "durationSeconds", "requestChars", "responseChars", "tokenUsage",
        "totalTokens", "inputTokens", "outputTokens", "errorType", "errorCode",
        "fallback", "fallbackUsed", "compact", "status", "success",
    }
    metrics = {
        key: item
        for key, item in value.items()
        if key in allowed_top_level and key != "attempts"
    }
    raw_attempts = value.get("attempts") if isinstance(value.get("attempts"), list) else []
    attempts = [
        {key: item for key, item in attempt.items() if key in allowed_attempt_fields}
        for attempt in raw_attempts
        if isinstance(attempt, dict)
    ]
    metrics["attempts"] = attempts
    metrics["attemptCount"] = int(metrics.get("attemptCount") or len(attempts))
    metrics["llmUsed"] = bool(metrics.get("llmUsed", metrics["attemptCount"] > 0))
    metrics["fallbackUsed"] = bool(
        metrics.get("fallbackUsed")
        or any(
            bool(attempt.get("fallbackUsed"))
            or str(attempt.get("attemptType") or "").lower() == "fallback"
            for attempt in attempts
        )
    )
    metrics["tokenUsage"] = _executor_token_usage(metrics)
    return metrics


def _executor_token_usage(metrics: Any) -> int:
    if not isinstance(metrics, dict):
        return 0
    attempts = metrics.get("attempts") if isinstance(metrics.get("attempts"), list) else []
    attempt_total = sum(
        _safe_world_knowledge_token_count(attempt.get("tokenUsage") or attempt.get("totalTokens"))
        for attempt in attempts
        if isinstance(attempt, dict)
    )
    if attempt_total:
        return attempt_total
    for key in ["tokenUsage", "totalTokenUsage", "totalTokens"]:
        if metrics.get(key) is not None:
            return _safe_world_knowledge_token_count(metrics.get(key))
    return 0


def _world_knowledge_attempt_metrics(
    *,
    llm: Any,
    attempt_type: str,
    compact: bool,
    timeout_seconds: float,
    duration_seconds: float,
    messages: List[Dict[str, Any]],
    response: Any,
    error: Optional[Exception],
    model_config: Optional[ResolvedNodeConfig] = None,
) -> Dict[str, Any]:
    response_text = str(getattr(response, "content", response) or "") if response is not None else ""
    attempt = {
        "model": (
            getattr(llm, "fallback_model_name", None)
            or os.getenv("TOP_QUERY_WORLD_KNOWLEDGE_FALLBACK_MODEL")
            or "qwen3.7-max"
            if attempt_type == "fallback"
            else getattr(llm, "primary_model_name", None)
            or os.getenv("TOP_QUERY_WORLD_KNOWLEDGE_MODEL")
            or "qwen3.6-plus"
        ),
        "timeoutSeconds": timeout_seconds,
        "durationSeconds": round(max(duration_seconds, 0.0), 6),
        "requestChars": sum(len(str(item.get("content") or "")) for item in messages if isinstance(item, dict)),
        "responseChars": len(response_text),
        "tokenUsage": _world_knowledge_token_usage(response),
        "errorType": type(error).__name__ if error is not None else None,
        "attemptType": attempt_type,
        "compact": compact,
    }
    if model_config is not None:
        attempt.update(model_audit_fields(model_config, attempt_type))
    return attempt


def _world_knowledge_agent_metrics(attempts: List[Dict[str, Any]]) -> Dict[str, Any]:
    copied_attempts = [dict(item) for item in attempts]
    return {
        "attempts": copied_attempts,
        "attemptCount": len(copied_attempts),
        "timeoutCount": sum(
            1 for item in copied_attempts
            if "timeout" in str(item.get("errorType") or "").lower()
        ),
        "totalDurationSeconds": round(sum(float(item.get("durationSeconds") or 0) for item in copied_attempts), 6),
    }


def _world_knowledge_token_usage(response: Any) -> int:
    if response is None:
        return 0
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        value = usage.get("total_tokens") or usage.get("totalTokenCount") or usage.get("total_token_count")
        if value is not None:
            return _safe_world_knowledge_token_count(value)
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, dict):
        usage = metadata.get("token_usage") or metadata.get("usage") or {}
        if isinstance(usage, dict):
            value = usage.get("total_tokens") or usage.get("totalTokenCount") or usage.get("total_token_count")
            if value is not None:
                return _safe_world_knowledge_token_count(value)
    return 0


def _safe_world_knowledge_token_count(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_attach_world_knowledge_agent_metrics(
    exc: Exception,
    agent_metrics: Dict[str, Any],
) -> bool:
    try:
        setattr(exc, "agent_metrics", agent_metrics)
        return True
    except Exception:
        return False


def _world_knowledge_query_profile(
    context: Dict[str, Any],
    tag_bucket_payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    normalized_preview = _normalized_tag_bucket_payload_preview(tag_bucket_payload)
    raw_query = str(context.get("query") or "").strip()
    filters = normalized_preview.get("filters") if isinstance(normalized_preview.get("filters"), list) else []
    gender = None
    for item in filters:
        if not isinstance(item, dict):
            continue
        if str(item.get("field") or "").strip() == "gender":
            gender = str(item.get("value") or "").strip() or None
            break
    return {
        "raw_query": raw_query,
        "keyword": str(normalized_preview.get("keyword") or raw_query).strip(),
        "gender": gender,
        "filters": filters,
    }


def _world_knowledge_product_evidence_summary(
    products: List[Dict[str, Any]],
    candidate_tags: List[Any],
) -> Dict[str, Any]:
    top_tag_names: List[str] = []
    candidate_tag_matches: Dict[str, Dict[str, Any]] = {}
    normalized_candidates = [
        str(tag).strip()
        for tag in dict.fromkeys(candidate_tags or [])
        if str(tag).strip()
    ]
    for product in products[:8]:
        tag_name = str(product.get("tag_name") or product.get("tagName") or "").strip()
        if tag_name and tag_name not in top_tag_names:
            top_tag_names.append(tag_name)
        searchable_text = " ".join([
            str(product.get("tag_name") or product.get("tagName") or ""),
            str(product.get("name") or product.get("productName") or product.get("title") or ""),
        ])
        for candidate in normalized_candidates:
            if candidate not in searchable_text:
                continue
            match = candidate_tag_matches.setdefault(candidate, {"count": 0, "sample_products": []})
            match["count"] += 1
            sample_name = str(product.get("name") or product.get("productName") or product.get("title") or "").strip()
            if sample_name and sample_name not in match["sample_products"] and len(match["sample_products"]) < 3:
                match["sample_products"].append(sample_name)
    return {
        "top_product_count": len(products[:8]),
        "top_tag_names": top_tag_names,
        "candidate_tag_matches": candidate_tag_matches,
    }


def _world_knowledge_seasonal_fit_summary(
    query: str,
    products: List[Dict[str, Any]],
    season: str,
    candidate_tags: List[str],
) -> Dict[str, Any]:
    product_items = []
    summer_fit_tokens = ["轻薄", "无袖", "短袖", "吊带", "亚麻", "棉麻", "透气", "凉感", "背心"]
    summer_mismatch_tokens = ["厚", "针织", "长袖", "毛", "羊毛", "厚重", "保暖", "羽绒", "大衣"]
    normalized_candidate_tags = [
        str(tag).strip()
        for tag in dict.fromkeys(candidate_tags or [])
        if str(tag).strip()
    ]
    for product in (products or [])[:8]:
        text = " ".join([
            str(product.get("name") or product.get("productName") or product.get("title") or ""),
            str(product.get("tag_name") or product.get("tagName") or ""),
        ])
        fit_hits = [token for token in summer_fit_tokens if token in text]
        mismatch_hits = [token for token in summer_mismatch_tokens if token in text]
        candidate_hits = [tag for tag in normalized_candidate_tags if tag in text]
        product_items.append({
            "product_id": product.get("product_id") or product.get("productId") or product.get("sku_id"),
            "name": product.get("name") or product.get("productName") or product.get("title"),
            "fit_hits": fit_hits,
            "mismatch_hits": mismatch_hits,
            "candidate_hits": candidate_hits,
        })
    total = len(product_items)
    fit_count = sum(1 for item in product_items if item["fit_hits"])
    mismatch_count = sum(1 for item in product_items if item["mismatch_hits"])
    candidate_hit_count = sum(1 for item in product_items if item["candidate_hits"])
    return {
        "season": season,
        "query": query,
        "total_products": total,
        "fit_count": fit_count,
        "mismatch_count": mismatch_count,
        "candidate_hit_count": candidate_hit_count,
        "fit_ratio": round(fit_count / total, 4) if total else 0,
        "mismatch_ratio": round(mismatch_count / total, 4) if total else 0,
        "items": product_items,
    }


def _world_knowledge_products_for_agent(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summarized: List[Dict[str, Any]] = []
    for product in products[:8]:
        summarized.append({
            "rank": product.get("rank"),
            "product_id": product.get("product_id") or product.get("productId") or product.get("sku_id") or product.get("skuId"),
            "name": product.get("name") or product.get("productName") or product.get("title"),
            "tag_name": product.get("tag_name") or product.get("tagName"),
            "price": product.get("price"),
            "bucket": product.get("bucket") or (product.get("ext") or {}).get("bucket") if isinstance(product.get("ext"), dict) else product.get("bucket"),
        })
    return summarized

def _world_knowledge_open_factor_for_agent(open_factor_context: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(open_factor_context, dict):
        return {}
    weather = open_factor_context.get("weather") if isinstance(open_factor_context.get("weather"), dict) else {}
    festival = open_factor_context.get("festival") if isinstance(open_factor_context.get("festival"), dict) else {}
    social_trend = open_factor_context.get("social_trend") if isinstance(open_factor_context.get("social_trend"), dict) else {}
    has_explicit_configured_factors = isinstance(open_factor_context.get("configured_factors"), list)
    configured_factors = _configured_world_knowledge_factors(open_factor_context)
    available_factors = [
        factor for factor in configured_factors
        if factor not in _runtime_unavailable_world_knowledge_factors(open_factor_context, configured_factors)
    ]
    visible_factors = set(available_factors if has_explicit_configured_factors else WORLD_KNOWLEDGE_FACTOR_TYPES)
    return {
        "season": open_factor_context.get("season"),
        "configured_factors": available_factors if has_explicit_configured_factors else [],
        "matched_factors": open_factor_context.get("matched_factors") or [],
        "suggested_tags": open_factor_context.get("suggested_tags") or [],
        "weather": {
            "status": weather.get("status"),
            "cities": [
                {
                    "city": item.get("city"),
                    "weather": item.get("weather") or item.get("condition"),
                    "temperature": item.get("temperature"),
                }
                for item in weather.get("cities") or []
                if isinstance(item, dict)
            ][:4],
        } if weather and "weather" in visible_factors else {},
        "festival": {
            "status": festival.get("status"),
            "items": [
                {
                    "name": item.get("name"),
                    "type": item.get("type"),
                }
                for item in festival.get("items") or []
                if isinstance(item, dict)
            ][:4],
        } if festival and "festival" in visible_factors else {},
        "social_trend": {
            "status": social_trend.get("status"),
            "items": [
                {
                    "description": item.get("description"),
                    "keywords": item.get("keywords") or item.get("query_patterns"),
                    "tags": item.get("tags"),
                }
                for item in social_trend.get("items") or []
                if isinstance(item, dict)
            ][:4],
        } if social_trend and "social_trend" in visible_factors else {},
    }


def _configured_world_knowledge_factors(open_factor_context: Dict[str, Any]) -> List[str]:
    return list(dict.fromkeys(
        str(item).strip()
        for item in open_factor_context.get("configured_factors") or []
        if str(item).strip() in WORLD_KNOWLEDGE_FACTOR_TYPES
    ))


def _runtime_unavailable_world_knowledge_factors(
    open_factor_context: Dict[str, Any],
    configured_factors: List[str],
) -> List[str]:
    return [
        factor for factor in configured_factors
        if isinstance(open_factor_context.get(factor), dict)
        and str(open_factor_context[factor].get("status") or "").strip().lower() == "unavailable"
    ]


def _world_knowledge_tag_validation_for_agent(validation: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(validation, dict):
        return {}
    return {
        "status": validation.get("status"),
        "existing_tags": [
            {
                "name": item.get("name"),
                "normalized_name": item.get("normalized_name") or item.get("name"),
                "available": item.get("available"),
            }
            for item in validation.get("existing_tags") or []
            if isinstance(item, dict)
        ],
        "missing_tags": [
            {
                "name": item.get("name"),
                "normalized_name": item.get("normalized_name") or item.get("name"),
                "available": item.get("available"),
            }
            for item in validation.get("missing_tags") or []
            if isinstance(item, dict)
        ],
    }


def _world_knowledge_current_config_for_agent(current_config: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(current_config, dict):
        return {}
    config = current_config.get("config") if isinstance(current_config.get("config"), dict) else {}
    return {
        "status": current_config.get("status"),
        "matched": current_config.get("matched"),
        "config": {
            "keyword": config.get("keyword"),
            "filters": config.get("filters") if isinstance(config.get("filters"), list) else [],
            "configs": [
                {
                    "tags": item.get("tags"),
                    "afterBucket": item.get("afterBucket"),
                }
                for item in config.get("configs") or []
                if isinstance(item, dict)
            ],
        } if config else {},
    }


def _world_knowledge_agent_failure_decision(
    context: Dict[str, Any],
    open_factor_context: Dict[str, Any],
    tag_bucket_payload: Optional[Dict[str, Any]],
    failure_reason: str,
    agent_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized_payload_preview = _normalized_tag_bucket_payload_preview(tag_bucket_payload)
    candidate_tags = [str(tag).strip() for tag in open_factor_context.get("suggested_tags") or [] if str(tag).strip()]
    after_bucket = _resolve_world_knowledge_after_bucket(tag_bucket_payload)
    selected_tags: List[str] = []
    config_plan: List[Dict[str, Any]] = []
    payload_preview = _apply_world_knowledge_config_plan(normalized_payload_preview, [], after_bucket)
    payload = _world_knowledge_decision_payload(WorldKnowledgeDecision(
        decision="manual_review",
        should_adjust=False,
        confidence=0.2,
        decision_reason=f"世界知识 Agent 调用失败，暂不自动调整标签权重: {failure_reason}",
        audit_status="unavailable",
        candidate_tags=candidate_tags,
        selected_tags=selected_tags,
        config_plan=config_plan,
        rejected_tags=[],
        existing_config_hit=False,
        config_mode="review",
        after_bucket=after_bucket,
        payload_preview=payload_preview,
        expected_impact="待 Agent 恢复后重新判断是否需要执行标签权重调整",
        risk_level="medium",
        verify_plan={"expect_result_set_change": False},
    ))
    payload["agent_failure"] = {
        "reason": failure_reason,
        "timeout_seconds": float(os.getenv("TOP_QUERY_WORLD_KNOWLEDGE_TIMEOUT_SECONDS", "120")),
    }
    if isinstance(agent_metrics, dict):
        payload["agent_metrics"] = agent_metrics
        if agent_metrics.get("errorType"):
            payload["agent_failure"]["error_type"] = agent_metrics["errorType"]
    return payload


def _tag_bucket_payload_from_world_knowledge_decision(
    world_knowledge_decision: Dict[str, Any],
    tag_bucket_payload: Optional[Dict[str, Any]],
    query: Any,
) -> Optional[Dict[str, Any]]:
    if str(world_knowledge_decision.get("decision") or "") != "execute":
        return None
    config_plan = world_knowledge_decision.get("config_plan")
    if isinstance(config_plan, list) and config_plan:
        preview_seed = world_knowledge_decision.get("payload_preview")
        payload_preview = dict(preview_seed) if isinstance(preview_seed, dict) else _normalized_tag_bucket_payload_preview(tag_bucket_payload)
        payload_preview["configs"] = [dict(item) for item in config_plan if isinstance(item, dict)]
        payload = _tag_bucket_config_payload({"tag_bucket_config": payload_preview, "query": query})
        if payload:
            return payload
    payload_preview = world_knowledge_decision.get("payload_preview")
    if isinstance(payload_preview, dict):
        payload = _tag_bucket_config_payload({"tag_bucket_config": payload_preview, "query": query})
        if payload:
            return payload
    return _tag_bucket_config_payload({"tag_bucket_config": tag_bucket_payload, "query": query})


def _validated_candidate_tags(validation: Dict[str, Any], candidate_tags: List[str]) -> List[str]:
    if not isinstance(validation, dict):
        return candidate_tags
    existing_tags = [
        str(item.get("normalized_name") or item.get("name") or "").strip()
        for item in validation.get("existing_tags") or []
        if isinstance(item, dict) and str(item.get("normalized_name") or item.get("name") or "").strip()
    ]
    if existing_tags:
        if candidate_tags:
            existing_set = set(existing_tags)
            return [
                tag for tag in dict.fromkeys(str(tag).strip() for tag in candidate_tags)
                if tag and tag in existing_set
            ]
        return existing_tags
    return candidate_tags if validation.get("status") in {None, "not_found"} else []


def _payload_selected_tags(tag_bucket_payload: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(tag_bucket_payload, dict):
        return []
    tags: List[str] = []
    for item in tag_bucket_payload.get("configs") or []:
        if not isinstance(item, dict):
            continue
        raw_tags = str(item.get("tags") or "")
        for tag in raw_tags.replace(";", ",").split(","):
            normalized = tag.strip()
            if normalized and normalized not in tags:
                tags.append(normalized)
    return tags


def _resolve_world_knowledge_after_bucket(tag_bucket_payload: Optional[Dict[str, Any]]) -> str:
    if isinstance(tag_bucket_payload, dict):
        for item in tag_bucket_payload.get("configs") or []:
            if isinstance(item, dict):
                after_bucket = str(item.get("afterBucket") or "").strip()
                if after_bucket:
                    return after_bucket
        default_after_bucket = str(
            tag_bucket_payload.get("default_after_bucket")
            or tag_bucket_payload.get("defaultAfterBucket")
            or ""
        ).strip()
        if default_after_bucket:
            return default_after_bucket
    return "top"


def _normalize_world_knowledge_config_plan(
    selected_tags: List[str],
    after_bucket: str,
    raw_plan: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    normalized_after_bucket = str(after_bucket or "top").strip() or "top"
    unique_selected_tags = [tag for tag in dict.fromkeys(str(tag).strip() for tag in selected_tags or []) if tag]
    normalized: List[Dict[str, str]] = []
    seen = set()
    has_raw_plan = isinstance(raw_plan, list) and bool(raw_plan)

    def append_config(tags_text: str, bucket: str) -> None:
        parts = [tag.strip() for tag in str(tags_text or "").replace(";", ",").split(",") if tag.strip()]
        if not parts:
            return
        if unique_selected_tags and not set(parts).issubset(set(unique_selected_tags)):
            return
        if _world_knowledge_combo_has_conflict(parts):
            return
        normalized_tags = ",".join(dict.fromkeys(parts))
        normalized_bucket = str(bucket or normalized_after_bucket).strip() or normalized_after_bucket
        key = (normalized_tags, normalized_bucket)
        if key in seen:
            return
        seen.add(key)
        normalized.append({"tags": normalized_tags, "afterBucket": normalized_bucket})

    if isinstance(raw_plan, list):
        for item in raw_plan:
            if not isinstance(item, dict):
                continue
            append_config(item.get("tags") or "", item.get("afterBucket") or normalized_after_bucket)

    if unique_selected_tags and not has_raw_plan:
        append_config(",".join(unique_selected_tags), normalized_after_bucket)
    for tag in unique_selected_tags:
        append_config(tag, normalized_after_bucket)
    return normalized


def _world_knowledge_combo_has_conflict(tags: List[str]) -> bool:
    family_counts: Dict[str, int] = {}
    for tag in tags:
        family = _world_knowledge_tag_family(tag)
        if not family:
            continue
        family_counts[family] = family_counts.get(family, 0) + 1
        if family_counts[family] > 1:
            return True
    return False


def _world_knowledge_tag_family(tag: str) -> Optional[str]:
    normalized = str(tag or "").strip().lower()
    sleeve_tags = {
        "无袖",
        "短袖",
        "中袖",
        "长袖",
        "半袖",
        "七分袖",
        "五分袖",
    }
    if normalized in sleeve_tags:
        return "sleeve_length"
    return None


def _apply_world_knowledge_config_plan(
    payload_preview: Dict[str, Any],
    config_plan: List[Dict[str, Any]],
    after_bucket: str,
) -> Dict[str, Any]:
    payload = dict(payload_preview or {})
    normalized_plan = _normalize_world_knowledge_config_plan(
        [],
        after_bucket,
        raw_plan=config_plan,
    )
    if normalized_plan:
        payload["configs"] = normalized_plan
    elif payload.get("configs"):
        payload["configs"] = [
            {
                "tags": str(item.get("tags") or ""),
                "afterBucket": str(item.get("afterBucket") or after_bucket),
            }
            for item in payload.get("configs") or []
            if isinstance(item, dict)
        ]
    return payload


def _tag_queries_are_unstable(current_config_record: Dict[str, Any], validation: Dict[str, Any]) -> bool:
    return str(current_config_record.get("status") or "").lower() == "error" or str(validation.get("status") or "").lower() == "error"


def _world_knowledge_semantic_guard_decision(
    context: Dict[str, Any],
    selected_tags: List[str],
    tag_bucket_payload: Optional[Dict[str, Any]],
    existing_config: Dict[str, Any],
    validation: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not selected_tags:
        return None
    if _existing_config_covers_selected_tags(existing_config, selected_tags):
        return None
    query_profile = _world_knowledge_query_profile(context, tag_bucket_payload)
    keyword = str(query_profile.get("keyword") or "").strip()
    if not keyword:
        return None
    category = _world_knowledge_query_category(keyword)
    if _world_knowledge_has_generic_summer_tags(selected_tags) and category in {"promotion", "attribute", "non_apparel", "bottoms", "outerwear"}:
        return {
            "decision": "skip",
            "decision_reason": "当前 Query 语义与夏季袖长/轻薄标签不稳定匹配，无需机械追加通用世界知识标签",
            "expected_impact": "保持当前结果集，避免错误放大不适配的季节标签",
            "confidence": 0.76,
            "risk_level": "low",
        }
    if (
        not tag_bucket_payload
        and _world_knowledge_query_has_precise_existing_tag(context, tag_bucket_payload)
        and _is_already_covered_by_products(context.get("products") or [], [keyword])
    ):
        return {
            "decision": "skip",
            "decision_reason": "当前 Query 已有更精确的标签语义，且 Top 商品已自然覆盖，无需追加通用世界知识标签",
            "expected_impact": "保持当前结果集并继续观察后续指标变化",
            "confidence": 0.72,
            "risk_level": "low",
        }
    return None


def _world_knowledge_query_has_precise_existing_tag(
    context: Dict[str, Any],
    tag_bucket_payload: Optional[Dict[str, Any]],
) -> bool:
    validation = context.get("query_semantic_tag_validation") if isinstance(context.get("query_semantic_tag_validation"), dict) else {}
    keyword = str(_world_knowledge_query_profile(context, tag_bucket_payload).get("keyword") or "").strip()
    if not keyword:
        return False
    for item in validation.get("existing_tags") or []:
        if not isinstance(item, dict):
            continue
        normalized_name = str(item.get("normalized_name") or item.get("name") or "").strip()
        if normalized_name == keyword:
            return True
    return False


def _world_knowledge_has_generic_summer_tags(tags: List[str]) -> bool:
    normalized = {str(tag or "").strip() for tag in tags if str(tag or "").strip()}
    return bool(normalized) and normalized.issubset({"无袖", "短袖", "轻薄"})


def _world_knowledge_query_category(keyword: str) -> str:
    normalized = str(keyword or "").strip()
    if not normalized:
        return "unknown"
    if any(token in normalized for token in ["五折", "折扣", "精选", "优惠", "特价"]):
        return "promotion"
    if any(token in normalized for token in ["香水", "饰品", "耳环", "项链"]):
        return "non_apparel"
    if any(token in normalized for token in ["白色", "黑色", "条纹", "纯色", "印花"]):
        return "attribute"
    if any(token in normalized for token in ["外套", "夹克", "大衣", "风衣"]):
        return "outerwear"
    if any(token in normalized for token in ["牛仔裤", "长裤", "半裙", "短裙"]):
        return "bottoms"
    return "apparel"


def _is_already_covered_by_products(products: List[Dict[str, Any]], selected_tags: List[str]) -> bool:
    if not products or not selected_tags:
        return False
    product_text = " ".join(
        str(
            product.get("tag_name")
            or product.get("tagName")
            or ""
        ) + " " + str(
            product.get("productName")
            or product.get("name")
            or product.get("title")
            or ""
        )
        for product in products
    )
    return all(tag and tag in product_text for tag in selected_tags)


def _world_knowledge_decision_payload(decision: WorldKnowledgeDecision) -> Dict[str, Any]:
    return {
        "decision": decision.decision,
        "should_adjust": decision.should_adjust,
        "confidence": decision.confidence,
        "decision_reason": decision.decision_reason,
        "considered_factors": decision.considered_factors,
        "factor_assessments": decision.factor_assessments,
        "audit_status": decision.audit_status,
        "audit_issues": decision.audit_issues,
        "candidate_tags": decision.candidate_tags,
        "selected_tags": decision.selected_tags,
        "config_plan": decision.config_plan,
        "rejected_tags": decision.rejected_tags,
        "existing_config_hit": decision.existing_config_hit,
        "config_mode": decision.config_mode,
        "after_bucket": decision.after_bucket,
        "payload_preview": decision.payload_preview,
        "expected_impact": decision.expected_impact,
        "risk_level": decision.risk_level,
        "verify_plan": decision.verify_plan,
    }


def _existing_config_covers_selected_tags(existing_config: Dict[str, Any], selected_tags: List[str]) -> bool:
    if not isinstance(existing_config, dict) or not selected_tags:
        return False
    existing_tags_text = ",".join(
        str(item.get("tags") or "")
        for item in existing_config.get("configs") or []
        if isinstance(item, dict)
    )
    return all(tag and tag in existing_tags_text for tag in selected_tags)


def _world_knowledge_kg_intent(
    context: Dict[str, Any],
    open_factor_context: Dict[str, Any],
    tag_bucket_payload: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    missing_tags = [
        str(item).strip()
        for item in open_factor_context.get("missing_kg_tags") or open_factor_context.get("missingKgTags") or []
        if str(item).strip()
    ]
    missing_relations = [
        item for item in open_factor_context.get("missing_kg_relations") or open_factor_context.get("missingKgRelations") or []
        if isinstance(item, dict)
    ]
    if not missing_tags and not missing_relations:
        return None
    return {
        "intent_type": "world_knowledge_missing_tag",
        "brand": context.get("brand"),
        "query": context.get("query"),
        "reason": "世界知识推荐标签需要先补齐知识图谱标签或关系",
        "missing_tags": missing_tags,
        "missing_relations": missing_relations,
        "open_factor_context": open_factor_context,
        "follow_up_tag_bucket_config": tag_bucket_payload,
        "directly_executable": False,
    }


def build_world_knowledge_verification_summary(
    context: Dict[str, Any],
    products: List[Dict[str, Any]],
) -> Dict[str, Any]:
    decision = context.get("world_knowledge_decision") if isinstance(context.get("world_knowledge_decision"), dict) else {}
    selected_tags = [
        str(tag).strip()
        for tag in decision.get("selected_tags") or []
        if str(tag).strip()
    ]
    tag_groups = _world_knowledge_coverage_tag_groups(decision, selected_tags)
    before_products = _summarize_products(context.get("products") or [])
    before_coverage, _, before_covered_count = _world_knowledge_tag_coverage(before_products, tag_groups)
    after_coverage, hit_tags, after_covered_count = _world_knowledge_tag_coverage(products, tag_groups)
    config_verified = _world_knowledge_config_verified(context, decision)
    before_irrelevant = _world_knowledge_irrelevant_count(before_products)
    after_irrelevant = _world_knowledge_irrelevant_count(products)
    irrelevant_product_increase = max(0, after_irrelevant - before_irrelevant)
    if not selected_tags or str(decision.get("decision") or "") != "execute":
        result_effect = "pending"
    elif irrelevant_product_increase > 0:
        result_effect = "worse"
    elif config_verified is False:
        result_effect = "unchanged"
    elif after_coverage >= 0.5 and after_coverage > before_coverage:
        result_effect = "improved"
    else:
        result_effect = "unchanged"
    verification = {
        "selected_tags": selected_tags,
        "before_tag_coverage": before_coverage,
        "after_tag_coverage": after_coverage,
        "before_covered_product_count": before_covered_count,
        "after_covered_product_count": after_covered_count,
        "checked_product_count": len(products or []),
        "coverage_match_mode": "any_config_expression",
        "config_verified": config_verified,
        "irrelevant_product_increase": irrelevant_product_increase,
        "result_effect": result_effect,
    }
    return {
        "world_knowledge_verification": verification,
        "selected_tags_hit_count": len(hit_tags),
        "selected_tags_hit_examples": hit_tags[:3],
        "top_result_tag_coverage": after_coverage,
    }


def _world_knowledge_tag_coverage(
    products: List[Dict[str, Any]],
    tag_groups: List[List[str]],
) -> tuple[float, List[str], int]:
    if not tag_groups:
        return 0.0, [], 0
    matched_product_count = 0
    hit_tags: List[str] = []
    for product in products or []:
        product_text = " ".join([
            str(product.get("productName") or product.get("name") or product.get("title") or ""),
            str(product.get("tag_name") or product.get("tagName") or ""),
        ])
        matched_groups = [
            group for group in tag_groups
            if group and all(tag in product_text for tag in group)
        ]
        if matched_groups:
            matched_product_count += 1
        for group in matched_groups:
            for tag in group:
                if tag not in hit_tags:
                    hit_tags.append(tag)
    coverage = round(matched_product_count / len(products), 4) if products else 0.0
    return coverage, hit_tags, matched_product_count


def _world_knowledge_coverage_tag_groups(
    decision: Dict[str, Any],
    selected_tags: List[str],
) -> List[List[str]]:
    groups: List[List[str]] = []
    for item in decision.get("config_plan") or []:
        if not isinstance(item, dict):
            continue
        group = [tag.strip() for tag in str(item.get("tags") or "").split(",") if tag.strip()]
        if group and group not in groups:
            groups.append(group)
    if groups:
        return groups
    return [[tag] for tag in selected_tags]


def _world_knowledge_config_verified(
    context: Dict[str, Any],
    decision: Dict[str, Any],
) -> Optional[bool]:
    action_verification = context.get("world_knowledge_action_verification")
    if isinstance(action_verification, dict) and isinstance(action_verification.get("config_verified"), bool):
        return action_verification.get("config_verified")
    after_snapshot = context.get("tag_bucket_after_snapshot")
    if not isinstance(after_snapshot, dict):
        return None
    config_record = after_snapshot.get("config") if isinstance(after_snapshot.get("config"), dict) else after_snapshot
    actual_config = config_record.get("config") if isinstance(config_record.get("config"), dict) else config_record
    actual_configs = actual_config.get("configs") if isinstance(actual_config, dict) else None
    expected_configs = decision.get("config_plan") or []
    if not isinstance(actual_configs, list) or not isinstance(expected_configs, list) or not expected_configs:
        return False
    normalized_actual = {
        (str(item.get("tags") or "").strip(), str(item.get("afterBucket") or item.get("after_bucket") or "").strip())
        for item in actual_configs
        if isinstance(item, dict)
    }
    normalized_expected = {
        (str(item.get("tags") or "").strip(), str(item.get("afterBucket") or item.get("after_bucket") or "").strip())
        for item in expected_configs
        if isinstance(item, dict)
    }
    return bool(normalized_expected) and normalized_expected.issubset(normalized_actual)


def _world_knowledge_irrelevant_count(products: List[Dict[str, Any]]) -> int:
    return sum(1 for product in products if isinstance(product, dict) and product.get("is_relevant") is False)


def _kg_action_payload(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = intent.get("kg_action") or intent.get("action") or intent
    if not isinstance(payload, dict):
        return None
    action_type = payload.get("actionType") or payload.get("action_type")
    params = payload.get("params")
    if not action_type or not isinstance(params, dict) or not params:
        return None
    return {"actionType": action_type, "params": params}


def _kg_wrapper_action_type(intent: Dict[str, Any], kg_payload: Dict[str, Any]) -> str:
    action_type = str(kg_payload.get("actionType") or kg_payload.get("action_type") or "")
    if action_type in {"add_tag", "update_tag", "blacklist_tag", "restore_tag"}:
        return "kg_tag"
    if intent.get("intent_type") == "expand_recall":
        return "kg_tag"
    return "kg_relation"


def _strip_json_fence(value: Any) -> str:
    text = str(getattr(value, "content", value) if value is not None else "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _summarize_products(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summarized = []
    for product in products:
        item = {
            "productId": product.get("productId") or product.get("spu") or product.get("skuId"),
            "productName": product.get("productName") or product.get("name"),
            "mainImage": product.get("mainImage") or product.get("main_image") or product.get("imageUrl"),
        }
        for key in ("product_id", "skuId", "sku_id", "spuId", "spu_id", "spu"):
            if product.get(key) is not None:
                item[key] = product.get(key)
        normalized_tag_name = product.get("tag_name") or product.get("tagName")
        if normalized_tag_name is not None:
            item["tag_name"] = normalized_tag_name
        for key in [
            "rank",
            "is_relevant",
            "relevance_score",
            "relevance_reason",
            "relevance_source",
            "relevance_check_status",
            "broken_rate",
            "broken_size_rate",
            "price",
        ]:
            if key in product:
                item[key] = product.get(key)
        summarized.append(item)
    return summarized
