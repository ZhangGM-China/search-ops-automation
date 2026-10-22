import json
import os
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional

from src.llm.node_config import ResolvedNodeConfig, model_audit_fields
from src.llm.rd_client import FallbackChatModel

from .constants import (
    DEFAULT_TOP_QUERY_CHECK_MODES,
    DEFAULT_TOP_QUERY_TOOL_MODES,
    PLANNER_EXECUTOR_KEYS,
    PLANNER_TOOL_KEYS,
)
from .schemas import ActionCandidate, GovernanceStage, GovernanceStrategy, PlannerCheck, PlannerOutput
from .policy_compliance import (
    POLICY_FACT_VERSION,
    AgentPolicyComplianceError,
    build_policy_facts,
    validate_planner_output,
)
from .recall_scoring import build_recall_assessment
from .llm_timeout import invoke_legacy_with_timeout


KG_ACTION_TYPES = {"kg_tag", "kg_relation"}


class PlannerStrategyError(ValueError):
    pass


def ensure_world_knowledge_assessment(plan: PlannerOutput) -> PlannerOutput:
    existing_checks = list(plan.checks or [])
    open_factor_check = next(
        (check for check in existing_checks if check.check_item == "open_factor"),
        PlannerCheck("open_factor", "综合评估季节、天气、节日和社媒趋势是否需要调整标签权重"),
    )
    checks = [open_factor_check, *[check for check in existing_checks if check.check_item != "open_factor"]]

    strategy = plan.strategy or GovernanceStrategy(objective="提升 Query 搜索转化")
    existing_stages = list(strategy.stages or [])
    open_factor_stage = None
    split_stage_key = None
    remaining_stages = []
    for stage in existing_stages:
        if "open_factor" not in stage.executor_keys:
            remaining_stages.append(stage)
            continue
        if open_factor_stage is None:
            open_factor_stage = stage
        remaining_executor_keys = [key for key in stage.executor_keys if key != "open_factor"]
        if remaining_executor_keys:
            if stage is open_factor_stage:
                split_stage_key = stage.stage_key
            remaining_stages.append(replace(
                stage,
                executor_keys=remaining_executor_keys,
                retry_check_items=[
                    key for key in stage.retry_check_items if key in remaining_executor_keys
                ],
                allowed_action_types=[
                    action_type for action_type in stage.allowed_action_types
                    if action_type != "tag_bucket_config"
                ],
            ))
    if open_factor_stage is None:
        open_factor_stage = GovernanceStage(
            stage_key="world_knowledge_assessment",
            executor_keys=["open_factor"],
            reason="世界知识标签权重可能改变 Top N，必须在其他治理前完成评估",
            allowed_action_types=["tag_bucket_config"],
            verification_required=True,
        )
        if not existing_stages:
            other_check_items = [check.check_item for check in checks if check.check_item != "open_factor"]
            if other_check_items:
                remaining_stages.append(GovernanceStage(
                    stage_key="planner_assessment",
                    executor_keys=other_check_items,
                    reason="执行 Planner 已识别的其他检查项",
                ))
    else:
        stage_key = open_factor_stage.stage_key
        if split_stage_key is not None:
            used_stage_keys = {stage.stage_key for stage in remaining_stages}
            stage_key = "world_knowledge_assessment"
            suffix = 2
            while stage_key in used_stage_keys:
                stage_key = f"world_knowledge_assessment_{suffix}"
                suffix += 1
            remaining_stages = [
                replace(
                    stage,
                    depends_on=[*stage.depends_on, stage_key]
                    if stage.stage_key == split_stage_key and stage_key not in stage.depends_on
                    else list(stage.depends_on),
                )
                for stage in remaining_stages
            ]
        open_factor_stage = replace(
            open_factor_stage,
            stage_key=stage_key,
            executor_keys=["open_factor"],
            retry_check_items=[
                key for key in open_factor_stage.retry_check_items if key == "open_factor"
            ],
            allowed_action_types=["tag_bucket_config"],
            depends_on=[],
        )

    stages = [
        replace(stage, priority=index)
        for index, stage in enumerate([open_factor_stage, *remaining_stages], start=1)
    ]
    ensured_strategy = replace(strategy, stages=stages)
    return replace(plan, checks=checks, strategy=ensured_strategy)


def strip_world_knowledge_assessment(plan: PlannerOutput) -> PlannerOutput:
    checks = [check for check in (plan.checks or []) if check.check_item != "open_factor"]
    actions = [action for action in (plan.actions or []) if action.check_item != "open_factor"]
    strategy = plan.strategy
    if strategy is None:
        return replace(plan, checks=checks, actions=actions)

    remaining_stages = []
    removed_stage_keys = set()
    for stage in strategy.stages or []:
        if "open_factor" not in stage.executor_keys:
            remaining_stages.append(stage)
            continue
        executor_keys = [key for key in stage.executor_keys if key != "open_factor"]
        if not executor_keys:
            removed_stage_keys.add(stage.stage_key)
            continue
        remaining_stages.append(replace(
            stage,
            executor_keys=executor_keys,
            retry_check_items=[
                key for key in stage.retry_check_items if key in executor_keys
            ],
            allowed_action_types=[
                action_type for action_type in stage.allowed_action_types
                if action_type != "tag_bucket_config"
            ],
        ))

    stages = [
        replace(
            stage,
            priority=index,
            depends_on=[key for key in stage.depends_on if key not in removed_stage_keys],
        )
        for index, stage in enumerate(remaining_stages, start=1)
    ]
    return replace(plan, checks=checks, actions=actions, strategy=replace(strategy, stages=stages))


