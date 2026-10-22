from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


TOP_QUERY_GOVERNANCE_EVENT_TYPES = frozenset({
    "context_collected",
    "plan_created",
    "executor_completed",
    "kg_executor_completed",
    "action_verified",
    "action_failed",
    "action_inconclusive",
    "search_verified",
})


WORLD_KNOWLEDGE_FACTOR_TYPES = ["season", "weather", "festival", "social_trend"]


def _default_world_knowledge_factor_assessments() -> List[Dict[str, Any]]:
    return [
        {
            "type": factor_type,
            "status": "not_assessed",
            "query_relevance": "unknown",
            "evidence": ["当前决策由系统降级生成，Agent 未提供该信号的结构化评估"],
            "expected_impact": "unknown",
            "candidate_tags": [],
        }
        for factor_type in WORLD_KNOWLEDGE_FACTOR_TYPES
    ]


@dataclass
class PlannerCheck:
    check_item: str
    reason: str
    severity: str = "warning"


@dataclass
class ActionCandidate:
    check_item: str
    action_type: str
    reason: str
    execution_mode: str
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    expected_impact: str = ""
    negative_impact: str = ""
    confidence: float = 0.0


@dataclass
class GovernanceStage:
    stage_key: str
    executor_keys: List[str]
    reason: str
    priority: int = 0
    verification_required: bool = True
    allowed_action_types: List[str] = field(default_factory=list)
    stage_goal: str = ""
    entry_condition: str = ""
    exit_condition: str = ""
    risk_level: str = ""
    depends_on: List[str] = field(default_factory=list)
    retry_check_items: List[str] = field(default_factory=list)


@dataclass
class GovernanceStrategy:
    objective: str
    root_causes: List[str] = field(default_factory=list)
    stages: List[GovernanceStage] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    replan_triggers: List[str] = field(default_factory=list)
    strategy_reason: str = ""
    conversion_hypothesis: str = ""
    evidence_refs: List[str] = field(default_factory=list)
    risk_level: str = ""


@dataclass
class PlannerOutput:
    checks: List[PlannerCheck] = field(default_factory=list)
    actions: List[ActionCandidate] = field(default_factory=list)
    strategy: Optional[GovernanceStrategy] = None
    llm_used: bool = False
    fallback_reason: Optional[str] = None
    token_usage: int = 0
    timeout_seconds: Optional[float] = None
    fact_pack_chars: Optional[int] = None
    request_chars: Optional[int] = None
    response_chars: Optional[int] = None
    error_type: Optional[str] = None
    llm_metrics: Dict[str, Any] = field(default_factory=dict)
    policy_acknowledgements: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutorResult:
    check_item: str
    status: str = "warning"
    root_cause: str = "uncertain"
    recommended_action: str = "observe"
    actions: List[ActionCandidate] = field(default_factory=list)
    kg_action_intents: List[Dict[str, Any]] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    conversion_hypothesis: str = ""
    expected_impact: str = ""
    negative_impact: str = ""
    confidence: float = 0.0
    token_usage: Optional[int] = None
    llm_metrics: Optional[Dict[str, Any]] = None


@dataclass
class WorldKnowledgeDecision:
    decision: str
    should_adjust: bool
    confidence: float
    decision_reason: str
    considered_factors: List[str] = field(default_factory=lambda: list(WORLD_KNOWLEDGE_FACTOR_TYPES))
    factor_assessments: List[Dict[str, Any]] = field(default_factory=_default_world_knowledge_factor_assessments)
    audit_status: str = "complete"
    audit_issues: List[str] = field(default_factory=list)
    candidate_tags: Dict[str, Any] | List[str] = field(default_factory=list)
    selected_tags: List[str] = field(default_factory=list)
    config_plan: List[Dict[str, Any]] = field(default_factory=list)
    rejected_tags: List[Dict[str, Any]] = field(default_factory=list)
    existing_config_hit: bool = False
    config_mode: str = "skip"
    after_bucket: str = "top"
    payload_preview: Dict[str, Any] = field(default_factory=dict)
    expected_impact: str = ""
    risk_level: str = ""
    verify_plan: Dict[str, Any] = field(default_factory=dict)
