from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

POLICY_FACT_VERSION = "v1"


@dataclass(frozen=True)
class PolicyViolation:
    code: str
    check_item: str
    message: str
    facts: Dict[str, Any]
    conflicting_fields: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AgentPolicyComplianceError(RuntimeError):
    def __init__(
        self,
        *,
        phase: str,
        violations: List[PolicyViolation],
        attempts: int,
        token_usage: int = 0,
        llm_metrics: Optional[Dict[str, Any]] = None,
    ):
        self.phase = str(phase or "agent")
        self.violations = list(violations)
        self.attempts = max(int(attempts or 0), 1)
        self.token_usage = max(int(token_usage or 0), 0)
        self.llm_metrics = dict(llm_metrics or {})
        codes = ",".join(item.code for item in self.violations) or "unknown_policy_violation"
        super().__init__(f"{self.phase} policy compliance failed after {self.attempts} attempts: {codes}")


def build_policy_facts(
    context: Mapping[str, Any],
    *,
    config_provider: Optional[Callable[[str], Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    resolved_config_provider = config_provider or _default_config_provider
    current_count = _recall_result_count(context)
    threshold, rule_source = _recall_threshold(context, resolved_config_provider)
    if current_count is None or threshold is None:
        quantity_status = "unknown"
    elif current_count == 0:
        quantity_status = "no_result"
    elif current_count < threshold:
        quantity_status = "below_threshold"
    else:
        quantity_status = "at_or_above_threshold"
    return {
        "version": POLICY_FACT_VERSION,
        "recall": {
            "currentResultCount": current_count,
            "lowResultThreshold": threshold,
            "quantityStatus": quantity_status,
            "ruleSource": rule_source,
        },
    }


def validate_planner_output(plan: Any, policy_facts: Mapping[str, Any]) -> List[PolicyViolation]:
    plan_data = _as_mapping(plan)
    recall_fact = _as_mapping(policy_facts.get("recall"))
    if recall_fact.get("quantityStatus") not in {"no_result", "below_threshold"}:
        return []
    violations = _acknowledgement_violations(plan_data, recall_fact)
    if "recall" not in _planner_check_items(plan_data):
        violations.append(PolicyViolation(
            code="recall_policy_omitted",
            check_item="recall",
            message="Agent 未将明确的少结果事实纳入召回诊断",
            facts=dict(recall_fact),
            conflicting_fields=["checks", "strategy.root_causes", "strategy.stages"],
        ))
    return violations


def _acknowledgement_violations(output: Mapping[str, Any], recall_fact: Mapping[str, Any]) -> List[PolicyViolation]:
    acknowledgements = _as_mapping(
        output.get("policyAcknowledgements") or output.get("policy_acknowledgements")
    )
    recall_ack = _as_mapping(acknowledgements.get("recall"))
    compared_fields = ("currentResultCount", "lowResultThreshold", "quantityStatus")
    if all(recall_ack.get(field) == recall_fact.get(field) for field in compared_fields):
        return []
    return [PolicyViolation(
        code="recall_policy_acknowledgement_conflict",
        check_item="recall",
        message="Agent 对召回政策事实的确认缺失或不一致",
        facts=dict(recall_fact),
        conflicting_fields=[f"policyAcknowledgements.recall.{field}" for field in compared_fields],
    )]


def _planner_check_items(plan: Mapping[str, Any]) -> set[str]:
    check_items: set[str] = set()
    for check in plan.get("checks") or []:
        check_data = _as_mapping(check)
        value = check_data.get("check_item") or check_data.get("checkItem")
        if value:
            check_items.add(str(value))
    strategy = _as_mapping(plan.get("strategy"))
    for stage in strategy.get("stages") or []:
        stage_data = _as_mapping(stage)
        for value in stage_data.get("executor_keys") or stage_data.get("executorKeys") or []:
            if value:
                check_items.add(str(value))
    return check_items


def _recall_threshold(
    context: Mapping[str, Any],
    config_provider: Callable[[str], Mapping[str, Any]],
) -> tuple[Optional[int], str]:
    policy_summary = _as_mapping(context.get("policySummary"))
    thresholds = _as_mapping(policy_summary.get("thresholds"))
    threshold = _parse_int(thresholds.get("lowResultThreshold"))
    if threshold is not None and threshold > 0:
        return threshold, "policy_summary"
    brand = _context_brand(context)
    config = config_provider(brand) if brand else {}
    threshold = _parse_int(_as_mapping(config).get("top_query_low_result_threshold"))
    if threshold is not None and threshold > 0:
        return threshold, "brand_config"
    return None, "missing"


def _recall_result_count(context: Mapping[str, Any]) -> Optional[int]:
    candidates: List[Any] = []
    after_snapshot = _as_mapping(context.get("afterSnapshot"))
    after = _as_mapping(after_snapshot.get("after"))
    candidates.extend([
        after.get("result_count"),
        after.get("resultCount"),
        after.get("currentResultCount"),
        context.get("result_count"),
    ])
    search = _as_mapping(context.get("search"))
    candidates.extend([search.get("result_count"), search.get("resultCount")])
    before_snapshot = _as_mapping(context.get("beforeSnapshot"))
    candidates.extend([before_snapshot.get("result_count"), before_snapshot.get("resultCount")])
    for candidate in candidates:
        parsed = _parse_int(candidate)
        if parsed is not None and parsed >= 0:
            return parsed
    return None


def _context_brand(context: Mapping[str, Any]) -> str:
    direct = context.get("brand")
    if direct:
        return str(direct)
    task = _as_mapping(context.get("task"))
    if task.get("brand"):
        return str(task["brand"])
    query = _as_mapping(context.get("query"))
    return str(query.get("brand") or "")


def _as_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    return {}


def _parse_int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _default_config_provider(brand: str) -> Mapping[str, Any]:
    from src.config import get_brand_business_config

    return get_brand_business_config(brand)