class DeterministicFallbackPlanner:
    def __init__(
        self,
        low_result_threshold: int = 4,
        drop_ratio_threshold: float = 0.4,
        broken_rate_threshold: float = 0.6,
        tag_bucket_api_configured: bool = False,
    ):
        self.low_result_threshold = low_result_threshold
        self.drop_ratio_threshold = drop_ratio_threshold
        self.broken_rate_threshold = broken_rate_threshold
        self.tag_bucket_api_configured = tag_bucket_api_configured

    def plan(self, context: Dict[str, Any]) -> PlannerOutput:
        context = _normalize_context(context)
        checks: List[PlannerCheck] = []
        actions: List[ActionCandidate] = []
        tag_bucket_api_ready = (self.tag_bucket_api_configured or bool(context.get("tag_bucket_api_configured"))) and context.get("tag_bucket_api_supported", True) is not False

        if not context.get("suppress_recall_check") and self._has_result_health_problem(context):
            checks.append(PlannerCheck("recall", "搜索结果为空、偏少或较历史明显下降"))

        if self._has_relevance_problem(context):
            checks.append(PlannerCheck("relevance", "Top 结果存在明显不相关商品"))

        if self._max_product_value(context, "broken_rate") >= self.broken_rate_threshold:
            checks.append(PlannerCheck("inventory", "Top 商品存在高断码风险"))

        open_factor_context = context.get("open_factor_context") or {}
        if self._has_open_factor_signal(open_factor_context):
            checks.append(PlannerCheck("open_factor", "季节、天气、节日或趋势因素可能影响排序"))
            if tag_bucket_api_ready:
                actions.append(
                    ActionCandidate(
                        check_item="open_factor",
                        action_type="tag_bucket_config",
                        reason="tag bucket API 已配置，可生成分桶配置候选",
                        execution_mode="auto",
                        payload={"query": context.get("query"), "open_factor_context": open_factor_context},
                        confidence=0.6,
                    )
                )
            else:
                actions.append(
                    ActionCandidate(
                        check_item="open_factor",
                        action_type="suggestion",
                        reason="tag bucket API 未配置，仅生成 dry-run 建议",
                        execution_mode="dry_run",
                        payload={"query": context.get("query"), "open_factor_context": open_factor_context},
                        confidence=0.5,
                    )
                )

        return PlannerOutput(checks=checks, actions=actions, strategy=self._build_strategy(checks), llm_used=False)

    def _build_strategy(self, checks: List[PlannerCheck]) -> GovernanceStrategy:
        check_items = [check.check_item for check in checks]
        stages: List[GovernanceStage] = []

        if "recall" in check_items:
            stages.append(
                GovernanceStage(
                    stage_key="recall_repair",
                    executor_keys=["recall"],
                    reason="无结果、少结果或结果数量骤降会直接限制有效曝光，优先诊断召回覆盖",
                    priority=len(stages) + 1,
                    allowed_action_types=["kg_tag", "kg_relation"],
                )
            )

        if "open_factor" in check_items:
            stages.append(
                GovernanceStage(
                    stage_key="world_knowledge_adjustment",
                    executor_keys=["open_factor"],
                    reason="世界知识只影响当前 Query 的标签权重调整，风险较低且可能改变后续 Top 结果集合",
                    priority=len(stages) + 1,
                    allowed_action_types=["tag_bucket_config"],
                )
            )

        if "relevance" in check_items:
            stages.append(
                GovernanceStage(
                    stage_key="relevance_quality",
                    executor_keys=["relevance"],
                    reason="在召回和世界知识调整后处理仍存在的意图不符商品或知识图谱根因",
                    priority=len(stages) + 1,
                    allowed_action_types=["product_blacklist", "kg_relation"],
                )
            )

        commercial_executors = [item for item in ["inventory"] if item in check_items]
        if commercial_executors:
            allowed_action_types = ["query_demote"]
            stages.append(
                GovernanceStage(
                    stage_key="commercial_quality",
                    executor_keys=commercial_executors,
                    reason="断码只在影响转化时处理，避免机械降桶",
                    priority=len(stages) + 1,
                    allowed_action_types=allowed_action_types,
                )
            )

        return GovernanceStrategy(
            objective="提升 Query 搜索转化",
            root_causes=check_items,
            stages=stages,
            success_criteria=["治理后召回数量、相关性和断码风险满足当前配置阈值"],
            replan_triggers=["治理后仍存在非召回问题", "低风险动作执行后 Top 结果集合发生变化"],
        )

    def _has_result_health_problem(self, context: Dict[str, Any]) -> bool:
        raw_result_count = context.get("result_count")
        if (
            context.get("result_count_known") is False
            or not isinstance(raw_result_count, int)
            or isinstance(raw_result_count, bool)
            or raw_result_count < 0
        ):
            return False
        assessment = build_recall_assessment(
            result_count=raw_result_count,
            low_result_threshold=self.low_result_threshold,
            drop_ratio_threshold=self.drop_ratio_threshold,
            seven_day_average=context.get("seven_day_average"),
            history_sample_count=int(context.get("history_sample_count") or 0),
        )
        return bool(assessment["reasons"])

    @staticmethod
    def _has_relevance_problem(context: Dict[str, Any]) -> bool:
        diagnostics = context.get("relevance_diagnostics") or {}
        if isinstance(diagnostics, dict):
            mismatch_count = int(diagnostics.get("mismatch_count") or 0)
            checked_count = int(diagnostics.get("checked_count") or len(diagnostics.get("items") or []) or 0)
            check_failed_count = int(diagnostics.get("check_failed_count") or 0)
            if mismatch_count > 0:
                return True
            if checked_count > 0 and check_failed_count / checked_count >= 0.5:
                return True
        if context.get("kg_candidate_gap") or context.get("kg_issue"):
            return True
        for product in context.get("products") or []:
            if product.get("is_relevant") is False:
                return True
            score = product.get("relevance_score")
            if score is not None and float(score) < 0.4:
                return True
        return False

    @staticmethod
    def _has_open_factor_signal(open_factor_context: Dict[str, Any]) -> bool:
        if not open_factor_context:
            return False
        if open_factor_context.get("matched_factors"):
            return True
        for key, value in open_factor_context.items():
            if key == "season":
                continue
            if isinstance(value, dict) and value.get("status") == "unavailable":
                continue
            if value:
                return True
        return False

    @staticmethod
    def _max_product_value(context: Dict[str, Any], key: str) -> float:
        values = [float(product.get(key) or 0) for product in context.get("products") or []]
        return max(values, default=0.0)


class PlannerAgent:
    def __init__(
        self,
        llm: Optional[Any] = None,
        fallback_planner: Optional[DeterministicFallbackPlanner] = None,
        check_modes: Optional[Dict[str, str]] = None,
        tool_modes: Optional[Dict[str, str]] = None,
        prefer_deterministic_for_simple_cases: bool = False,
        policy_compliance_enabled: bool = False,
        model_config: Optional[ResolvedNodeConfig] = None,
    ):
        self.llm = llm
        self.fallback_planner = fallback_planner or DeterministicFallbackPlanner()
        self.prefer_deterministic_for_simple_cases = prefer_deterministic_for_simple_cases
        self.strategy_planner = LLMStrategyPlanner(
            llm=llm,
            check_modes=check_modes,
            tool_modes=tool_modes,
            policy_compliance_enabled=policy_compliance_enabled,
            model_config=model_config,
        ) if llm else None

    def plan(self, context: Dict[str, Any]) -> PlannerOutput:
        context = _normalize_context(context)
        deterministic_plan = self.fallback_planner.plan(context)
        if self.prefer_deterministic_for_simple_cases and _should_skip_llm_planning(deterministic_plan):
            return deterministic_plan
        if not self.llm:
            return deterministic_plan

        try:
            return self.strategy_planner.plan(context)
        except AgentPolicyComplianceError:
            raise
        except Exception as exc:
            plan = deterministic_plan
            plan.fallback_reason = str(exc)
            plan.error_type = type(exc).__name__
            plan.llm_metrics = getattr(exc, "planner_llm_metrics", {}) or {}
            if plan.llm_metrics:
                plan.timeout_seconds = plan.llm_metrics.get("timeoutSeconds")
                plan.request_chars = plan.llm_metrics.get("requestChars")
                plan.response_chars = plan.llm_metrics.get("responseChars")
                plan.token_usage = int(plan.llm_metrics.get("tokenUsage") or 0)
            return plan


class LLMStrategyPlanner:
    def __init__(
        self,
        llm: Any,
        check_modes: Optional[Dict[str, str]] = None,
        tool_modes: Optional[Dict[str, str]] = None,
        policy_compliance_enabled: bool = False,
        model_config: Optional[ResolvedNodeConfig] = None,
    ):
        self.llm = llm
        self.fact_pack_builder = PlannerFactPackBuilder(check_modes=check_modes, tool_modes=tool_modes)
        self.policy_compliance_enabled = bool(policy_compliance_enabled)
        self.model_config = model_config

    def plan(self, context: Dict[str, Any]) -> PlannerOutput:
        started_at = time.monotonic()
        deadline = started_at + _planner_total_timeout_seconds()
        fact_pack = self.fact_pack_builder.build(context)
        fact_pack_chars = len(json.dumps(fact_pack, ensure_ascii=False))
        primary_invoke = getattr(self.llm, "invoke_primary", None) or self.llm.invoke
        fallback_invoke = getattr(self.llm, "invoke_fallback", None)
        primary_metrics: List[Dict[str, Any]] = []

        try:
            return self._plan_with_invoke(
                primary_invoke,
                fact_pack,
                fact_pack_chars,
                model_role="primary",
                metrics_sink=primary_metrics,
                deadline=deadline,
                started_at=started_at,
            )
        except AgentPolicyComplianceError:
            raise
        except Exception as primary_exc:
            if fallback_invoke is None:
                raise
            _merge_attempt_entries(primary_metrics, primary_exc)
            if _remaining_seconds(deadline) <= 0:
                setattr(
                    primary_exc,
                    "planner_llm_metrics",
                    _planner_llm_metrics(
                        primary_metrics,
                        final_error_type=type(primary_exc).__name__,
                        started_at=started_at,
                        budget_exhausted=True,
                    ),
                )
                raise
            partial_phase_one_result = getattr(primary_exc, "planner_phase_one_result", None)
            return self._plan_with_invoke(
                fallback_invoke,
                fact_pack,
                fact_pack_chars,
                model_role="fallback",
                metrics_sink=primary_metrics,
                phase_one_result=partial_phase_one_result,
                deadline=deadline,
                started_at=started_at,
            )

    def _plan_with_invoke(
        self,
        invoke_fn: Any,
        fact_pack: Dict[str, Any],
        fact_pack_chars: int,
        model_role: str,
        metrics_sink: List[Dict[str, Any]],
        phase_one_result: Optional[Dict[str, Any]] = None,
        deadline: Optional[float] = None,
        started_at: Optional[float] = None,
        compliance_feedback: Optional[List[Dict[str, Any]]] = None,
        compliance_attempt: int = 1,
    ) -> PlannerOutput:
        try:
            phase_one_response = None
            phase_one_system_prompt = ""
            phase_one_request_text = ""
            if phase_one_result is None:
                phase_one_system_prompt = _planner_phase_one_system_prompt()
                phase_one_payload = {"task": "top_query_strategy_diagnosis", "fact_pack": fact_pack}
                if compliance_feedback:
                    phase_one_payload["policy_violation"] = compliance_feedback
                phase_one_request_text = json.dumps(phase_one_payload, ensure_ascii=False)
                phase_one_messages = [
                    {"role": "system", "content": phase_one_system_prompt},
                    {"role": "user", "content": phase_one_request_text},
                ]
                phase_one_response = self._invoke_phase_one_with_metrics(
                    phase="phase1",
                    model_role=model_role,
                    messages=phase_one_messages,
                    timeout_seconds=_planner_phase_one_timeout_seconds(),
                    metrics_sink=metrics_sink,
                    deadline=deadline,
                )
                phase_one_result = self._phase_one_result_from_response(phase_one_response)

            phase_two_system_prompt = _planner_phase_two_system_prompt()
            phase_two_payload = {
                "task": "top_query_strategy_staging",
                "fact_pack": fact_pack,
                "diagnosis": phase_one_result,
            }
            if compliance_feedback:
                phase_two_payload["policy_violation"] = compliance_feedback
            phase_two_request_text = json.dumps(phase_two_payload, ensure_ascii=False)
            phase_two_messages = [
                {"role": "system", "content": phase_two_system_prompt},
                {"role": "user", "content": phase_two_request_text},
            ]
            phase_two_response = self._invoke_phase_two_with_metrics(
                phase="phase2",
                model_role=model_role,
                messages=phase_two_messages,
                timeout_seconds=_planner_phase_two_timeout_seconds(),
                metrics_sink=metrics_sink,
                deadline=deadline,
            )
            combined_payload = self._combined_payload_from_phase_two_response(
                phase_two_response,
                phase_one_result,
            )
            llm_metrics = _planner_llm_metrics(metrics_sink, started_at=started_at)
            llm_metrics.update({
                "complianceAttempts": compliance_attempt,
                "complianceCorrected": compliance_attempt > 1,
                "policyFactVersion": POLICY_FACT_VERSION,
                "policyViolationCodes": [
                    str(item.get("code") or "")
                    for item in (compliance_feedback or [])
                    if str(item.get("code") or "")
                ],
            })
            output = self._build_output_from_payload(
                combined_payload,
                fact_pack_chars,
                request_chars=(
                    len(phase_one_system_prompt)
                    + len(phase_one_request_text)
                    + len(phase_two_system_prompt)
                    + len(phase_two_request_text)
                ),
                response_chars=(
                    len(str(getattr(phase_one_response, "content", phase_one_response) or ""))
                    + len(str(getattr(phase_two_response, "content", phase_two_response) or ""))
                ),
                token_usage=_token_usage_from_response(phase_one_response) + _token_usage_from_response(phase_two_response),
                llm_metrics=llm_metrics,
            )
            if not self.policy_compliance_enabled:
                return output
            violations = validate_planner_output(output, fact_pack.get("policyFacts") or {})
            if not violations:
                return output
            serialized_violations = [item.to_dict() for item in violations]
            if compliance_attempt >= 2:
                failure_metrics = _planner_llm_metrics(
                    metrics_sink,
                    final_error_type="AgentPolicyComplianceError",
                    started_at=started_at,
                    budget_exhausted=bool(deadline is not None and _remaining_seconds(deadline) <= 0),
                )
                failure_metrics.update({
                    "complianceAttempts": compliance_attempt,
                    "complianceCorrected": False,
                    "policyFactVersion": POLICY_FACT_VERSION,
                    "policyViolationCodes": [item.code for item in violations],
                })
                raise AgentPolicyComplianceError(
                    phase="planner",
                    violations=violations,
                    attempts=compliance_attempt,
                    token_usage=int(failure_metrics.get("tokenUsage") or 0),
                    llm_metrics=failure_metrics,
                )
            return self._plan_with_invoke(
                invoke_fn,
                fact_pack,
                fact_pack_chars,
                model_role,
                metrics_sink,
                phase_one_result=_correct_phase_one_policy_violations(phase_one_result, violations),
                deadline=deadline,
                started_at=started_at,
                compliance_feedback=serialized_violations,
                compliance_attempt=compliance_attempt + 1,
            )
        except Exception as exc:
            _mark_latest_planner_post_processing_error(metrics_sink, exc)
            if phase_one_result is not None:
                setattr(exc, "planner_phase_one_result", phase_one_result)
            setattr(exc, "planner_llm_metrics", _planner_llm_metrics(
                metrics_sink,
                final_error_type=type(exc).__name__,
                started_at=started_at,
                budget_exhausted=bool(deadline is not None and _remaining_seconds(deadline) <= 0),
            ))
            raise

    def _invoke_phase_one_with_metrics(
        self,
        phase: str,
        model_role: str,
        messages: List[Dict[str, str]],
        timeout_seconds: float,
        metrics_sink: List[Dict[str, Any]],
        deadline: Optional[float],
    ) -> Any:
        structured_model = _structured_model_for_role(self.llm, model_role)
        if structured_model is not None:
            try:
                return self._invoke_with_metrics(
                    structured_model.invoke,
                    phase=phase,
                    model_role=model_role,
                    model_name=_model_name_for_role(self.llm, model_role),
                    messages=messages,
                    timeout_seconds=timeout_seconds,
                    metrics_sink=metrics_sink,
                    structured_output=True,
                    deadline=deadline,
                )
            except Exception as exc:
                if not _is_structured_output_unsupported_error(exc):
                    raise
        invoke_fn = getattr(self.llm, "invoke_fallback", None) if model_role == "fallback" else getattr(self.llm, "invoke_primary", None)
        invoke_fn = invoke_fn or self.llm.invoke
        return self._invoke_with_metrics(
            invoke_fn,
            phase=phase,
            model_role=model_role,
            model_name=_model_name_for_role(self.llm, model_role),
            messages=messages,
            timeout_seconds=timeout_seconds,
            metrics_sink=metrics_sink,
            structured_output=False,
            deadline=deadline,
        )

    def _invoke_phase_two_with_metrics(
        self,
        phase: str,
        model_role: str,
        messages: List[Dict[str, str]],
        timeout_seconds: float,
        metrics_sink: List[Dict[str, Any]],
        deadline: Optional[float],
    ) -> Any:
        structured_model = _structured_model_for_role(self.llm, model_role, phase=phase)
        if structured_model is not None:
            try:
                return self._invoke_with_metrics(
                    structured_model.invoke,
                    phase=phase,
                    model_role=model_role,
                    model_name=_model_name_for_role(self.llm, model_role),
                    messages=messages,
                    timeout_seconds=timeout_seconds,
                    metrics_sink=metrics_sink,
                    structured_output=True,
                    deadline=deadline,
                )
            except Exception as exc:
                if not _is_structured_output_unsupported_error(exc):
                    raise
        invoke_fn = getattr(self.llm, "invoke_fallback", None) if model_role == "fallback" else getattr(self.llm, "invoke_primary", None)
        invoke_fn = invoke_fn or self.llm.invoke
        return self._invoke_with_metrics(
            invoke_fn,
            phase=phase,
            model_role=model_role,
            model_name=_model_name_for_role(self.llm, model_role),
            messages=messages,
            timeout_seconds=timeout_seconds,
            metrics_sink=metrics_sink,
            structured_output=False,
            deadline=deadline,
        )

    def _invoke_with_metrics(
        self,
        invoke_fn: Any,
        phase: str,
        model_role: str,
        model_name: Optional[str],
        messages: List[Dict[str, str]],
        timeout_seconds: float,
        metrics_sink: List[Dict[str, Any]],
        structured_output: bool,
        deadline: Optional[float],
    ) -> Any:
        attempt_timeout_cap = _planner_attempt_timeout_seconds(model_role)
        effective_timeout = min(timeout_seconds, attempt_timeout_cap)
        if deadline is not None:
            effective_timeout = min(effective_timeout, _remaining_seconds(deadline))
        if effective_timeout <= 0:
            raise TimeoutError("Planner LLM total time budget exhausted")
        request_text = "".join(str(message.get("content") or "") for message in messages)
        model_identity = (
            model_audit_fields(self.model_config, model_role)
            if self.model_config is not None
            else {"modelRole": model_role, "modelName": model_name}
        )
        started_at = time.monotonic()
        try:
            response = _invoke_with_timeout(
                invoke_fn,
                messages,
                effective_timeout,
                require_plain_json=not structured_output,
            )
            response_text = str(getattr(response, "content", response) or "")
            metrics_sink.append({
                "phase": phase,
                **model_identity,
                "structuredOutput": structured_output,
                "configuredTimeoutSeconds": timeout_seconds,
                "attemptTimeoutCapSeconds": attempt_timeout_cap,
                "timeoutSeconds": effective_timeout,
                "durationSeconds": round(time.monotonic() - started_at, 4),
                "requestChars": len(request_text),
                "responseChars": len(response_text),
                "tokenUsage": _token_usage_from_response(response),
                "errorType": None,
            })
            return response
        except Exception as exc:
            metrics_sink.append({
                "phase": phase,
                **model_identity,
                "structuredOutput": structured_output,
                "configuredTimeoutSeconds": timeout_seconds,
                "attemptTimeoutCapSeconds": attempt_timeout_cap,
                "timeoutSeconds": effective_timeout,
                "durationSeconds": round(time.monotonic() - started_at, 4),
                "requestChars": len(request_text),
                "responseChars": 0,
                "tokenUsage": 0,
                "errorType": type(exc).__name__,
            })
            raise

    def _phase_one_result_from_response(self, response: Any) -> Dict[str, Any]:
        payload = _loads_json_payload(response)
        if not isinstance(payload, dict):
            raise PlannerStrategyError("Planner diagnosis payload must be an object")
        root_causes = [_coerce_check_item(item) for item in payload.get("root_causes", payload.get("rootCauses", []))]
        root_causes = [item for item in root_causes if item]
        if not root_causes:
            raise PlannerStrategyError("Planner diagnosis missing root causes")
        for check_item in root_causes:
            _validate_check_item(check_item)
        return {
            "objective": payload.get("objective") or "提升 Query 搜索转化",
            "strategy_reason": payload.get("strategy_reason", payload.get("strategyReason", "")),
            "conversion_hypothesis": payload.get("conversion_hypothesis", payload.get("conversionHypothesis", "")),
            "risk_level": payload.get("risk_level", payload.get("riskLevel", "")),
            "root_causes": root_causes,
            "evidence_refs": [str(item) for item in payload.get("evidence_refs", payload.get("evidenceRefs", []))],
            "policy_acknowledgements": payload.get("policy_acknowledgements", payload.get("policyAcknowledgements", {})) or {},
        }

    def _combined_payload_from_phase_two_response(self, response: Any, phase_one_result: Dict[str, Any]) -> Dict[str, Any]:
        payload = _loads_json_payload(response)
        if not isinstance(payload, dict):
            raise PlannerStrategyError("Planner staging payload must be an object")
        strategy_patch = payload.get("strategy") or payload.get("governanceStrategy") or payload
        if not isinstance(strategy_patch, dict):
            raise PlannerStrategyError("Planner staging strategy missing")
        strategy = {
            "objective": phase_one_result.get("objective") or "提升 Query 搜索转化",
            "strategy_reason": phase_one_result.get("strategy_reason") or "",
            "conversion_hypothesis": phase_one_result.get("conversion_hypothesis") or "",
            "risk_level": phase_one_result.get("risk_level") or "",
            "root_causes": phase_one_result.get("root_causes") or [],
            "evidence_refs": phase_one_result.get("evidence_refs") or [],
            "stages": strategy_patch.get("stages") or [],
            "success_criteria": strategy_patch.get("success_criteria", strategy_patch.get("successCriteria", [])),
            "replan_triggers": strategy_patch.get("replan_triggers", strategy_patch.get("replanTriggers", [])),
        }
        return {
            "checks": payload.get("checks") or [],
            "actions": payload.get("actions") or [],
            "strategy": strategy,
            "policy_acknowledgements": phase_one_result.get("policy_acknowledgements") or {},
        }

    def _build_output_from_payload(
        self,
        payload: Dict[str, Any],
        fact_pack_chars: int,
        request_chars: int,
        response_chars: int,
        token_usage: int,
        llm_metrics: Dict[str, Any],
    ) -> PlannerOutput:
        checks = self._checks_from_payload(payload)
        actions = self._actions_from_payload(payload)
        strategy = self._strategy_from_payload(payload)
        self._validate_strategy(strategy)
        if not checks:
            checks = _checks_from_strategy(strategy)
        aggregate_request_chars = int(llm_metrics.get("requestChars") or request_chars or 0)
        aggregate_response_chars = int(llm_metrics.get("responseChars") or response_chars or 0)
        aggregate_token_usage = int(llm_metrics.get("tokenUsage") or token_usage or 0)
        return PlannerOutput(
            checks=checks,
            actions=actions,
            strategy=strategy,
            llm_used=True,
            token_usage=aggregate_token_usage,
            timeout_seconds=_planner_total_timeout_seconds(),
            fact_pack_chars=fact_pack_chars,
            request_chars=aggregate_request_chars,
            response_chars=aggregate_response_chars,
            llm_metrics=llm_metrics,
            policy_acknowledgements=payload.get("policy_acknowledgements", payload.get("policyAcknowledgements", {})) or {},
        )
        

    def _checks_from_payload(self, payload: Dict[str, Any]) -> List[PlannerCheck]:
        checks = []
        for item in payload.get("checks") or []:
            if not isinstance(item, dict):
                continue
            check_item = _coerce_check_item(item.get("check_item") or item.get("checkItem") or "")
            if not check_item:
                continue
            checks.append(
                PlannerCheck(
                    check_item=check_item,
                    reason=item.get("reason", ""),
                    severity=item.get("severity", "warning"),
                )
            )
        return checks

    def _actions_from_payload(self, payload: Dict[str, Any]) -> List[ActionCandidate]:
        actions = []
        for item in payload.get("actions") or []:
            if not isinstance(item, dict):
                continue
            check_item = _coerce_check_item(item.get("check_item") or item.get("checkItem") or "")
            if not check_item:
                continue
            action_type = str(item.get("action_type") or item.get("actionType") or "")
            if not action_type:
                continue
            _validate_check_item(check_item)
            _validate_tool_key(action_type)
            if check_item == "open_factor" and action_type in KG_ACTION_TYPES:
                raise PlannerStrategyError("world knowledge strategy cannot use KG actions")
            actions.append(
                ActionCandidate(
                    check_item=check_item,
                    action_type=action_type,
                    reason=item.get("reason", ""),
                    execution_mode=item.get("execution_mode", item.get("executionMode", "human_approval")),
                    payload=item.get("payload") or {},
                    priority=int(item.get("priority") or 0),
                    expected_impact=item.get("expected_impact", item.get("expectedImpact", "")),
                    negative_impact=item.get("negative_impact", item.get("negativeImpact", "")),
                    confidence=float(item.get("confidence") or 0),
                )
            )
        return actions

    def _strategy_from_payload(self, payload: Dict[str, Any]) -> GovernanceStrategy:
        raw_strategy = payload.get("strategy") or payload.get("governanceStrategy")
        if not isinstance(raw_strategy, dict):
            raise PlannerStrategyError("LLM strategy missing")
        normalized_root_causes = [
            _coerce_check_item(check_item)
            for check_item in raw_strategy.get("root_causes", raw_strategy.get("rootCauses", []))
        ]
        normalized_root_causes = [check_item for check_item in normalized_root_causes if check_item]

        stages = []
        for index, item in enumerate(raw_strategy.get("stages") or [], start=1):
            if not isinstance(item, dict):
                continue
            executor_keys = item.get("executor_keys") or item.get("executorKeys") or []
            if not isinstance(executor_keys, list) or not executor_keys:
                continue
            normalized_executor_keys = [_coerce_check_item(key) for key in executor_keys]
            if any(not key for key in normalized_executor_keys):
                invalid_executor = executor_keys[normalized_executor_keys.index("")]
                raise PlannerStrategyError(f"Unknown executor: {invalid_executor}")
            normalized_executor_keys = [key for key in normalized_executor_keys if key]
            if not normalized_executor_keys:
                continue
            for executor_key in normalized_executor_keys:
                _validate_executor_key(executor_key)
            allowed_action_types = [
                str(action_type)
                for action_type in item.get("allowed_action_types", item.get("allowedActionTypes", []))
            ]
            for action_type in allowed_action_types:
                _validate_tool_key(action_type)
            if "open_factor" in normalized_executor_keys and any(action_type in KG_ACTION_TYPES for action_type in allowed_action_types):
                raise PlannerStrategyError("world knowledge strategy cannot use KG actions")
            raw_retry_check_items = item.get("retry_check_items", item.get("retryCheckItems", []))
            if not isinstance(raw_retry_check_items, list):
                raise PlannerStrategyError("retry_check_items must be a list")
            retry_check_items = [str(value or "").strip() for value in raw_retry_check_items]
            if any(check_item not in PLANNER_EXECUTOR_KEYS for check_item in retry_check_items):
                raise PlannerStrategyError("retry_check_items contains unknown check item")
            if any(check_item not in normalized_executor_keys for check_item in retry_check_items):
                raise PlannerStrategyError("retry_check_items must be a subset of executor_keys")
            stages.append(
                GovernanceStage(
                    stage_key=item.get("stage_key") or item.get("stageKey") or f"stage_{index}",
                    executor_keys=normalized_executor_keys,
                    reason=item.get("reason", ""),
                    priority=_safe_stage_priority(item.get("priority"), index),
                    verification_required=bool(item.get("verification_required", item.get("verificationRequired", True))),
                    allowed_action_types=allowed_action_types,
                    stage_goal=item.get("stage_goal", item.get("stageGoal", "")),
                    entry_condition=item.get("entry_condition", item.get("entryCondition", "")),
                    exit_condition=item.get("exit_condition", item.get("exitCondition", "")),
                    risk_level=item.get("risk_level", item.get("riskLevel", "")),
                    depends_on=[
                        str(stage_key)
                        for stage_key in item.get("depends_on", item.get("dependsOn", []))
                    ],
                    retry_check_items=retry_check_items,
                )
            )
        for check_item in normalized_root_causes:
            _validate_check_item(check_item)
        if not stages and normalized_root_causes:
            stages = _default_stages_for_root_causes(normalized_root_causes)
        if not stages and (payload.get("checks") or raw_strategy.get("root_causes") or raw_strategy.get("rootCauses")):
            raise PlannerStrategyError("LLM strategy has no executable stages")

        return GovernanceStrategy(
            objective=raw_strategy.get("objective") or "提升 Query 搜索转化",
            root_causes=normalized_root_causes,
            stages=stages,
            success_criteria=[
                str(item)
                for item in raw_strategy.get("success_criteria", raw_strategy.get("successCriteria", []))
            ],
            replan_triggers=[
                str(item)
                for item in raw_strategy.get("replan_triggers", raw_strategy.get("replanTriggers", []))
            ],
            strategy_reason=raw_strategy.get("strategy_reason", raw_strategy.get("strategyReason", "")),
            conversion_hypothesis=raw_strategy.get("conversion_hypothesis", raw_strategy.get("conversionHypothesis", "")),
            evidence_refs=[str(item) for item in raw_strategy.get("evidence_refs", raw_strategy.get("evidenceRefs", []))],
            risk_level=raw_strategy.get("risk_level", raw_strategy.get("riskLevel", "")),
        )

    def _validate_strategy(self, strategy: GovernanceStrategy) -> None:
        for stage in strategy.stages:
            for executor_key in stage.executor_keys:
                _validate_executor_key(executor_key)
            for action_type in stage.allowed_action_types:
                _validate_tool_key(action_type)
            if "open_factor" in stage.executor_keys and any(action_type in KG_ACTION_TYPES for action_type in stage.allowed_action_types):
                raise PlannerStrategyError("world knowledge strategy cannot use KG actions")


class PlannerFactPackBuilder:
    def __init__(
        self,
        check_modes: Optional[Dict[str, str]] = None,
        tool_modes: Optional[Dict[str, str]] = None,
        max_products: int = 16,
    ):
        self.check_modes = {**DEFAULT_TOP_QUERY_CHECK_MODES, **(check_modes or {})}
        self.tool_modes = {**DEFAULT_TOP_QUERY_TOOL_MODES, **(tool_modes or {})}
        self.max_products = max_products

    def build(self, context: Dict[str, Any]) -> Dict[str, Any]:
        context = _normalize_context(context)
        task = context.get("task") or {}
        search = context.get("search") or {}
        products = context.get("products") or []
        inspected_products = products[: self.max_products]
        return {
            "query": {
                "brand": context.get("brand"),
                "text": context.get("query"),
                "rank": task.get("query_rank") or context.get("query_rank"),
            },
            "metrics": context.get("metrics") or {},
            "search": {
                "success": search.get("success"),
                "result_count": context.get("result_count"),
                "result_count_known": context.get(
                    "result_count_known",
                    search.get("result_count_known"),
                ),
                "result_complete": search.get("result_complete"),
                "failure_reason": search.get("failure_reason") or search.get("error_reason"),
                "previous_result_count": context.get("previous_result_count"),
                "suppress_recall_check": bool(context.get("suppress_recall_check")),
            },
            "product_summary": _summarize_product_cohort_for_planner(inspected_products),
            "products": _select_product_examples_for_planner(inspected_products),
            "relevance_diagnostics": _summarize_relevance_diagnostics(context.get("relevance_diagnostics")),
            "world_knowledge": {
                "open_factor_context": _summarize_open_factor_context(context.get("open_factor_context") or {}),
                "tag_bucket_config": context.get("tag_bucket_config") or context.get("tagBucketConfig"),
            },
            "history": _summarize_history(context.get("history") or {}),
            "governance_history": _summarize_governance_history(
                context.get("governance_history") or {}
            ),
            "policy": {
                "check_modes": {
                    key: self.check_modes[key]
                    for key in PLANNER_EXECUTOR_KEYS
                },
                "tool_modes": {
                    key: self.tool_modes[key]
                    for key in PLANNER_TOOL_KEYS
                },
                "kg_actions_require_human_approval": True,
            },
            "policyFacts": build_policy_facts(context),
            "available": {
                "executors": list(PLANNER_EXECUTOR_KEYS),
                "tools": list(PLANNER_TOOL_KEYS),
            },
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
    if "metrics" not in normalized:
        normalized["metrics"] = {
            "ctr": task.get("ctr"),
            "cvr": task.get("cvr"),
            "avg_ctr": task.get("avg_ctr"),
            "avg_cvr": task.get("avg_cvr"),
        }
    normalized_products = []
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
        normalized_products.append(item)
    normalized["products"] = normalized_products
    if "open_factor_context" not in normalized and normalized.get("open_factors"):
        normalized["open_factor_context"] = normalized.get("open_factors")
    return normalized


def _summarize_governance_history(history: Any) -> Dict[str, Any]:
    if not isinstance(history, dict):
        return {}
    executor_results = []
    for result in (history.get("executor_results") or [])[:10]:
        if not isinstance(result, dict):
            continue
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        executor_results.append({
            "check_item": result.get("check_item"),
            "status": result.get("status"),
            "root_cause": result.get("root_cause"),
            "recommended_action": result.get("recommended_action"),
            "confidence": result.get("confidence"),
            "evidence_keys": [
                str(key) for key in sorted(evidence.keys(), key=str)
                if not any(marker in str(key).lower() for marker in ("secret", "token", "password", "api_key", "authorization"))
            ][:10],
        })
    actions = []
    for action in (history.get("actions") or [])[:10]:
        if not isinstance(action, dict):
            continue
        actions.append({
            "id": action.get("id"),
            "check_key": action.get("checkKey") or action.get("check_key"),
            "action_type": action.get("actionType") or action.get("action_type"),
            "execution_mode": action.get("executionMode") or action.get("execution_mode"),
            "status": action.get("status"),
        })
    verification = history.get("verification") if isinstance(history.get("verification"), dict) else {}
    after = verification.get("after") if isinstance(verification.get("after"), dict) else {}
    after_products = [item for item in (after.get("products") or []) if isinstance(item, dict)]
    return {
        "round": int(history.get("round") or 0),
        "executed_check_items": [
            str(item) for item in (history.get("executed_check_items") or [])[:10]
            if str(item) in PLANNER_EXECUTOR_KEYS
        ],
        "executor_results": executor_results,
        "actions": actions,
        "verification": {
            "result_count": after.get("result_count"),
            "product_ids": [
                item.get("productId") or item.get("product_id")
                for item in after_products[:5]
                if item.get("productId") or item.get("product_id")
            ],
            "relevance": _summarize_relevance_diagnostics(after.get("relevance_diagnostics")),
        },
    }


def _summarize_product_for_planner(product: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "product_id": product.get("productId") or product.get("product_id"),
        "rank": product.get("rank"),
        "is_relevant": product.get("is_relevant"),
        "tag_name": product.get("tag_name") or product.get("tagName"),
        "price": product.get("price"),
        "broken_rate": product.get("broken_rate"),
        "size_inventory": _summarize_size_inventory(product.get("size_inventory")),
    }


def _summarize_size_inventory(size_inventory: Any) -> Dict[str, Any]:
    if not isinstance(size_inventory, dict):
        return {}
    available_sizes = [
        str(item)
        for item in (size_inventory.get("available_sizes") or [])
        if str(item).strip()
    ]
    unavailable_sizes = [
        str(item)
        for item in (size_inventory.get("unavailable_sizes") or [])
        if str(item).strip()
    ]
    return {
        "broken_rate": size_inventory.get("broken_rate"),
        "available_size_count": len(available_sizes),
        "unavailable_size_count": len(unavailable_sizes),
    }


def _summarize_product_cohort_for_planner(products: List[Dict[str, Any]]) -> Dict[str, Any]:
    inspected = [product for product in products if isinstance(product, dict)]
    inventory_risks = []
    irrelevant = []
    for product in inspected:
        product_id = product.get("productId") or product.get("product_id")
        broken_rate = product.get("broken_rate")
        if broken_rate is not None and float(broken_rate or 0) > 0 and len(inventory_risks) < 5:
            inventory_risks.append(product_id)
        if product.get("is_relevant") is False and len(irrelevant) < 5:
            irrelevant.append(product_id)
    return {
        "inspected_product_count": len(inspected),
        "irrelevant_count": sum(1 for product in inspected if product.get("is_relevant") is False),
        "inventory_risk_count": sum(1 for product in inspected if float(product.get("broken_rate") or 0) > 0),
        "inventory_risk_examples": inventory_risks,
        "irrelevant_examples": irrelevant,
    }


def _select_product_examples_for_planner(products: List[Dict[str, Any]], max_examples: int = 6) -> List[Dict[str, Any]]:
    def sort_key(product: Dict[str, Any]) -> tuple:
        is_irrelevant = product.get("is_relevant") is False
        broken_rate = float(product.get("broken_rate") or 0)
        rank = int(product.get("rank") or 9999)
        return (
            0 if is_irrelevant else 1,
            -broken_rate,
            rank,
        )

    prioritized = sorted(
        [product for product in products if isinstance(product, dict)],
        key=sort_key,
    )
    return [_summarize_product_for_planner(product) for product in prioritized[:max_examples]]


def _summarize_relevance_diagnostics(relevance_diagnostics: Any) -> Dict[str, Any]:
    if not isinstance(relevance_diagnostics, dict):
        return {}
    items = relevance_diagnostics.get("items") or []
    mismatch_examples = []
    check_failed_examples = []
    for item in items:
        if not isinstance(item, dict):
            continue
        summary = {
            "product_id": item.get("product_id"),
            "rank": item.get("rank"),
            "name": item.get("name"),
            "relevance_check_status": item.get("relevance_check_status"),
            "is_relevant": item.get("is_relevant"),
        }
        if item.get("is_relevant") is False and len(mismatch_examples) < 3:
            mismatch_examples.append(summary)
        if item.get("relevance_check_status") == "check_failed" and len(check_failed_examples) < 3:
            check_failed_examples.append(summary)
    return {
        "checked_count": int(relevance_diagnostics.get("checked_count") or 0),
        "mismatch_count": int(relevance_diagnostics.get("mismatch_count") or 0),
        "check_failed_count": int(relevance_diagnostics.get("check_failed_count") or 0),
        "mismatch_ratio": relevance_diagnostics.get("mismatch_ratio"),
        "mismatch_examples": mismatch_examples,
        "check_failed_examples": check_failed_examples,
    }


def _summarize_open_factor_context(open_factor_context: Any) -> Dict[str, Any]:
    if not isinstance(open_factor_context, dict):
        return {}
    matched_factors = open_factor_context.get("matched_factors") or []
    return {
        "season": open_factor_context.get("season"),
        "matched_factor_count": len(matched_factors),
        "matched_factors": matched_factors[:3],
        "suggested_tags": (open_factor_context.get("suggested_tags") or [])[:5],
        "season_mismatch": bool(open_factor_context.get("season_mismatch")),
        "weather": _summarize_open_factor_subsignal(open_factor_context.get("weather")),
        "festival": _summarize_open_factor_subsignal(open_factor_context.get("festival")),
        "social_trend": _summarize_open_factor_subsignal(open_factor_context.get("social_trend")),
    }


def _summarize_open_factor_subsignal(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in ["status", "summary", "matched", "reason"] if key in value}


def _summarize_history(history: Any) -> Dict[str, Any]:
    if not isinstance(history, dict):
        return {}
    action_logs = history.get("action_logs") or []
    inventory_recovery_tasks = history.get("inventory_recovery_tasks") or []
    top_query_actions = history.get("top_query_actions") or []
    return {
        "action_log_count": len(action_logs),
        "inventory_recovery_task_count": len(inventory_recovery_tasks),
        "top_query_action_count": len(top_query_actions),
        "recent_action_types": [item.get("action_type") for item in action_logs[:5] if isinstance(item, dict) and item.get("action_type")],
        "recent_action_statuses": [item.get("status") for item in top_query_actions[:5] if isinstance(item, dict) and item.get("status")],
        "recent_tool_keys": [item.get("tool_key") for item in top_query_actions[:5] if isinstance(item, dict) and item.get("tool_key")],
    }


def _planner_phase_one_system_prompt() -> str:
    return (
        "你是 Top Query 搜索治理 Planner 的诊断阶段。"
        "你必须输出原始 JSON object。"
        "第一字符必须是{，最后字符必须是}。"
        "不要输出```json、不要输出Markdown、不要输出解释文本。"
        "root_causes 只能从 recall,relevance,open_factor,inventory 中选择。"
        "不要输出中文枚举值，不要把原因句子写进 root_causes。"
        "只输出字段：objective、root_causes、strategy_reason、conversion_hypothesis、risk_level、evidence_refs、policy_acknowledgements。"
        "本阶段只做根因诊断，不输出 stages、不输出 actions。"
        "必须逐项确认 fact_pack.policyFacts 中的当前值、阈值和比较状态，不得输出与政策事实冲突的结论。"
        "如果输入包含 policy_violation，必须针对冲突重新诊断并返回完整修正版 JSON。"
        "优先依据 result_count、previous_result_count、断码风险、相关性汇总计数和世界知识命中摘要做判断。"
        "不要复述全部商品明细，不要逐个商品解释。"
        "断码必须结合转化受损假设，不能只因阈值命中就治理。"
    )


def _planner_phase_two_system_prompt() -> str:
    return (
        "你是 Top Query 搜索治理 Planner 的编排阶段。"
        "你必须输出原始 JSON object。"
        "第一字符必须是{，最后字符必须是}。"
        "不要输出```json、不要输出Markdown、不要输出解释文本。"
        "strategy.stages[].executor_keys 只能从 recall,relevance,open_factor,inventory 中选择。"
        "已执行过的检查默认不得重复；只有存在治理后新证据且确需重试时，才把对应检查写入 strategy.stages[].retry_check_items。"
        "allowed_action_types 只能从 product_blacklist,query_demote,tag_bucket_config,kg_tag,kg_relation 中选择。"
        "不要输出中文枚举值，不要把原因句子写进 executor_keys。"
        "只能使用 diagnosis.root_causes、fact_pack.available.executors 和 fact_pack.available.tools 中允许的能力。"
        "不得编排与 fact_pack.policyFacts 或 diagnosis.policy_acknowledgements 冲突的阶段。"
        "如果输入包含 policy_violation，必须针对冲突重新编排并返回完整修正版 JSON。"
        "世界知识只能通过 tag_bucket_config 做当前 Query 的标签权重调整，禁止 KG 动作。"
        "必须至少输出一个可执行 stage；如果不确定，也要基于 diagnosis.root_causes 给出最保守的可执行 stage。"
        "输出字段：strategy.stages、strategy.success_criteria、strategy.replan_triggers；可选输出 checks、actions。"
    )


def _validate_check_item(check_item: str) -> None:
    if check_item not in PLANNER_EXECUTOR_KEYS:
        raise PlannerStrategyError(f"Unknown check item: {check_item}")


def _validate_executor_key(executor_key: str) -> None:
    if executor_key not in PLANNER_EXECUTOR_KEYS:
        raise PlannerStrategyError(f"Unknown executor: {executor_key}")


def _validate_tool_key(tool_key: str) -> None:
    if tool_key not in PLANNER_TOOL_KEYS:
        raise PlannerStrategyError(f"Unknown tool: {tool_key}")


def _checks_from_strategy(strategy: GovernanceStrategy) -> List[PlannerCheck]:
    ordered = []
    for check_item in strategy.root_causes:
        if check_item not in ordered:
            ordered.append(check_item)
    for stage in strategy.stages:
        for executor_key in stage.executor_keys:
            if executor_key not in ordered:
                ordered.append(executor_key)
    return [PlannerCheck(check_item=check_item, reason="derived from governance strategy") for check_item in ordered]


def _token_usage_from_response(response: Any) -> int:
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        value = usage.get("total_tokens") or usage.get("totalTokenCount") or usage.get("total_token_count")
        if value is not None:
            return int(value or 0)
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, dict):
        usage = metadata.get("token_usage") or metadata.get("usage") or {}
        if isinstance(usage, dict):
            value = usage.get("total_tokens") or usage.get("totalTokenCount") or usage.get("total_token_count")
            if value is not None:
                return int(value or 0)
    return 0


def _planner_timeout_seconds() -> float:
    return float(os.getenv("TOP_QUERY_PLANNER_TIMEOUT_SECONDS", "120"))


def _planner_phase_one_timeout_seconds() -> float:
    return float(os.getenv("TOP_QUERY_PLANNER_PHASE1_TIMEOUT_SECONDS") or _planner_timeout_seconds())


def _planner_phase_two_timeout_seconds() -> float:
    return float(os.getenv("TOP_QUERY_PLANNER_PHASE2_TIMEOUT_SECONDS") or _planner_timeout_seconds())


def _planner_total_timeout_seconds() -> float:
    configured_total = os.getenv("TOP_QUERY_PLANNER_TOTAL_TIMEOUT_SECONDS")
    if configured_total:
        return float(configured_total)
    return _planner_phase_one_timeout_seconds() + _planner_phase_two_timeout_seconds()


def _planner_attempt_timeout_seconds(model_role: str) -> float:
    if model_role == "fallback":
        return float(os.getenv("TOP_QUERY_PLANNER_FALLBACK_ATTEMPT_TIMEOUT_SECONDS", "60"))
    return float(os.getenv("TOP_QUERY_PLANNER_PRIMARY_ATTEMPT_TIMEOUT_SECONDS", "90"))


def _correct_phase_one_policy_violations(
    phase_one_result: Dict[str, Any],
    violations: List[Any],
) -> Dict[str, Any]:
    corrected = dict(phase_one_result)
    recall_violation = next(
        (item for item in violations if getattr(item, "check_item", None) == "recall"),
        None,
    )
    if recall_violation is None:
        return corrected
    recall_facts = dict(getattr(recall_violation, "facts", {}) or {})
    acknowledgements = dict(corrected.get("policy_acknowledgements") or {})
    acknowledgements["recall"] = recall_facts
    corrected["policy_acknowledgements"] = acknowledgements
    root_causes = list(corrected.get("root_causes") or [])
    corrected["root_causes"] = ["recall", *[item for item in root_causes if item != "recall"]]
    return corrected


def _model_name_for_role(llm: Any, model_role: str) -> Optional[str]:
    if model_role == "fallback":
        return getattr(llm, "fallback_model_name", None)
    return getattr(llm, "primary_model_name", None)


def _structured_model_for_role(llm: Any, model_role: str, phase: str = "phase1") -> Optional[Any]:
    if _planner_structured_output_enabled(llm) is False:
        return None
    target = getattr(llm, "fallback", None) if model_role == "fallback" else getattr(llm, "primary", None)
    if target is None or not hasattr(target, "with_structured_output"):
        return None
    schema = _structured_schema_for_phase(phase)
    return target.with_structured_output(schema, method="json_schema", strict=True)


def _planner_structured_output_enabled(llm: Any) -> bool:
    flag = os.getenv("TOP_QUERY_PLANNER_STRUCTURED_OUTPUT_ENABLED")
    if flag is not None:
        return flag.strip().lower() not in {"0", "false", "off", "no"}
    return not isinstance(llm, FallbackChatModel)


def _is_structured_output_unsupported_error(exc: Exception) -> bool:
    message = str(exc)
    normalized = message.lower()
    return (
        ("response_format" in normalized and "json_schema" in normalized)
        or "json_schema field must be provided" in normalized
        or "parameter=response_format" in normalized
    )


def _loads_json_payload(response: Any) -> Any:
    raw = getattr(response, "content", response)
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        raise json.JSONDecodeError("Empty JSON payload", "", 0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        normalized = _strip_markdown_code_fence(text)
        if normalized != text:
            return json.loads(normalized)
        extracted = _extract_first_json_object(text)
        if extracted and extracted != text:
            return json.loads(extracted)
        raise


def _strip_markdown_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _extract_first_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return text


def _invoke_with_timeout(
    invoke_fn: Any,
    messages: List[Dict[str, str]],
    timeout_seconds: float,
    require_plain_json: bool = False,
) -> Any:
    invoke_kwargs: Dict[str, Any] = {"timeout": timeout_seconds}
    if require_plain_json:
        invoke_kwargs.update({
            "response_format": {"type": "json_object"},
            "extra_body": {"enable_thinking": False},
        })
    try:
        return invoke_fn(messages, **invoke_kwargs)
    except TypeError as exc:
        message = str(exc)
        if "unexpected keyword argument" in message or "positional arguments" in message:
            return invoke_legacy_with_timeout(invoke_fn, messages, timeout_seconds)
        raise


def _remaining_seconds(deadline: Optional[float]) -> float:
    if deadline is None:
        return float("inf")
    return max(0.0, deadline - time.monotonic())


def _mark_latest_planner_post_processing_error(phase_metrics: List[Dict[str, Any]], exc: Exception) -> None:
    if (
        phase_metrics
        and not isinstance(exc, TimeoutError)
        and not phase_metrics[-1].get("errorType")
    ):
        phase_metrics[-1]["postProcessingErrorType"] = type(exc).__name__


def _planner_llm_metrics(
    phase_metrics: List[Dict[str, Any]],
    final_error_type: Optional[str] = None,
    started_at: Optional[float] = None,
    budget_exhausted: bool = False,
) -> Dict[str, Any]:
    metrics = {
        "timeoutSeconds": _planner_total_timeout_seconds(),
        "phaseTimeoutSeconds": {
            "phase1": _planner_phase_one_timeout_seconds(),
            "phase2": _planner_phase_two_timeout_seconds(),
        },
        "attemptTimeoutSeconds": {
            "primary": _planner_attempt_timeout_seconds("primary"),
            "fallback": _planner_attempt_timeout_seconds("fallback"),
        },
        "phaseMetrics": phase_metrics,
    }
    success_metrics = [item for item in phase_metrics if not item.get("errorType")]
    metrics["requestChars"] = sum(int(item.get("requestChars") or 0) for item in success_metrics)
    metrics["responseChars"] = sum(int(item.get("responseChars") or 0) for item in success_metrics)
    metrics["tokenUsage"] = sum(int(item.get("tokenUsage") or 0) for item in success_metrics)
    metrics["totalDurationSeconds"] = round(
        time.monotonic() - started_at if started_at is not None else sum(float(item.get("durationSeconds") or 0) for item in phase_metrics),
        4,
    )
    metrics["errorType"] = final_error_type or next((item.get("errorType") for item in reversed(phase_metrics) if item.get("errorType")), None)
    metrics["usedFallbackModel"] = any(item.get("modelRole") == "fallback" for item in phase_metrics)
    metrics["budgetExhausted"] = budget_exhausted
    for phase in ["phase1", "phase2"]:
        latest = next((item for item in reversed(phase_metrics) if item.get("phase") == phase and not item.get("errorType")), None)
        if latest:
            metrics[phase] = latest
    attempts = list(phase_metrics)
    metrics["attempts"] = attempts
    metrics["attemptCount"] = len(attempts)
    return metrics


def _default_stages_for_root_causes(root_causes: List[str]) -> List[GovernanceStage]:
    ordered = []
    for check_item in ["recall", "open_factor", "relevance", "inventory"]:
        if check_item in root_causes and check_item not in ordered:
            ordered.append(check_item)
    stages: List[GovernanceStage] = []
    if "recall" in ordered:
        stages.append(GovernanceStage(
            stage_key="recall_repair",
            executor_keys=["recall"],
            reason="phase2 未提供可执行 stage，按召回根因生成保守兜底阶段",
            priority=len(stages) + 1,
            allowed_action_types=["kg_tag", "kg_relation"],
        ))
    if "open_factor" in ordered:
        stages.append(GovernanceStage(
            stage_key="world_knowledge_adjustment",
            executor_keys=["open_factor"],
            reason="phase2 未提供可执行 stage，按世界知识根因生成保守兜底阶段",
            priority=len(stages) + 1,
            allowed_action_types=["tag_bucket_config"],
        ))
    if "relevance" in ordered:
        stages.append(GovernanceStage(
            stage_key="relevance_quality",
            executor_keys=["relevance"],
            reason="phase2 未提供可执行 stage，按相关性根因生成保守兜底阶段",
            priority=len(stages) + 1,
            allowed_action_types=["product_blacklist", "kg_relation"],
        ))
    commercial = [item for item in ["inventory"] if item in ordered]
    if commercial:
        stages.append(GovernanceStage(
            stage_key="commercial_quality",
            executor_keys=commercial,
            reason="phase2 未提供可执行 stage，按商业质量根因生成保守兜底阶段",
            priority=len(stages) + 1,
            allowed_action_types=["query_demote"],
        ))
    return stages


def _safe_stage_priority(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    normalized = str(value).strip().lower()
    named_priorities = {
        "high": 1,
        "medium": default,
        "low": max(default, 3),
        "高": 1,
        "中": default,
        "低": max(default, 3),
    }
    if normalized in named_priorities:
        return named_priorities[normalized]
    try:
        return int(float(normalized))
    except (TypeError, ValueError):
        return default


def _structured_schema_for_phase(phase: str) -> Dict[str, Any]:
    if phase == "phase2":
        return {
            "name": "planner_phase_two",
            "schema": {
                "type": "object",
                "properties": {
                    "checks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "check_item": {"type": "string"},
                                "reason": {"type": "string"},
                                "severity": {"type": "string"},
                            },
                            "required": ["check_item", "reason", "severity"],
                            "additionalProperties": False,
                        },
                    },
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "check_item": {"type": "string"},
                                "action_type": {"type": "string"},
                                "reason": {"type": "string"},
                                "execution_mode": {"type": "string"},
                            },
                            "required": ["check_item", "action_type", "reason", "execution_mode"],
                            "additionalProperties": True,
                        },
                    },
                    "strategy": {
                        "type": "object",
                        "properties": {
                            "stages": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "stage_key": {"type": "string"},
                                        "executor_keys": {"type": "array", "items": {"type": "string"}},
                                        "reason": {"type": "string"},
                                        "allowed_action_types": {"type": "array", "items": {"type": "string"}},
                                        "priority": {"type": "integer"},
                                        "verification_required": {"type": "boolean"},
                                        "stage_goal": {"type": "string"},
                                        "entry_condition": {"type": "string"},
                                        "exit_condition": {"type": "string"},
                                        "risk_level": {"type": "string"},
                                        "depends_on": {"type": "array", "items": {"type": "string"}},
                                    },
                                    "required": ["stage_key", "executor_keys", "reason", "allowed_action_types"],
                                    "additionalProperties": False,
                                },
                            },
                            "success_criteria": {"type": "array", "items": {"type": "string"}},
                            "replan_triggers": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["stages", "success_criteria", "replan_triggers"],
                        "additionalProperties": False,
                    },
                },
                "required": ["strategy", "checks", "actions"],
                "additionalProperties": False,
            },
        }
    return {
        "name": "planner_phase_one",
        "schema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "root_causes": {"type": "array", "items": {"type": "string"}},
                "strategy_reason": {"type": "string"},
                "conversion_hypothesis": {"type": "string"},
                "risk_level": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "policy_acknowledgements": {
                    "type": "object",
                    "properties": {
                        "recall": {
                            "type": "object",
                            "properties": {
                                "currentResultCount": {"type": ["integer", "null"]},
                                "lowResultThreshold": {"type": ["integer", "null"]},
                                "quantityStatus": {
                                    "type": "string",
                                    "enum": ["no_result", "below_threshold", "at_or_above_threshold", "unknown"],
                                },
                                "ruleSource": {"type": "string"},
                            },
                            "required": [
                                "currentResultCount",
                                "lowResultThreshold",
                                "quantityStatus",
                                "ruleSource",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["recall"],
                    "additionalProperties": False,
                },
            },
            "required": [
                "objective",
                "root_causes",
                "strategy_reason",
                "conversion_hypothesis",
                "risk_level",
                "evidence_refs",
                "policy_acknowledgements",
            ],
            "additionalProperties": False,
        },
    }


def _attempt_entries_from_exception(exc: Exception) -> List[Dict[str, Any]]:
    llm_metrics = getattr(exc, "planner_llm_metrics", {}) or {}
    attempts = list(llm_metrics.get("attempts") or [])
    if attempts:
        return attempts
    error_type = llm_metrics.get("errorType")
    phase_metrics = llm_metrics.get("phaseMetrics") or []
    if error_type and phase_metrics:
        last_metric = phase_metrics[-1]
        return [{
            "phase": last_metric.get("phase"),
            "modelRole": last_metric.get("modelRole"),
            "modelName": last_metric.get("modelName"),
            "timeoutSeconds": last_metric.get("timeoutSeconds"),
            "durationSeconds": 0.0,
            "requestChars": last_metric.get("requestChars", 0),
            "responseChars": last_metric.get("responseChars", 0),
            "tokenUsage": last_metric.get("tokenUsage", 0),
            "errorType": error_type,
        }]
    return []


def _merge_attempt_entries(metrics_sink: List[Dict[str, Any]], exc: Exception) -> None:
    for candidate in _attempt_entries_from_exception(exc):
        phase = candidate.get("phase")
        model_role = candidate.get("modelRole")
        error_type = candidate.get("errorType")
        if any(
            item.get("phase") == phase
            and item.get("modelRole") == model_role
            and item.get("errorType") == error_type
            for item in metrics_sink
        ):
            continue
        metrics_sink.append(candidate)


def _coerce_check_item(raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    if not value:
        return ""
    if value in PLANNER_EXECUTOR_KEYS:
        return value

    lowered = value.lower()
    keyword_map = [
        ("open_factor", ("open_factor", "世界知识", "天气", "节日", "趋势", "季节")),
        ("relevance", ("relevance", "相关性", "不相关", "意图")),
        ("inventory", ("inventory", "断码", "库存", "尺码")),
        ("recall", ("recall", "召回", "结果量", "无结果", "少结果")),
    ]
    for check_item, keywords in keyword_map:
        if any(keyword in lowered or keyword in value for keyword in keywords):
            return check_item
    return ""


def _should_skip_llm_planning(plan: PlannerOutput) -> bool:
    check_items = {check.check_item for check in plan.checks}
    if not check_items:
        return True
    return check_items == {"inventory"}
