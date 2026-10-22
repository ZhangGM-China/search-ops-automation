from dataclasses import asdict, dataclass
import datetime
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy import func

from src.persistence import (
    SessionLocal,
    TopQueryActionPlan,
    TopQueryAgentEvent,
    TopQueryBudgetLedger,
    TopQueryStepStatus,
    TopQueryTask,
    append_top_query_action_plan,
    append_top_query_agent_event,
    append_top_query_context_snapshot,
    append_top_query_step_status,
)
from src.config import get_brand_business_config
from src.llm.rd_client import FallbackChatModel, build_rd_chat_model
from src.llm.node_config import (
    NodeConfigError,
    NodeConfigSnapshot,
    ResolvedNodeConfig,
    capture_snapshot,
    get_snapshot_node_config,
)
from src.integrations.operational_service import OperationalService

from .context import TopQueryContextCollector
from .executors import (
    KnowledgeGraphExecutor,
    OpenFactorExecutor,
    RecallExecutor,
    VerificationExecutor,
    WorldKnowledgeDecisionAgent,
)
from .planner import (
    DeterministicFallbackPlanner,
    PlannerAgent,
    ensure_world_knowledge_assessment,
    strip_world_knowledge_assessment,
)
from .policy import ActionPolicy
from .policy_compliance import AgentPolicyComplianceError
from .query_demotion_contract import uses_authoritative_query_demotion
from src.governance.query_identity import normalize_governance_query_key, normalize_query_key
from .schemas import ActionCandidate, ExecutorResult
from .recall_scoring import build_recall_assessment
from .kg_planner import TopQueryKnowledgeGraphPlanner
from .sanitization import sanitize_action_error
from .budget import (
    BudgetConfig,
    BudgetDecision,
    action_fingerprint,
    check_before_planner,
    check_plan,
    check_round_actions,
    load_query_budget,
)


def _to_plain_data(value: Any) -> Any:
    """Convert dataclasses and nested containers for checkpoint payloads."""
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _to_plain_data(getattr(value, name))
            for name in value.__dataclass_fields__
        }
    if isinstance(value, Mapping):
        return {str(key): _to_plain_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_plain_data(item) for item in value]
    return value
from .persistence import (
    BudgetExceededError,
    BudgetReservationError,
    BudgetReservationRequest,
    BudgetSettlementRequest,
    WorkflowCheckpointCodec,
    create_top_query_persistence_services,
)


_ATOMIC_BUDGET_KEYS = (
    "top_query_brand_day_token_limit",
    "top_query_brand_day_action_limit",
    "top_query_batch_token_limit",
    "top_query_batch_action_limit",
    "top_query_llm_reserve_tokens",
    "top_query_max_consecutive_planner_failures",
    "top_query_max_consecutive_tool_failures",
    "top_query_max_consecutive_no_improvement",
)

QUERY_DEMOTION_VERIFICATION_DEADLINE_SECONDS = 15 * 60
QUERY_DEMOTION_VERIFICATION_LEASE_SECONDS = 60
_QUERY_DEMOTION_LOW_WEIGHT_BUCKETS = frozenset({
    "productIdsByOpsLowWeight",
    "productIdsByLowWeight",
})


@dataclass(frozen=True)
class AtomicWorkflowBudgetConfig:
    brand_day_token_limit: int = 0
    brand_day_action_limit: int = 0
    batch_token_limit: int = 0
    batch_action_limit: int = 0
    llm_reserve_tokens: int = 0
    max_planner_failures: int = 0
    max_tool_failures: int = 0
    max_no_improvement: int = 0
    valid: bool = False
    error: str = ""

    @classmethod
    def from_mapping(cls, values: Any) -> "AtomicWorkflowBudgetConfig":
        source = values if isinstance(values, dict) else {}
        parsed = {}
        for key in _ATOMIC_BUDGET_KEYS:
            value = source.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                return cls(error=f"invalid atomic budget config: {key}")
            parsed[key] = value
        query_token_limit = source.get("top_query_token_limit")
        if (
            query_token_limit is not None
            and (
                isinstance(query_token_limit, bool)
                or not isinstance(query_token_limit, int)
                or parsed["top_query_llm_reserve_tokens"] < query_token_limit
            )
        ):
            return cls(error="invalid atomic budget config: top_query_llm_reserve_tokens")

        return cls(
            brand_day_token_limit=parsed["top_query_brand_day_token_limit"],
            brand_day_action_limit=parsed["top_query_brand_day_action_limit"],
            batch_token_limit=parsed["top_query_batch_token_limit"],
            batch_action_limit=parsed["top_query_batch_action_limit"],
            llm_reserve_tokens=parsed["top_query_llm_reserve_tokens"],
            max_planner_failures=parsed["top_query_max_consecutive_planner_failures"],
            max_tool_failures=parsed["top_query_max_consecutive_tool_failures"],
            max_no_improvement=parsed["top_query_max_consecutive_no_improvement"],
            valid=True,
        )


class TopQueryWorkflowState(TypedDict, total=False):
    task_id: int
    round_count: int
    current_stage_index: int
    dry_run: bool
    context: Dict[str, Any]
    initial_context: Dict[str, Any]
    plan: Any
    executor_results: List[ExecutorResult]
    executed_check_items: List[str]
    kg_intents: List[Dict[str, Any]]
    verification: Dict[str, Any]
    final_status: str
    replan_reason: str
    has_manual_actions: bool
    verification_blockers: List[Dict[str, Any]]
    round_action_ids: List[int]
    budget_stop: Dict[str, Any]
    failure_stop: Dict[str, Any]
    compliance_stop: Dict[str, Any]
    prepared_kg_result: Any
    prepared_kg_actions: List[Dict[str, Any]]
    prepared_kg_duration: float
    run_id: int
    checkpoint_version: int
    resume_node: str
    atomic_account_ids: Dict[str, int]
    batch_key: str
    action_reservation_keys: List[str]
    planner_failures: int
    tool_failures: int
    no_improvement: int
    retry_node: str
    worker_id: str
    workflow_lease_seconds: int
    token_budget_exhausted: bool
    token_budget_reason: str
    continue_stage: bool


_BUDGET_CONFIG_UNSET = object()


def _action_plan_identity(
    state: TopQueryWorkflowState,
    action: ActionCandidate,
    action_index: int,
) -> Dict[str, Any]:
    round_no = int(state.get("round_count") or 0) + 1
    workflow_run_id = state.get("run_id")
    payload = json.dumps(
        action.payload or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    run_scope = f"run:{workflow_run_id}" if workflow_run_id is not None else f"task:{state['task_id']}"
    return {
        "workflow_run_id": workflow_run_id,
        "round_no": round_no,
        "idempotency_key": ":".join((
            "top-query-action",
            run_scope,
            f"round:{round_no}",
            str(action.check_item or "unknown"),
            str(action.action_type or "unknown"),
            f"index:{action_index}",
            digest,
        )),
    }


def _planner_timeout_seconds() -> float:
    return float(os.getenv("TOP_QUERY_PLANNER_TIMEOUT_SECONDS", "120"))


def _set_task_status(task_id: int, status: str) -> None:
    db = SessionLocal()
    try:
        task = db.get(TopQueryTask, task_id)
        if task:
            task.status = status # type: ignore
            db.commit()
    finally:
        db.close()


def create_top_query_workflow(
    context_collector: TopQueryContextCollector = None,
    planner: PlannerAgent = None,
    verification_service: Any = None,
    check_top_n: int = 16,
    low_result_threshold: int = 4,
    drop_ratio_threshold: float = 0.4,
    broken_rate_threshold: float = 0.6,
    check_modes: Dict[str, str] = None,
    tool_modes: Dict[str, str] = None,
    auto_action_executor: Any = None,
    max_rounds: int = 2,
    kg_planner: Any = None,
    world_knowledge_agent: Any = None,
    allow_deterministic_world_knowledge_fallback: bool = False,
    operational_service: Any = None,
    budget_config: Any = _BUDGET_CONFIG_UNSET,
    atomic_budget_config: Optional[Dict[str, Any]] = None,
    persistence_services: Optional[tuple[Any, Any]] = None,
    model_config_snapshot: Optional[NodeConfigSnapshot] = None,
):
    collector = context_collector
    resolved_operational_service = (
        operational_service
        or (getattr(collector, "service", None) if collector is not None else None)
        or OperationalService()
    )
    if collector is None:
        collector = TopQueryContextCollector(
            check_top_n=check_top_n,
            service=resolved_operational_service,
        )
    else:
        setattr(collector, "service", resolved_operational_service)
    policy = ActionPolicy(check_modes=check_modes, tool_modes=tool_modes)
    fallback_planner = DeterministicFallbackPlanner(
        low_result_threshold=low_result_threshold,
        drop_ratio_threshold=drop_ratio_threshold,
        broken_rate_threshold=broken_rate_threshold,
        tag_bucket_api_configured=(policy.tool_modes.get("tag_bucket_config") != "disabled"),
    )
    planner_snapshot = model_config_snapshot or capture_snapshot(
        node_keys=(
            "top_query.planner",
            "top_query.world_knowledge",
        ),
    )
    _planner_config_from_snapshot(planner_snapshot)
    get_snapshot_node_config(
        planner_snapshot,
        "top_query.world_knowledge",
        required_channel="rd_llm",
    )
    planner_agent = planner or PlannerAgent(
        llm=_default_planner_llm(planner_snapshot),
        fallback_planner=fallback_planner,
        check_modes=check_modes,
        tool_modes=tool_modes,
        prefer_deterministic_for_simple_cases=True,
        policy_compliance_enabled=True,
        model_config=_planner_config_from_snapshot(planner_snapshot),
    )
    resolved_world_knowledge_agent = (
        world_knowledge_agent
        if world_knowledge_agent is not None
        else WorldKnowledgeDecisionAgent(model_config_snapshot=planner_snapshot)
    )
    verifier = VerificationExecutor(
        search_service=verification_service or resolved_operational_service,
        page_size=check_top_n,
    )
    graph_planner = kg_planner or TopQueryKnowledgeGraphPlanner()
    budget_guard_enabled = budget_config is not _BUDGET_CONFIG_UNSET
    resolved_budget_config = BudgetConfig.from_mapping(
        budget_config if budget_guard_enabled else {}
    )
    atomic_budget_service, workflow_run_service = (
        persistence_services or create_top_query_persistence_services()
    )
    explicit_atomic_config = (
        AtomicWorkflowBudgetConfig.from_mapping(atomic_budget_config)
        if atomic_budget_config is not None
        else None
    )
    if (
        explicit_atomic_config is not None
        and explicit_atomic_config.valid
        and resolved_budget_config.valid
        and explicit_atomic_config.llm_reserve_tokens < resolved_budget_config.token_limit
    ):
        explicit_atomic_config = AtomicWorkflowBudgetConfig(
            error="invalid atomic budget config: top_query_llm_reserve_tokens"
        )

    def atomic_config_for_task(task: Any) -> AtomicWorkflowBudgetConfig:
        if explicit_atomic_config is not None:
            return explicit_atomic_config
        return AtomicWorkflowBudgetConfig.from_mapping(
            get_brand_business_config(str(task.brand or ""))
        )

    def ensure_atomic_accounts(task: Any, config: AtomicWorkflowBudgetConfig) -> Dict[str, int]:
        data_date = task.data_date or datetime.date.today()
        period_start = datetime.datetime.combine(data_date, datetime.time.min)
        period_end = period_start + datetime.timedelta(days=1)
        brand = str(task.brand or "")
        # Keep legacy scope_type values for ledger compatibility while the
        # task-specific key prevents one Query from consuming another's budget.
        batch_key = f"{brand}:{data_date.isoformat()}:task:{int(task.id)}"
        return {
            "brand_day": atomic_budget_service.ensure_account(
                scope_type="brand_day",
                scope_key=batch_key,
                period_start=period_start,
                period_end=period_end,
                token_limit=config.brand_day_token_limit,
                action_limit=config.brand_day_action_limit,
            ),
            "batch": atomic_budget_service.ensure_account(
                scope_type="batch",
                scope_key=batch_key,
                period_start=period_start,
                period_end=period_end,
                token_limit=config.batch_token_limit,
                action_limit=config.batch_action_limit,
            ),
        }

    def reserve_atomic(
        state: TopQueryWorkflowState,
        node: str,
        *,
        tokens: int,
        actions: int = 0,
    ) -> List[str]:
        account_ids = state.get("atomic_account_ids") or {}
        round_no = int(state.get("round_count") or 0) + 1
        keys = []
        requests = []
        attempt = (
            int(state.get("planner_failures") or 0)
            if node == "planner"
            else int(state.get("tool_failures") or 0)
        )
        for scope in ("brand_day", "batch"):
            key = f"run:{state['run_id']}:round:{round_no}:{node}:attempt:{attempt}:{scope}"
            keys.append(key)
            requests.append(BudgetReservationRequest(
                account_id=int(account_ids[scope]),
                workflow_run_id=int(state["run_id"]),
                task_id=int(state["task_id"]),
                round_no=round_no,
                tokens=tokens,
                actions=actions,
                idempotency_key=key,
                reason=f"{node} pre-call reservation",
            ))
        atomic_budget_service.reserve_many(requests)
        return keys

    def llm_reserve_tokens(state: TopQueryWorkflowState) -> int:
        if explicit_atomic_config is not None:
            return explicit_atomic_config.llm_reserve_tokens
        db = SessionLocal()
        try:
            task = db.get(TopQueryTask, state["task_id"])
            if task is None:
                raise ValueError(f"top query task not found: {state['task_id']}")
            return atomic_config_for_task(task).llm_reserve_tokens
        finally:
            db.close()

    def atomic_limits(state: TopQueryWorkflowState) -> AtomicWorkflowBudgetConfig:
        if explicit_atomic_config is not None:
            return explicit_atomic_config
        db = SessionLocal()
        try:
            task = db.get(TopQueryTask, state["task_id"])
            if task is None:
                raise ValueError(f"top query task not found: {state['task_id']}")
            return atomic_config_for_task(task)
        finally:
            db.close()

    def workflow_lease_seconds(task: Any) -> int:
        value = get_brand_business_config(str(task.brand or "")).get(
            "top_query_workflow_lease_seconds", 300,
        )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return 300
        return value

    def settle_atomic(
        reservation_keys: List[str],
        *,
        tokens: int,
        actions: int = 0,
    ) -> None:
        atomic_budget_service.settle_many([
            BudgetSettlementRequest(
                reservation_key=key,
                actual_tokens=tokens,
                actual_actions=actions,
                idempotency_key=f"{key}:settle",
            )
            for key in reservation_keys
        ])

    def release_atomic(reservation_keys: List[str]) -> None:
        atomic_budget_service.release_many([
            BudgetSettlementRequest(
                reservation_key=key,
                actual_tokens=0,
                actual_actions=0,
                idempotency_key=f"{key}:release",
            )
            for key in reservation_keys
        ])

    def reconcile_open_action_reservations(state: TopQueryWorkflowState) -> None:
        run_id = state.get("run_id")
        if not run_id:
            return
        db = SessionLocal()
        try:
            ledgers = db.query(TopQueryBudgetLedger).filter(
                TopQueryBudgetLedger.workflow_run_id == int(run_id),
            ).order_by(TopQueryBudgetLedger.id.asc()).all()
            terminal_keys = set()
            reserves = []
            settled_actions = {}
            for row in ledgers:
                detail = _safe_json_loads(row.detail_payload) or {}
                if row.entry_type in {"settle", "release"} and isinstance(detail, dict):
                    terminal_key = detail.get("reservationKey")
                    if terminal_key:
                        terminal_keys.add(str(terminal_key))
                    if row.entry_type == "settle":
                        scope = (int(row.round_no or 0), int(row.account_id))
                        settled_actions[scope] = settled_actions.get(scope, 0) + max(
                            0, int(row.action_delta or 0),
                        )
                if row.entry_type == "reserve" and ":actions:" in str(row.idempotency_key or ""):
                    reserves.append(row)
            open_rows = [row for row in reserves if str(row.idempotency_key) not in terminal_keys]
            if not open_rows:
                return
            actions_by_round = {
                int(round_no): int(count)
                for round_no, count in db.query(
                    TopQueryActionPlan.round_no,
                    func.count(TopQueryActionPlan.id),
                ).filter(
                    TopQueryActionPlan.workflow_run_id == int(run_id),
                ).group_by(TopQueryActionPlan.round_no).all()
            }
        finally:
            db.close()
        settlements = []
        releases = []
        rows_by_scope = {}
        for row in open_rows:
            rows_by_scope.setdefault(
                (int(row.round_no or 0), int(row.account_id)), [],
            ).append(row)
        for scope, rows in rows_by_scope.items():
            round_no, _ = scope
            remaining_actions = max(
                0,
                actions_by_round.get(round_no, 0) - settled_actions.get(scope, 0),
            )
            reserved_capacity = sum(max(0, int(row.action_delta or 0)) for row in rows)
            if remaining_actions > reserved_capacity:
                raise BudgetReservationError("persisted actions exceed open reservation capacity")
            for row in sorted(rows, key=lambda item: int(item.id)):
                actual_actions = min(
                    remaining_actions,
                    max(0, int(row.action_delta or 0)),
                )
                remaining_actions -= actual_actions
                request = BudgetSettlementRequest(
                    reservation_key=str(row.idempotency_key),
                    actual_tokens=0,
                    actual_actions=actual_actions,
                    idempotency_key=f"{row.idempotency_key}:finalize",
                    reason="finalize open action reservation",
                )
                (settlements if actual_actions else releases).append(request)
        if settlements:
            atomic_budget_service.settle_many(settlements)
        if releases:
            atomic_budget_service.release_many(releases)

    def checkpoint(
        state: TopQueryWorkflowState,
        updates: Dict[str, Any],
        next_node: str,
    ) -> Dict[str, Any]:
        if not state.get("run_id"):
            return updates
        merged = {**state, **updates}
        encoded = WorkflowCheckpointCodec.encode(next_node=next_node, state=merged)
        saved = workflow_run_service.save_checkpoint(
            run_id=int(state["run_id"]),
            expected_version=int(state.get("checkpoint_version") or 0),
            current_node=next_node,
            round_no=int(merged.get("round_count") or 0),
            checkpoint=encoded,
            planner_failures=int(merged.get("planner_failures") or 0),
            tool_failures=int(merged.get("tool_failures") or 0),
            no_improvement=int(merged.get("no_improvement") or 0),
            worker_id=str(state["worker_id"]),
            lease_seconds=int(state["workflow_lease_seconds"]),
        )
        return {**updates, "checkpoint_version": saved.checkpoint_version}

    def checkpointed(node_fn: Any, next_node_fn: Any):
        def wrapped(state: TopQueryWorkflowState) -> Dict[str, Any]:
            updates = node_fn(state)
            merged = {**state, **updates}
            next_node = next_node_fn(merged)
            return checkpoint(state, updates, next_node)
        return wrapped

    def existing_budget_stop(task_id: int) -> Optional[Dict[str, Any]]:
        db = SessionLocal()
        try:
            existing_events = db.query(TopQueryAgentEvent).filter(
                TopQueryAgentEvent.task_id == task_id,
                TopQueryAgentEvent.agent_name == "WorkflowGuard",
                TopQueryAgentEvent.event_type == "executor_completed",
            ).order_by(TopQueryAgentEvent.id.asc()).all()
            for event in existing_events:
                try:
                    existing_payload = json.loads(event.output_payload or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                budget_stop = existing_payload.get("budgetStop") if isinstance(existing_payload, dict) else None
                if isinstance(budget_stop, dict) and budget_stop.get("reason"):
                    return budget_stop
        finally:
            db.close()
        return None

    def existing_compliance_stop(task_id: int) -> Optional[Dict[str, Any]]:
        db = SessionLocal()
        try:
            events = db.query(TopQueryAgentEvent).filter(
                TopQueryAgentEvent.task_id == task_id,
                TopQueryAgentEvent.agent_name == "WorkflowGuard",
                TopQueryAgentEvent.event_type == "executor_completed",
            ).order_by(TopQueryAgentEvent.id.asc()).all()
            for event in events:
                try:
                    payload = json.loads(event.output_payload or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                compliance_stop = payload.get("complianceStop") if isinstance(payload, dict) else None
                if isinstance(compliance_stop, dict) and compliance_stop.get("phase"):
                    return compliance_stop
        finally:
            db.close()
        return None

    def stop_for_budget(
        task_id: int,
        decision: Any,
        stage: str,
        *,
        token_usage: int = 0,
        duration: float = 0.0,
    ) -> Dict[str, Any]:
        existing = existing_budget_stop(task_id)
        if existing is not None:
            return existing
        payload = {
            "schemaVersion": 2,
            "artifact": {"artifactType": "budget_stop"},
            "budgetStop": {
                "reason": decision.reason,
                "stage": stage,
                "detail": decision.detail or {},
            },
        }
        append_top_query_agent_event(task_id, {
            "agent_name": "WorkflowGuard",
            "stage": stage,
            "event_type": "executor_completed",
            "summary": f"Query 治理预算停止: {decision.reason}",
            "output_payload": payload,
            "duration": max(0.0, float(duration or 0.0)),
            "token_usage": max(0, int(token_usage or 0)),
        })
        return payload["budgetStop"]

    def stop_for_failure(task_id: int, node: str, exc: Exception) -> Dict[str, Any]:
        token_usage = max(0, int(getattr(exc, "token_usage", 0) or 0))
        for metrics_name in ("planner_llm_metrics", "llm_metrics"):
            metrics = getattr(exc, metrics_name, None)
            if isinstance(metrics, dict):
                token_usage = max(token_usage, int(metrics.get("tokenUsage") or metrics.get("token_usage") or 0))
        failure_stop = {
            "node": node,
            "reason": "node_execution_failed",
            "errorType": type(exc).__name__,
            "errorSummary": sanitize_action_error(exc),
        }
        append_top_query_agent_event(task_id, {
            "agent_name": "WorkflowGuard",
            "stage": node,
            "event_type": "executor_completed",
            "summary": f"Query 治理节点失败: {node}",
            "output_payload": {
                "schemaVersion": 2,
                "artifact": {"artifactType": "failure_stop"},
                "failureStop": failure_stop,
            },
            "duration": max(0.0, float(getattr(exc, "_node_duration", 0.0) or 0.0)),
            "token_usage": token_usage,
        })
        return failure_stop

    def stop_for_compliance(task_id: int, exc: AgentPolicyComplianceError) -> Dict[str, Any]:
        existing = existing_compliance_stop(task_id)
        if existing is not None:
            return existing
        llm_metrics = _sanitize_llm_metrics(getattr(exc, "llm_metrics", {}) or {})
        compliance_stop = {
            "phase": exc.phase,
            "attempts": exc.attempts,
            "violations": [item.to_dict() for item in exc.violations],
        }
        append_top_query_agent_event(task_id, {
            "agent_name": "WorkflowGuard",
            "stage": exc.phase,
            "event_type": "executor_completed",
            "summary": f"Agent 政策事实依从性停止: {exc.phase}",
            "output_payload": {
                "schemaVersion": 2,
                "artifact": {"artifactType": "agent_policy_compliance_stop"},
                "complianceStop": compliance_stop,
                "llmMetrics": llm_metrics,
            },
            "duration": max(0.0, float(getattr(exc, "_node_duration", 0.0) or 0.0)),
            "token_usage": max(0, int(getattr(exc, "token_usage", 0) or 0)),
        })
        return compliance_stop

    def guard_node(node: str, node_fn: Any):
        def guarded(state: TopQueryWorkflowState) -> Dict[str, Any]:
            try:
                return node_fn(state)
            except AgentPolicyComplianceError as exc:
                return {
                    "retry_node": "",
                    "compliance_stop": stop_for_compliance(state["task_id"], exc),
                }
            except Exception as exc:
                limits = atomic_limits(state)
                if node == "planner":
                    failures = int(state.get("planner_failures") or 0) + 1
                    if failures < limits.max_planner_failures:
                        return {"planner_failures": failures, "retry_node": "budget_gate"}
                    return {
                        "planner_failures": failures,
                        "retry_node": "",
                        "failure_stop": stop_for_failure(state["task_id"], node, exc),
                    }
                if node in {"executor", "verification"}:
                    failures = int(state.get("tool_failures") or 0) + 1
                    if getattr(exc, "retryable", True) is False:
                        return {
                            "tool_failures": failures,
                            "retry_node": "",
                            "failure_stop": stop_for_failure(
                                state["task_id"],
                                str(getattr(exc, "_workflow_failure_node", node)),
                                exc,
                            ),
                        }
                    if failures < limits.max_tool_failures:
                        retry_node = "execute_actions" if node == "executor" else "verify"
                        return {"tool_failures": failures, "retry_node": retry_node}
                    return {
                        "tool_failures": failures,
                        "retry_node": "",
                        "failure_stop": stop_for_failure(
                            state["task_id"],
                            str(getattr(exc, "_workflow_failure_node", node)),
                            exc,
                        ),
                    }
                return {"failure_stop": stop_for_failure(state["task_id"], node, exc)}
        return guarded

    def collect_context_node(state: TopQueryWorkflowState) -> Dict[str, Any]:
        started_at = time.perf_counter()
        try:
            context = collector.collect(state["task_id"])
        except Exception as exc:
            setattr(exc, "_node_duration", time.perf_counter() - started_at)
            raise
        duration = time.perf_counter() - started_at
        append_top_query_agent_event(state["task_id"], {
            "agent_name": "ContextCollector",
            "stage": "collecting_context",
            "event_type": "context_collected",
            "summary": "Top Query 上下文采集完成",
            "output_payload": _build_context_event_payload(context),
            "duration": duration,
        })
        return {"context": context, "initial_context": context}

    def entry_budget_gate_node(state: TopQueryWorkflowState) -> Dict[str, Any]:
        worker_id = str(state.get("worker_id") or uuid.uuid4().hex)
        db = SessionLocal()
        try:
            task = db.get(TopQueryTask, state["task_id"])
            if task is None:
                raise ValueError(f"top query task not found: {state['task_id']}")
            task_brand = str(task.brand or "")
            task_date = task.data_date or datetime.date.today()
            atomic_config = atomic_config_for_task(task)
            lease_seconds = workflow_lease_seconds(task)
            run = workflow_run_service.claim(
                task_id=int(state["task_id"]),
                run_key=f"task:{state['task_id']}:{uuid.uuid4().hex}",
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            base = {
                "run_id": run.id,
                "checkpoint_version": run.checkpoint_version,
                "batch_key": f"{task_brand}:{task_date.isoformat()}:task:{int(task.id)}",
                "worker_id": worker_id,
                "workflow_lease_seconds": lease_seconds,
            }
            if not atomic_config.valid:
                decision = BudgetDecision(False, "invalid_atomic_budget_config", {
                    "error": atomic_config.error,
                })
                return {
                    **base,
                    "budget_stop": stop_for_budget(state["task_id"], decision, "entry"),
                    "resume_node": "finalize",
                }
            base["atomic_account_ids"] = ensure_atomic_accounts(task, atomic_config)
            if run.checkpoint_version > 0 and run.checkpoint:
                resume_node, restored = WorkflowCheckpointCodec.decode(run.checkpoint)
                return {
                    **restored,
                    **base,
                    "resume_node": resume_node,
                    "budget_stop": existing_budget_stop(state["task_id"]) or restored.get("budget_stop") or {},
                    "compliance_stop": existing_compliance_stop(state["task_id"]) or restored.get("compliance_stop") or {},
                }
        finally:
            db.close()
        existing = existing_budget_stop(state["task_id"])
        compliance_stop = existing_compliance_stop(state["task_id"])
        return {
            **base,
            "budget_stop": existing or {},
            "compliance_stop": compliance_stop or {},
            "resume_node": "finalize" if existing or compliance_stop else "collect_context",
        }

    def route_entry_budget_gate(state: TopQueryWorkflowState) -> str:
        if state.get("budget_stop") or state.get("compliance_stop"):
            return "finalize"
        return str(state.get("resume_node") or "collect_context")

    def budget_gate_node(state: TopQueryWorkflowState) -> Dict[str, Any]:
        if not budget_guard_enabled:
            return {"budget_stop": {}, "token_budget_exhausted": False, "token_budget_reason": ""}
        existing = existing_budget_stop(state["task_id"])
        if existing is not None:
            if existing.get("reason") == "query_token_limit_reached":
                return {
                    "budget_stop": {},
                    "token_budget_exhausted": True,
                    "token_budget_reason": "token_budget_exhausted",
                }
            return {"budget_stop": existing}
        snapshot = load_query_budget(state["task_id"])
        decision = check_before_planner(resolved_budget_config, snapshot)
        if decision.allowed:
            return {"budget_stop": {}, "token_budget_exhausted": False, "token_budget_reason": ""}
        if decision.reason == "query_token_limit_reached":
            return {
                "budget_stop": {},
                "token_budget_exhausted": True,
                "token_budget_reason": "token_budget_exhausted",
            }
        return {"budget_stop": stop_for_budget(state["task_id"], decision, "planning")}

    def plan_node(state: TopQueryWorkflowState) -> Dict[str, Any]:
        _set_task_status(state["task_id"], "planning")
        token_budget_exhausted_before_call = bool(state.get("token_budget_exhausted"))
        atomic_reservations: List[str] = []
        if not token_budget_exhausted_before_call:
            try:
                atomic_reservations = reserve_atomic(
                    state,
                    "planner",
                    tokens=llm_reserve_tokens(state),
                )
            except BudgetExceededError:
                decision = BudgetDecision(False, "atomic_budget_limit_reached", {"node": "planner"})
                return {"plan": None, "budget_stop": stop_for_budget(
                    state["task_id"], decision, "planning",
                )}
        started_at = time.perf_counter()
        try:
            if token_budget_exhausted_before_call:
                plan = fallback_planner.plan(state["context"])
                plan.llm_used = False
                plan.fallback_reason = "token_budget_exhausted"
                plan.token_usage = 0
                plan.llm_metrics = _token_budget_metrics(
                    load_query_budget(state["task_id"]).token_usage,
                    resolved_budget_config.token_limit,
                )
            else:
                plan = planner_agent.plan(state["context"])
        except Exception as exc:
            usage = max(0, int(getattr(exc, "token_usage", 0) or 0))
            if atomic_reservations and usage:
                settle_atomic(atomic_reservations, tokens=usage)
            elif atomic_reservations:
                release_atomic(atomic_reservations)
            setattr(exc, "_node_duration", time.perf_counter() - started_at)
            raise
        executed_check_items = {
            str(check_item)
            for check_item in (state.get("executed_check_items") or [])
            if str(check_item)
        }
        if "open_factor" not in executed_check_items:
            plan = ensure_world_knowledge_assessment(plan)
        else:
            plan = strip_world_knowledge_assessment(plan)
        duration = time.perf_counter() - started_at
        if atomic_reservations:
            settle_atomic(
                atomic_reservations,
                tokens=max(0, int(getattr(plan, "token_usage", 0) or 0)),
            )
        strategy_payload = asdict(plan.strategy) if getattr(plan, "strategy", None) else None
        planner_metrics = {
            "timeoutSeconds": getattr(plan, "timeout_seconds", None),
            "factPackChars": getattr(plan, "fact_pack_chars", None),
            "requestChars": getattr(plan, "request_chars", None),
            "responseChars": getattr(plan, "response_chars", None),
            "errorType": getattr(plan, "error_type", None),
        }
        planner_metrics.update(getattr(plan, "llm_metrics", {}) or {})
        planner_metrics = _successful_event_llm_metrics(planner_metrics)
        plan_round = int(state.get("round_count") or 0) + 1
        plan_payload = {
            "schemaVersion": 2,
            "round": plan_round,
            "artifact": {"artifactType": "plan", "round": plan_round},
            "checks": [check.__dict__ for check in plan.checks],
            "actions": [action.__dict__ for action in plan.actions],
            "strategy": strategy_payload,
            "stages": _summarize_planner_stages(plan),
            "successCriteria": list(getattr(getattr(plan, "strategy", None), "success_criteria", []) or []),
            "conversionHypothesis": str(
                getattr(getattr(plan, "strategy", None), "conversion_hypothesis", "") or ""
            ),
            "riskLevel": str(getattr(getattr(plan, "strategy", None), "risk_level", "") or ""),
            "downstream": _planner_downstream(plan),
            "fallbackReason": _successful_event_telemetry_string(
                getattr(plan, "fallback_reason", None)
            ),
            "llmMetrics": planner_metrics,
            "relevanceDiagnostics": state["context"].get("relevance_diagnostics") or {},
            "kgSignals": {
                "kg_candidate_gap": bool(state["context"].get("kg_candidate_gap")),
                "kg_issue": bool(state["context"].get("kg_issue")),
            },
        }
        budget_snapshot = load_query_budget(state["task_id"])
        projected_tokens = budget_snapshot.token_usage + max(0, int(getattr(plan, "token_usage", 0) or 0))
        projected_duration = budget_snapshot.active_duration_seconds + max(0.0, duration)
        token_budget_exhausted = token_budget_exhausted_before_call or (
            budget_guard_enabled
            and projected_tokens >= resolved_budget_config.token_limit
        )
        if budget_guard_enabled and projected_duration >= resolved_budget_config.active_duration_seconds:
            decision = BudgetDecision(False, "query_active_duration_limit_reached", {
                "usedSeconds": budget_snapshot.active_duration_seconds,
                "plannedSeconds": duration,
                "limitSeconds": resolved_budget_config.active_duration_seconds,
            })
            return {"plan": None, "budget_stop": stop_for_budget(
                state["task_id"],
                decision,
                "planning",
                token_usage=int(getattr(plan, "token_usage", 0) or 0),
                duration=duration,
            )}
        plan_decision = check_plan(resolved_budget_config, budget_snapshot, plan_payload) if budget_guard_enabled else None
        if plan_decision is not None and not plan_decision.allowed:
            return {"plan": None, "budget_stop": stop_for_budget(
                state["task_id"],
                plan_decision,
                "planning",
                token_usage=int(getattr(plan, "token_usage", 0) or 0),
                duration=duration,
            )}
        append_top_query_agent_event(state["task_id"], {
            "agent_name": "PlannerAgent",
            "stage": "planning",
            "event_type": "plan_created",
            "summary": ",".join(check.check_item for check in plan.checks) or "no_action_needed",
            "output_payload": plan_payload,
            "duration": duration,
            "token_usage": getattr(plan, "token_usage", 0),
        })
        return {
            "plan": plan,
            "current_stage_index": 0,
            "replan_reason": "",
            "planner_failures": 0,
            "retry_node": "",
            "token_budget_exhausted": token_budget_exhausted,
            "token_budget_reason": "token_budget_exhausted" if token_budget_exhausted else "",
        }

    def execute_node(state: TopQueryWorkflowState) -> Dict[str, Any]:
        _set_task_status(state["task_id"], "executing")
        current_stage_index = int(state.get("current_stage_index") or 0)
        check_items = _effective_current_stage_check_items(state)
        context = _context_with_recall_suppression(
            state,
            low_result_threshold=low_result_threshold,
            drop_ratio_threshold=drop_ratio_threshold,
        )
        token_usage_total = load_query_budget(state["task_id"]).token_usage
        token_budget_exhausted = bool(state.get("token_budget_exhausted")) or (
            budget_guard_enabled and token_usage_total >= resolved_budget_config.token_limit
        )
        executor_atomic_reservations: List[str] = []
        if {"open_factor"} & set(check_items) and not token_budget_exhausted:
            try:
                executor_atomic_reservations = reserve_atomic(
                    state,
                    "executor",
                    tokens=llm_reserve_tokens(state),
                )
            except BudgetExceededError:
                decision = BudgetDecision(False, "atomic_budget_limit_reached", {"node": "executor"})
                return {"budget_stop": stop_for_budget(
                    state["task_id"], decision, "executing",
                )}
        executed_results: List[tuple[ExecutorResult, float]] = []

        def run_executor(
            executor: Any,
            check_item: str,
            *,
            uses_llm: bool = False,
            budget_fallback: bool = False,
        ) -> None:
            nonlocal token_usage_total, token_budget_exhausted
            if uses_llm and token_budget_exhausted:
                executed_results.append((
                    _token_budget_skipped_executor_result(
                        check_item,
                        token_usage_total,
                        resolved_budget_config.token_limit,
                    ),
                    0.0,
                ))
                return
            started_at = time.perf_counter()
            try:
                result = executor.execute(context)
            except Exception as exc:
                usage = sum(
                    max(0, int(getattr(completed, "token_usage", 0) or 0))
                    for completed, _ in executed_results
                ) + max(0, int(getattr(exc, "token_usage", 0) or 0))
                if executor_atomic_reservations and usage:
                    settle_atomic(executor_atomic_reservations, tokens=usage)
                elif executor_atomic_reservations:
                    release_atomic(executor_atomic_reservations)
                setattr(exc, "_node_duration", time.perf_counter() - started_at)
                raise
            if budget_fallback:
                result.llm_metrics = {
                    **(getattr(result, "llm_metrics", {}) or {}),
                    **_token_budget_metrics(token_usage_total, resolved_budget_config.token_limit),
                }
                result.evidence = {
                    **(getattr(result, "evidence", {}) or {}),
                    "token_budget_reason": "token_budget_exhausted",
                }
            executed_results.append((result, time.perf_counter() - started_at))
            token_usage_total += max(0, int(getattr(result, "token_usage", 0) or 0))
            if budget_guard_enabled and token_usage_total >= resolved_budget_config.token_limit:
                token_budget_exhausted = True

        previous_results_by_check = {
            result.check_item: result
            for result in (state.get("executor_results") or [])
            if isinstance(result, ExecutorResult) and result.check_item
        }
        if "open_factor" in check_items:
            run_executor(OpenFactorExecutor(
                tag_bucket_api_configured=(policy.tool_modes.get("tag_bucket_config") != "disabled"),
            policy=policy,
            world_knowledge_agent=resolved_world_knowledge_agent,
            allow_deterministic_fallback=allow_deterministic_world_knowledge_fallback,
            operational_service=getattr(collector, "service", None),
            ), "open_factor", uses_llm=True)
        if "recall" in check_items:
            if context.get("suppress_recall_check"):
                executed_results.append((ExecutorResult(
                    check_item="recall",
                    status="pass",
                    root_cause="none",
                    recommended_action="skip",
                    evidence={
                        "suppressed": True,
                        "reason": "world_knowledge_stage_refreshed_result_set",
                        "sevenDayAverage": context.get("seven_day_average"),
                        "historySampleCount": int(context.get("history_sample_count") or 0),
                    },
                    expected_impact="世界知识治理后结果集已恢复，无需重复执行旧召回阶段",
                    confidence=1.0,
                ), 0.0))
            else:
                run_executor(
                    RecallExecutor(low_result_threshold=low_result_threshold, drop_ratio_threshold=drop_ratio_threshold),
                    "recall",
                )
        if "relevance" in check_items:
            executed_results.append((ExecutorResult(
                check_item="relevance",
                status="skipped",
                root_cause="retired_legacy_stage",
                recommended_action="skip",
                evidence={"reason": "legacy_relevance_pipeline_retired"},
                confidence=1.0,
            ), 0.0))
        executor_names = {
            "recall": "RecallExecutor",
            "relevance": "RelevanceNodeRetired",
            "open_factor": "OpenFactorExecutor",
        }
        current_results: List[ExecutorResult] = []
        for raw_result, duration in executed_results:
            result = _normalize_disabled_action_result(raw_result)
            current_results.append(result)
        kg_intents = [
            intent
            for result in current_results
            for intent in result.kg_action_intents
        ]
        prepared_kg_result = None
        prepared_kg_actions: List[Dict[str, Any]] = []
        prepared_kg_duration = 0.0
        if kg_intents:
            kg_started_at = time.perf_counter()
            try:
                prepared_kg_result = KnowledgeGraphExecutor(kg_planner=graph_planner).execute(
                    kg_intents,
                    state.get("context") or {},
                )
            except Exception as exc:
                usage = sum(
                    max(0, int(getattr(completed, "token_usage", 0) or 0))
                    for completed in current_results
                ) + max(0, int(getattr(exc, "token_usage", 0) or 0))
                if executor_atomic_reservations and usage:
                    settle_atomic(executor_atomic_reservations, tokens=usage)
                elif executor_atomic_reservations:
                    release_atomic(executor_atomic_reservations)
                setattr(exc, "_node_duration", time.perf_counter() - kg_started_at)
                setattr(exc, "_workflow_failure_node", "knowledge_graph_executor")
                raise
            prepared_kg_duration = time.perf_counter() - kg_started_at
            for action in prepared_kg_result.actions:
                execution_mode = policy.resolve(action.check_item, action.action_type)
                prepared_kg_actions.append({"action": action, "execution_mode": execution_mode})
        round_actions = [
            {
                "actionType": action.action_type,
                "checkKey": action.check_item,
                "payload": action.payload,
            }
            for result in current_results
            for action in result.actions
            if action.execution_mode not in {"dry_run", "disabled"}
        ]
        round_actions.extend({
            "actionType": item["action"].action_type,
            "checkKey": item["action"].check_item,
            "payload": item["action"].payload,
        } for item in prepared_kg_actions if item["execution_mode"] not in {"dry_run", "disabled"})
        existing_round_fingerprints = _persisted_round_action_fingerprints(
            state["task_id"],
            state.get("run_id"),
            int(state.get("round_count") or 0) + 1,
        )
        new_round_actions = [
            item for item in round_actions
            if action_fingerprint(item["actionType"], item["checkKey"], item["payload"])
            not in existing_round_fingerprints
        ]
        executor_tokens = sum(int(getattr(result, "token_usage", 0) or 0) for result in current_results)
        executor_tokens += int(getattr(prepared_kg_result, "token_usage", 0) or 0) if prepared_kg_result else 0
        if executor_atomic_reservations:
            settle_atomic(executor_atomic_reservations, tokens=executor_tokens)
        action_decision = check_round_actions(
            resolved_budget_config,
            load_query_budget(state["task_id"]),
            new_round_actions,
        ) if budget_guard_enabled else None
        if action_decision is not None and not action_decision.allowed:
            consumed_tokens = sum(int(getattr(result, "token_usage", 0) or 0) for result in current_results)
            consumed_tokens += int(getattr(prepared_kg_result, "token_usage", 0) or 0) if prepared_kg_result else 0
            consumed_duration = sum(duration for _, duration in executed_results) + prepared_kg_duration
            return {"budget_stop": stop_for_budget(
                state["task_id"],
                action_decision,
                "executing",
                token_usage=consumed_tokens,
                duration=consumed_duration,
            )}
        action_reservation_keys: List[str] = []
        if round_actions:
            try:
                action_reservation_keys = reserve_atomic(
                    state,
                    "actions",
                    tokens=0,
                    actions=len(round_actions),
                )
            except BudgetExceededError:
                decision = BudgetDecision(False, "atomic_action_limit_reached", {
                    "planned": len(round_actions),
                })
                return {"budget_stop": stop_for_budget(
                    state["task_id"], decision, "executing",
                )}
        planned_checks = _planned_check_items(state["plan"])
        result_by_check = dict(previous_results_by_check)
        for result in current_results:
            result_by_check[result.check_item] = result
        results: List[ExecutorResult] = []
        for check_item in ["recall", "relevance", "open_factor"]:
            preserved = result_by_check.get(check_item)
            if preserved is not None:
                results.append(preserved)
                continue
            if check_item not in planned_checks:
                if check_item == "recall":
                    results.append(RecallExecutor(
                        low_result_threshold=low_result_threshold,
                        drop_ratio_threshold=drop_ratio_threshold,
                    ).execute(context))
                else:
                    results.append(ExecutorResult(check_item=check_item, status="pass", root_cause="none", recommended_action="observe"))

        has_manual_actions = bool(state.get("has_manual_actions"))
        round_action_ids = list(state.get("round_action_ids") or [])
        auto_action_attempts = 0
        safe_prewrite_failures = 0
        completed_real_auto_actions = 0
        duration_by_check = {result.check_item: duration for result, duration in zip(current_results, [item[1] for item in executed_results])}
        for result in current_results:
            append_top_query_step_status(state["task_id"], {
                "check_key": result.check_item,
                "status": result.status,
                "display_status": _display_status_for_step(result.status),
                "summary": _step_summary_from_result(result),
                "evidence": result.evidence,
            })
            proposed_actions = []
            pending_auto_actions = []
            for action_index, action in enumerate(result.actions):
                action_id = None
                if action.execution_mode not in {"dry_run", "disabled"}:
                    status = "waiting_human_approval" if action.execution_mode == "human_approval" else "approved"
                    if action.execution_mode == "human_approval":
                        has_manual_actions = True
                    action_id = append_top_query_action_plan(state["task_id"], {
                        **_action_plan_identity(state, action, action_index),
                        "check_key": action.check_item,
                        "tool_key": action.action_type,
                        "action_type": action.action_type,
                        "execution_mode": action.execution_mode,
                        "status": status,
                        "request_payload": action.payload,
                        "reason": action.reason,
                    })
                    round_action_ids.append(action_id)
                    if (
                        action.execution_mode == "auto"
                        and auto_action_executor
                        and _action_plan_status(action_id) == "approved"
                    ):
                        pending_auto_actions.append(action_id)
                proposed_actions.append(_executor_action_artifact(action, action_id))
            is_suppressed = bool((result.evidence or {}).get("suppressed"))
            append_top_query_agent_event(state["task_id"], {
                "agent_name": "WorkflowGuard" if is_suppressed else executor_names.get(result.check_item, "GovernanceExecutor"),
                "stage": "executing",
                "event_type": "executor_completed",
                "summary": _step_summary_from_result(result),
                "output_payload": _executor_event_artifact(result, proposed_actions),
                "duration": duration_by_check.get(result.check_item, 0.0),
                "token_usage": int(getattr(result, "token_usage", 0) or 0),
            })
            for action_id in pending_auto_actions:
                auto_action_attempts += 1
                try:
                    auto_action_executor(action_id, bool(state.get("dry_run", True)))
                    if not bool(state.get("dry_run", True)):
                        completed_real_auto_actions += 1
                except Exception as exc:
                    if not _is_safe_prewrite_action_failure(exc):
                        raise
                    safe_prewrite_failures += 1
                    append_top_query_agent_event(state["task_id"], {
                        "agent_name": "WorkflowGuard",
                        "stage": "executing",
                        "event_type": "stage_continuation",
                        "summary": "前置治理动作未写入，继续执行后续独立阶段",
                        "output_payload": {
                            "actionId": action_id,
                            "errorType": type(exc).__name__,
                            "errorSummary": sanitize_action_error(exc),
                            "writeSucceeded": False,
                        },
                        "duration": 0.0,
                        "token_usage": 0,
                    })
        executed_check_items = list(state.get("executed_check_items") or [])
        for result in current_results:
            if result.check_item and result.check_item not in executed_check_items:
                executed_check_items.append(result.check_item)
        continue_stage = (
            _has_remaining_stages(state)
            and not has_manual_actions
            and not prepared_kg_actions
            and not token_budget_exhausted
            and completed_real_auto_actions == 0
            and safe_prewrite_failures == auto_action_attempts
        )
        if continue_stage and action_reservation_keys:
            settle_atomic(
                list(action_reservation_keys),
                tokens=0,
                actions=len(round_action_ids),
            )
        return {
            "executor_results": results,
            "executed_check_items": executed_check_items,
            "current_stage_index": (
                current_stage_index + 1
                if continue_stage
                else current_stage_index
            ),
            "continue_stage": continue_stage,
            "kg_intents": kg_intents,
            "has_manual_actions": has_manual_actions,
            "round_action_ids": [] if continue_stage else round_action_ids,
            "prepared_kg_result": prepared_kg_result,
            "prepared_kg_actions": prepared_kg_actions,
            "prepared_kg_duration": prepared_kg_duration,
            "action_reservation_keys": [] if continue_stage else action_reservation_keys,
            "tool_failures": 0,
            "retry_node": "",
            "token_budget_exhausted": token_budget_exhausted,
            "token_budget_reason": "token_budget_exhausted" if token_budget_exhausted else "",
        }

    def planner_review_node(state: TopQueryWorkflowState) -> Dict[str, Any]:
        _set_task_status(state["task_id"], "planner_reviewing")
        kg_intents = state.get("kg_intents") or []
        kg_result = state.get("prepared_kg_result")
        if not kg_intents or kg_result is None:
            if state.get("action_reservation_keys"):
                settle_atomic(
                    list(state["action_reservation_keys"]),
                    tokens=0,
                    actions=len(state.get("round_action_ids") or []),
                )
            return {}
        has_manual_actions = bool(state.get("has_manual_actions"))
        generated_actions = []
        for action_index, prepared in enumerate(state.get("prepared_kg_actions") or []):
            action = prepared["action"]
            execution_mode = prepared["execution_mode"]
            action_id = None
            if execution_mode not in {"dry_run", "disabled"}:
                status = "waiting_human_approval" if execution_mode == "human_approval" else "approved"
                if execution_mode == "human_approval":
                    has_manual_actions = True
                action_id = append_top_query_action_plan(state["task_id"], {
                    **_action_plan_identity(state, action, action_index),
                    "check_key": action.check_item,
                    "tool_key": action.action_type,
                    "action_type": action.action_type,
                    "execution_mode": execution_mode,
                    "status": status,
                    "request_payload": action.payload,
                    "reason": action.reason,
                })
            action_artifact = _executor_action_artifact(action, action_id)
            action_artifact["mode"] = execution_mode
            generated_actions.append(action_artifact)
        duration = float(state.get("prepared_kg_duration") or 0.0)
        action_ids = [item["actionId"] for item in generated_actions if item.get("actionId") is not None]
        round_action_ids = list(state.get("round_action_ids") or [])
        round_action_ids.extend(action_ids)
        missing_reasons, skipped_reasons = _kg_diagnostic_reasons(kg_result.evidence)
        kg_metrics = _sanitize_llm_metrics(
            getattr(kg_result, "llm_metrics", {}) or {}
        )
        kg_payload = {
            "schemaVersion": 2,
            "artifact": {"artifactType": "kg_executor_decision"},
            "intents": kg_intents,
            "status": kg_result.status,
            "rootCause": kg_result.root_cause,
            "evidence": kg_result.evidence,
            "generatedActions": generated_actions,
            "actionIds": action_ids,
            "missingReasons": missing_reasons,
            "skippedReasons": skipped_reasons,
            "duration": duration,
        }
        if kg_metrics:
            kg_payload["llmMetrics"] = kg_metrics
        append_top_query_agent_event(state["task_id"], {
            "agent_name": "KnowledgeGraphExecutor",
            "stage": "planner_reviewing",
            "event_type": "kg_executor_completed",
            "summary": _step_summary_from_result(kg_result),
            "output_payload": kg_payload,
            "duration": duration,
            "token_usage": int(getattr(kg_result, "token_usage", 0) or 0),
        })
        if state.get("action_reservation_keys"):
            settle_atomic(
                list(state["action_reservation_keys"]),
                tokens=0,
                actions=len(round_action_ids),
            )
        return {
            "executor_results": [*state["executor_results"], kg_result],
            "has_manual_actions": has_manual_actions,
            "round_action_ids": round_action_ids,
        }

    def verify_node(state: TopQueryWorkflowState) -> Dict[str, Any]:
        started_at = time.perf_counter()
        verification_context = dict(state["context"])
        open_factor_result = next(
            (
                result for result in (state.get("executor_results") or [])
                if getattr(result, "check_item", None) == "open_factor"
            ),
            None,
        )
        if open_factor_result is not None:
            decision = _world_knowledge_decision_payload(getattr(open_factor_result, "evidence", {}) or {})
            if decision:
                verification_context["world_knowledge_decision"] = decision
        tag_bucket_actions = [
            action for action in _task_actions_payload(state["task_id"])
            if action.get("actionType") == "tag_bucket_config"
            and action.get("status") in {"executed", "dry_run_executed"}
        ]
        if tag_bucket_actions:
            latest_response = tag_bucket_actions[-1].get("responsePayload") or {}
            if isinstance(latest_response, dict):
                action_verification = latest_response.get("world_knowledge_verification")
                if isinstance(action_verification, dict):
                    verification_context["world_knowledge_action_verification"] = action_verification
                if isinstance(latest_response.get("after_snapshot"), dict):
                    verification_context["tag_bucket_after_snapshot"] = latest_response["after_snapshot"]
        try:
            verification = verifier.execute(verification_context)
        except Exception as exc:
            setattr(exc, "_node_duration", time.perf_counter() - started_at)
            raise
        duration = time.perf_counter() - started_at
        append_top_query_context_snapshot(state["task_id"], "after_search", verification)
        _reconcile_executed_product_actions(state["task_id"], verification)
        verification_blockers = _task_inconclusive_action_blockers(state["task_id"])
        append_top_query_agent_event(state["task_id"], {
            "agent_name": "VerificationExecutor",
            "stage": "verification",
            "event_type": "search_verified",
            "summary": "治理后召回快照已保存",
            "output_payload": verification,
            "duration": duration,
        })
        return {
            "verification": verification,
            "verification_blockers": verification_blockers,
            "tool_failures": 0,
            "retry_node": "",
        }

    def should_replan_after_verification(state: TopQueryWorkflowState) -> str:
        if state.get("verification_blockers") or _task_inconclusive_action_blockers(state["task_id"]):
            return "finalize"
        if bool(state.get("has_manual_actions")) or _task_has_pending_manual_actions(state["task_id"]):
            return "finalize"
        next_round = int(state.get("round_count") or 0) + 1
        if next_round >= max_rounds:
            return "finalize"
        if not _task_has_executed_actions(state["task_id"], state.get("round_action_ids") or []):
            return "finalize"
        return "prepare_replan"

    def post_verification_budget_gate_node(state: TopQueryWorkflowState) -> Dict[str, Any]:
        if not budget_guard_enabled:
            return {"budget_stop": {}}
        decision = check_before_planner(
            resolved_budget_config,
            load_query_budget(state["task_id"]),
        )
        if decision.allowed:
            return {"budget_stop": {}}
        if decision.reason == "query_token_limit_reached":
            return {
                "budget_stop": {},
                "token_budget_exhausted": True,
                "token_budget_reason": "token_budget_exhausted",
            }
        return {"budget_stop": stop_for_budget(state["task_id"], decision, "verification")}

    def route_post_verification(state: TopQueryWorkflowState) -> str:
        if state.get("budget_stop"):
            return "finalize"
        return should_replan_after_verification(state)

    def prepare_replan_node(state: TopQueryWorkflowState) -> Dict[str, Any]:
        next_round = int(state.get("round_count") or 0) + 1
        verification = state.get("verification") or {}
        before = verification.get("before") if isinstance(verification, dict) else {}
        after = verification.get("after") if isinstance(verification, dict) else {}
        before = before if isinstance(before, dict) else {}
        after = after if isinstance(after, dict) else {}
        improved = _verification_improved(before, after)
        current_no_improvement = int(state.get("no_improvement") or 0)
        no_improvement = (
            current_no_improvement
            if improved is None
            else 0 if improved else current_no_improvement + 1
        )
        if improved is not None and no_improvement >= atomic_limits(state).max_no_improvement:
            decision = BudgetDecision(False, "consecutive_no_improvement_limit_reached", {
                "count": no_improvement,
            })
            return {
                "no_improvement": no_improvement,
                "budget_stop": stop_for_budget(state["task_id"], decision, "verification"),
            }
        after_context = _context_from_verification(state["context"], state.get("verification") or {})
        after_context["governance_history"] = {
            "round": next_round,
            "executed_check_items": list(state.get("executed_check_items") or []),
            "executor_results": _to_plain_data(state.get("executor_results") or []),
            "actions": _task_actions_payload(state["task_id"]),
            "verification": _to_plain_data(state.get("verification") or {}),
        }
        return {
            "context": after_context,
            "initial_context": state.get("initial_context") or state.get("context") or {},
            "round_count": next_round,
            "current_stage_index": 0,
            "plan": None,
            "executor_results": state.get("executor_results") or [],
            "executed_check_items": state.get("executed_check_items") or [],
            "kg_intents": [],
            "has_manual_actions": bool(state.get("has_manual_actions")),
            "round_action_ids": [],
            "replan_reason": "after_verification",
            "no_improvement": no_improvement,
        }

    def finalize_node(state: TopQueryWorkflowState) -> Dict[str, Any]:
        reconcile_open_action_reservations(state)
        db = SessionLocal()
        try:
            has_manual_action = bool(state.get("has_manual_actions")) or _task_has_pending_manual_actions(state["task_id"])
            has_unsettled_action = _task_has_unsettled_actions(state["task_id"])
            raw_manual_review = _results_require_manual_review(state.get("executor_results") or [])
            verification_blockers = (
                state.get("verification_blockers")
                or _task_inconclusive_action_blockers(state["task_id"])
            )
            if state.get("failure_stop"):
                status = "failed"
            else:
                status = "manual_required" if (
                    state.get("compliance_stop")
                    or
                    state.get("budget_stop")
                    or has_manual_action
                    or has_unsettled_action
                    or raw_manual_review
                    or verification_blockers
                    or bool(state.get("token_budget_exhausted"))
                ) else "completed"
            task = db.get(TopQueryTask, state["task_id"])
            if task:
                task.status = status # type: ignore
                task.display_status = (
                    "still_problematic" if status == "failed"
                    else "waiting_human_approval" if status == "manual_required"
                    else "normal"
                ) # type: ignore
                db.commit()
            if state.get("run_id"):
                workflow_run_service.finish(
                    run_id=int(state["run_id"]),
                    status=status,
                    expected_version=int(state.get("checkpoint_version") or 0),
                    worker_id=str(state["worker_id"]),
                )
            return {
                "final_status": status,
                "verification_blockers": verification_blockers,
            }
        finally:
            db.close()

    workflow = StateGraph(TopQueryWorkflowState)
    workflow.add_node("entry_budget_gate", entry_budget_gate_node)
    workflow.add_node("collect_context", checkpointed(
        guard_node("context", collect_context_node),
        lambda state: "finalize" if state.get("failure_stop") or state.get("compliance_stop") else "budget_gate",
    ))
    workflow.add_node("budget_gate", budget_gate_node)
    workflow.add_node("plan", checkpointed(
        guard_node("planner", plan_node),
        lambda state: {
            "execute": "execute_actions",
            "evaluate": "finalize",
            "retry": "budget_gate",
            "finalize": "finalize",
        }[_route_planned_work(state)],
    ))
    workflow.add_node("execute_actions", checkpointed(
        guard_node("executor", execute_node),
        lambda state: _route_after_execution(state),
    ))
    workflow.add_node("planner_review", checkpointed(planner_review_node, lambda _: "verify"))
    workflow.add_node("verify", checkpointed(
        guard_node("verification", verify_node),
        lambda state: (
            "finalize" if state.get("budget_stop") or state.get("failure_stop")
            else "verify" if state.get("retry_node") == "verify"
            else "post_verification_budget_gate"
        ),
    ))
    workflow.add_node("post_verification_budget_gate", post_verification_budget_gate_node)
    workflow.add_node("prepare_replan", checkpointed(
        prepare_replan_node,
        lambda state: "finalize" if state.get("budget_stop") else "budget_gate",
    ))
    workflow.add_node("finalize", finalize_node)

    workflow.set_entry_point("entry_budget_gate")
    workflow.add_conditional_edges("entry_budget_gate", route_entry_budget_gate, {
        "collect_context": "collect_context",
        "budget_gate": "budget_gate",
        "plan": "plan",
        "execute_actions": "execute_actions",
        "planner_review": "planner_review",
        "verify": "verify",
        "post_verification_budget_gate": "post_verification_budget_gate",
        "prepare_replan": "prepare_replan",
        "finalize": "finalize",
    })
    workflow.add_conditional_edges("collect_context", _route_after_execution, {
        "planner_review": "budget_gate",
        "finalize": "finalize",
    })
    workflow.add_conditional_edges("budget_gate", _route_budget_gate, {
        "plan": "plan",
        "finalize": "finalize",
    })
    workflow.add_conditional_edges("plan", _route_planned_work, {
        "execute": "execute_actions",
        "evaluate": "finalize",
        "retry": "budget_gate",
        "finalize": "finalize",
    })
    workflow.add_conditional_edges("execute_actions", _route_after_execution, {
        "planner_review": "planner_review",
        "execute_actions": "execute_actions",
        "finalize": "finalize",
    })
    workflow.add_edge("planner_review", "verify")
    workflow.add_conditional_edges("verify", _route_after_execution, {
        "planner_review": "post_verification_budget_gate",
        "verify": "verify",
        "finalize": "finalize",
    })
    workflow.add_conditional_edges("post_verification_budget_gate", route_post_verification, {
        "prepare_replan": "prepare_replan",
        "evaluate": "finalize",
        "finalize": "finalize",
    })
    workflow.add_conditional_edges("prepare_replan", lambda state: (
        "finalize" if state.get("budget_stop") else "budget_gate"
    ), {
        "budget_gate": "budget_gate",
        "finalize": "finalize",
    })
    workflow.add_edge("finalize", END)
    return workflow.compile()


def resolve_query_demotion_terminal_status(
    config_status: Any,
    search_status: Any,
) -> str:
    normalized_config = str(config_status or "inconclusive").strip().lower()
    normalized_search = str(search_status or "inconclusive").strip().lower()
    if "failed" in {normalized_config, normalized_search}:
        return "verification_failed"
    if normalized_config == "passed" and normalized_search == "passed":
        return "verified"
    return "verification_inconclusive"


def build_query_demotion_search_effect(
    verification: Dict[str, Any],
    targets: List[str],
    *,
    checked_at: str,
) -> Dict[str, Any]:
    if not targets:
        return {
            "status": "passed",
            "targets": [],
            "observedRemainingIds": [],
            "verifiedBucketIds": [],
            "unverifiedIds": [],
            "remainingIds": [],
            "reason": "no_added_demotion_targets",
            "resultCount": None,
            "resultComplete": None,
            "observedTargetBuckets": {},
            "observedTargetRanks": {},
            "checkedProductCount": 0,
            "checkedAt": checked_at,
            "evidence": {
                "querySucceeded": None,
                "resultComplete": None,
                "resultCount": None,
                "checkedProductIds": [],
                "observedProducts": [],
                "observedTargetBuckets": {},
                "observedTargetRanks": {},
                "failureReason": None,
            },
        }
    after = verification.get("after") if isinstance(verification, dict) else {}
    after = after if isinstance(after, dict) else {}
    query_succeeded = after.get("query_succeeded") is True
    raw_result_complete = after.get("result_complete")
    result_complete_valid = (
        "result_complete" in after
        and isinstance(raw_result_complete, bool)
    )
    result_complete = raw_result_complete if result_complete_valid else False
    raw_result_count = after.get("result_count")
    result_count_valid = (
        isinstance(raw_result_count, int)
        and not isinstance(raw_result_count, bool)
        and raw_result_count >= 0
    )
    result_count = raw_result_count if result_count_valid else None
    raw_failure_reason = after.get("failure_reason")
    failure_reason_valid = (
        raw_failure_reason is None
        or isinstance(raw_failure_reason, str)
    )
    failure_reason = (
        raw_failure_reason.strip()
        if isinstance(raw_failure_reason, str)
        else ""
    )
    raw_checked_product_count = after.get("checked_product_count")
    checked_product_count_valid = (
        isinstance(raw_checked_product_count, int)
        and not isinstance(raw_checked_product_count, bool)
        and raw_checked_product_count >= 0
    )
    checked_product_count = (
        raw_checked_product_count
        if checked_product_count_valid
        else 0
    )

    raw_bucket_observations = after.get("bucket_observations")
    raw_missing_product_ids = after.get("missing_product_ids")
    bucket_evidence_present = (
        "bucket_observations" in after
        or "missing_product_ids" in after
    )
    bucket_evidence_invalid = False
    bucket_observations: Dict[str, Any] = {}
    declared_missing_ids: List[str] = []
    if bucket_evidence_present:
        if not isinstance(raw_bucket_observations, dict) or not isinstance(raw_missing_product_ids, list):
            bucket_evidence_invalid = True
        else:
            bucket_observations = raw_bucket_observations
            for product_id in raw_missing_product_ids:
                if not isinstance(product_id, str) or not product_id.strip():
                    bucket_evidence_invalid = True
                    continue
                normalized_product_id = product_id.strip()
                if (
                    normalized_product_id != product_id
                    or normalized_product_id not in targets
                    or normalized_product_id in declared_missing_ids
                ):
                    bucket_evidence_invalid = True
                    continue
                declared_missing_ids.append(normalized_product_id)
            if any(
                not isinstance(product_id, str) or product_id not in targets
                for product_id in bucket_observations
            ):
                bucket_evidence_invalid = True
    observed_target_buckets: Dict[str, str] = {}
    observed_target_ranks: Dict[str, int] = {}
    for target in targets:
        if target not in bucket_observations:
            continue
        observation = bucket_observations.get(target)
        if not isinstance(observation, dict):
            bucket_evidence_invalid = True
            continue
        observed_product_id = observation.get("product_id")
        bucket = observation.get("bucket")
        normalized_bucket = bucket.strip() if isinstance(bucket, str) else ""
        rank = observation.get("rank")
        if (
            not isinstance(observed_product_id, str)
            or observed_product_id != target
            or not normalized_bucket
            or not isinstance(rank, int)
            or isinstance(rank, bool)
            or rank <= 0
        ):
            bucket_evidence_invalid = True
            continue
        observed_target_buckets[target] = normalized_bucket
        observed_target_ranks[target] = rank

    observed_ids = [
        target for target in targets if target in observed_target_buckets
    ]
    verified_bucket_ids = [
        target
        for target in observed_ids
        if observed_target_buckets[target] in _QUERY_DEMOTION_LOW_WEIGHT_BUCKETS
    ]
    observed_remaining_ids = [
        target for target in observed_ids if target not in verified_bucket_ids
    ]
    missing_ids = [target for target in targets if target not in observed_target_buckets]
    if bucket_evidence_present:
        if query_succeeded and set(declared_missing_ids) != set(missing_ids):
            bucket_evidence_invalid = True
        elif not query_succeeded and any(
            product_id in observed_target_buckets
            for product_id in declared_missing_ids
        ):
            bucket_evidence_invalid = True

    envelope_invalid = False
    if bucket_evidence_present:
        if not result_complete_valid or not failure_reason_valid:
            envelope_invalid = True
        if query_succeeded:
            if not result_count_valid or not checked_product_count_valid:
                envelope_invalid = True
            elif checked_product_count > result_count:
                envelope_invalid = True
            elif result_complete != (checked_product_count == result_count):
                envelope_invalid = True
            if failure_reason:
                envelope_invalid = True
        ranks = list(observed_target_ranks.values())
        if (
            not checked_product_count_valid
            or any(rank > checked_product_count for rank in ranks)
        ):
            envelope_invalid = True
        if len(set(ranks)) != len(ranks):
            envelope_invalid = True

    evidence_invalid = bucket_evidence_invalid or envelope_invalid
    query_failure_has_primary_reason = (
        not query_succeeded
        and failure_reason_valid
        and bool(failure_reason)
    )
    if evidence_invalid:
        status = "inconclusive"
        reason = "invalid_search_bucket_evidence"
    elif observed_remaining_ids:
        status = "failed"
        reason = "target_observed"
    elif len(verified_bucket_ids) == len(targets):
        status = "passed"
        reason = "targets_observed_in_low_weight_bucket"
    elif query_failure_has_primary_reason:
        status = "inconclusive"
        reason = failure_reason
    elif not query_succeeded:
        status = "inconclusive"
        reason = "target_bucket_not_observed"
    elif result_complete:
        status = "passed"
        reason = "targets_absent_from_complete_result"
    else:
        status = "inconclusive"
        reason = failure_reason or "result_incomplete"
    evidence_failure_reason = failure_reason or None
    if evidence_invalid and not query_failure_has_primary_reason:
        evidence_failure_reason = "invalid_search_bucket_evidence"
    return {
        "status": status,
        "targets": list(targets),
        "observedRemainingIds": [] if evidence_invalid else observed_remaining_ids,
        "verifiedBucketIds": [] if evidence_invalid else verified_bucket_ids,
        "unverifiedIds": list(targets) if evidence_invalid else (
            missing_ids if status == "inconclusive" else []
        ),
        "remainingIds": [] if evidence_invalid else observed_remaining_ids,
        "reason": reason,
        "resultCount": result_count,
        "resultComplete": result_complete,
        "observedTargetBuckets": observed_target_buckets,
        "observedTargetRanks": observed_target_ranks,
        "checkedProductCount": checked_product_count,
        "checkedAt": checked_at,
        "evidence": {
            "querySucceeded": query_succeeded,
            "resultComplete": result_complete,
            "resultCount": result_count,
            "checkedProductIds": sorted(observed_target_buckets),
            "observedProducts": [],
            "observedTargetBuckets": observed_target_buckets,
            "observedTargetRanks": observed_target_ranks,
            "failureReason": evidence_failure_reason,
        },
    }


def _parse_verification_datetime(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return parsed


def _verification_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def _verification_lease_token(value: datetime.datetime) -> datetime.datetime:
    normalized = value
    if normalized.tzinfo is not None:
        normalized = normalized.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return normalized.replace(microsecond=0)


def _verification_iso(value: datetime.datetime) -> str:
    normalized = value
    if normalized.tzinfo is not None:
        normalized = normalized.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return f"{normalized.isoformat()}Z"


def initialize_query_demotion_verification_snapshot(
    snapshot: Dict[str, Any],
    *,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    normalized = dict(snapshot) if isinstance(snapshot, dict) else {}
    normalized_now = now or _verification_now()
    if normalized_now.tzinfo is not None:
        normalized_now = normalized_now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    started_at = _parse_verification_datetime(normalized.get("verificationStartedAt"))
    if started_at is None:
        started_at = normalized_now
        normalized["verificationStartedAt"] = _verification_iso(started_at)
    deadline_at = _parse_verification_datetime(normalized.get("verificationDeadlineAt"))
    if deadline_at is None:
        deadline_at = started_at + datetime.timedelta(
            seconds=QUERY_DEMOTION_VERIFICATION_DEADLINE_SECONDS,
        )
        normalized["verificationDeadlineAt"] = _verification_iso(deadline_at)
    attempts = normalized.get("verificationAttempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        normalized["verificationAttempts"] = 0
    consecutive_passes = normalized.get("verificationConsecutivePasses")
    if (
        not isinstance(consecutive_passes, int)
        or isinstance(consecutive_passes, bool)
        or consecutive_passes < 0
    ):
        normalized["verificationConsecutivePasses"] = 0
    return normalized


def _query_demotion_verification_window(
    snapshots: List[Dict[str, Any]],
    now: datetime.datetime,
) -> tuple[datetime.datetime, datetime.datetime, int]:
    started_values = [
        parsed
        for snapshot in snapshots
        if (parsed := _parse_verification_datetime(snapshot.get("verificationStartedAt"))) is not None
    ]
    deadline_values = [
        parsed
        for snapshot in snapshots
        if (parsed := _parse_verification_datetime(snapshot.get("verificationDeadlineAt"))) is not None
    ]
    started_at = min(started_values) if started_values else now
    deadline_at = min(deadline_values) if deadline_values else (
        started_at + datetime.timedelta(seconds=QUERY_DEMOTION_VERIFICATION_DEADLINE_SECONDS)
    )
    attempts = max([
        int(snapshot.get("verificationAttempts") or 0)
        for snapshot in snapshots
        if isinstance(snapshot.get("verificationAttempts"), int)
        and not isinstance(snapshot.get("verificationAttempts"), bool)
    ] or [0]) + 1
    return started_at, deadline_at, attempts


def _query_demotion_group_config_status(snapshots: List[Dict[str, Any]]) -> str:
    statuses = []
    for snapshot in snapshots:
        verification = snapshot.get("configVerification")
        status = (
            str(verification.get("status") or "").strip().lower()
            if isinstance(verification, dict)
            else "inconclusive"
        )
        statuses.append(status if status in {"passed", "failed", "inconclusive"} else "inconclusive")
    if any(status == "failed" for status in statuses):
        return "failed"
    if len(statuses) == len(snapshots) and statuses and all(status == "passed" for status in statuses):
        return "passed"
    return "inconclusive"


def _verification_payload_ids(payload: Dict[str, Any], keys: tuple[str, ...]) -> List[str]:
    normalized: List[str] = []
    for key in keys:
        raw_value = payload.get(key)
        is_array = isinstance(raw_value, list)
        values = raw_value if is_array else [raw_value]
        for value in values:
            parts = [value] if is_array else re.split(r"[;,]", value) if isinstance(value, str) else [value]
            for part in parts:
                product_id = str(part or "").strip()
                if product_id and product_id not in normalized:
                    normalized.append(product_id)
    return normalized


def _query_demotion_payload_deltas(payload: Any) -> tuple[List[str], List[str]]:
    if not isinstance(payload, dict):
        return [], []
    generic_ids = _verification_payload_ids(
        payload,
        ("product_ids", "productIds", "skus", "sku_list", "product_id", "productId", "sku"),
    )
    add_ids = _verification_payload_ids(
        payload,
        ("add_ids", "addIds", "add_product_ids", "addProductIds"),
    )
    remove_ids = _verification_payload_ids(
        payload,
        ("remove_ids", "removeIds", "remove_product_ids", "removeProductIds"),
    )
    if str(payload.get("operation") or "add").strip().lower() == "remove":
        remove_ids.extend(generic_ids)
    else:
        add_ids.extend(generic_ids)
    return sorted(set(add_ids)), sorted(set(remove_ids))


def query_demotion_verification_targets(
    actions: List[TopQueryActionPlan],
) -> tuple[List[str], Optional[str]]:
    complete_added_ids: List[List[str]] = []
    complete_snapshot_count = 0
    for action in actions:
        loaded_snapshot = _safe_json_loads(action.before_snapshot)
        snapshot = loaded_snapshot if isinstance(loaded_snapshot, dict) else {}
        if snapshot.get("complete") is not True:
            continue
        complete_snapshot_count += 1
        added_ids = snapshot.get("addedIds")
        if not isinstance(added_ids, list):
            return [], "query_demotion_before_snapshot_incomplete"
        complete_added_ids.append(sorted({
            str(product_id).strip()
            for product_id in added_ids
            if str(product_id).strip()
        }))
    if complete_snapshot_count:
        if complete_snapshot_count != len(actions):
            return [], "query_demotion_before_snapshot_inconsistent"
        first_added_ids = complete_added_ids[0]
        if any(added_ids != first_added_ids for added_ids in complete_added_ids[1:]):
            return [], "query_demotion_before_snapshot_inconsistent"
        return first_added_ids, None

    add_ids = set()
    remove_ids = set()
    for action in actions:
        action_add_ids, action_remove_ids = _query_demotion_payload_deltas(
            _safe_json_loads(action.request_payload)
        )
        add_ids.update(action_add_ids)
        remove_ids.update(action_remove_ids)
    return sorted(add_ids - remove_ids), None


def query_demotion_verification_group_key(
    action: TopQueryActionPlan,
    snapshot: Optional[Dict[str, Any]] = None,
) -> str:
    if snapshot is None:
        loaded_snapshot = _safe_json_loads(action.after_snapshot)
        snapshot = loaded_snapshot if isinstance(loaded_snapshot, dict) else {}
    group_request_id = str(snapshot.get("groupExternalRequestId") or "").strip()
    if group_request_id:
        return f"request:{group_request_id}"
    status_family = "rollback" if str(action.status or "") == "rollback_pending" else "verification"
    return "|".join([
        str(int(action.task_id)),
        normalize_query_key(action.brand),
        normalize_governance_query_key(action.brand, action.query_text),
        status_family,
    ])


def _authoritative_query_demotion_group_metadata(
    action: TopQueryActionPlan,
) -> Optional[Dict[str, Any]]:
    required_keys = {
        "groupExternalRequestId",
        "leaderActionId",
        "actionIds",
        "brand",
        "normalizedQuery",
        "beforeIds",
        "desiredIds",
        "configVersion",
    }
    normalized_copies = []
    for raw_value in (action.before_snapshot, action.after_snapshot):
        payload = _safe_json_loads(raw_value)
        if not isinstance(payload, dict) or not required_keys.issubset(payload):
            return None
        group_request_id = payload.get("groupExternalRequestId")
        leader_action_id = payload.get("leaderActionId")
        action_ids = payload.get("actionIds")
        brand = payload.get("brand")
        normalized_query = payload.get("normalizedQuery")
        config_version = payload.get("configVersion")
        if (
            not isinstance(group_request_id, str)
            or not group_request_id.strip()
            or not isinstance(leader_action_id, int)
            or isinstance(leader_action_id, bool)
            or not isinstance(action_ids, list)
            or not action_ids
            or any(
                not isinstance(item, int) or isinstance(item, bool) or item <= 0
                for item in action_ids
            )
            or not isinstance(brand, str)
            or not brand.strip()
            or not isinstance(normalized_query, str)
            or not normalized_query.strip()
            or not isinstance(config_version, str)
            or not config_version.strip()
        ):
            return None
        normalized_action_ids = sorted(set(action_ids))
        if leader_action_id not in normalized_action_ids:
            return None
        normalized_lists = {}
        for key in ("beforeIds", "desiredIds"):
            values = payload.get(key)
            if not isinstance(values, list) or any(
                not isinstance(item, (str, int))
                or isinstance(item, bool)
                or not str(item).strip()
                for item in values
            ):
                return None
            normalized_lists[key] = sorted({str(item).strip() for item in values})
        normalized_copies.append({
            "groupExternalRequestId": group_request_id.strip(),
            "leaderActionId": leader_action_id,
            "actionIds": normalized_action_ids,
            "brand": brand.strip(),
            "normalizedQuery": normalized_query.strip(),
            "beforeIds": normalized_lists["beforeIds"],
            "desiredIds": normalized_lists["desiredIds"],
            "configVersion": config_version.strip(),
        })
    if normalized_copies[0] != normalized_copies[1]:
        return None
    return normalized_copies[0]



def _reconcile_query_demotion_verifying_actions(
    task_id: int,
    verification: Dict[str, Any],
    *,
    action_ids: Optional[List[int]] = None,
    expected_lease_expires_at: Optional[datetime.datetime] = None,
    expected_group_key: Optional[str] = None,
) -> None:
    db = SessionLocal()
    try:
        query = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
            TopQueryActionPlan.action_type == "query_demote",
            TopQueryActionPlan.status == "verifying",
        )
        if action_ids is not None:
            query = query.filter(TopQueryActionPlan.id.in_(action_ids))
        actions = query.order_by(TopQueryActionPlan.id.asc()).all()
        actions_by_id = {int(action.id): action for action in actions}
        requested_ids = sorted(set(action_ids)) if action_ids is not None else None
        grouped_actions: Dict[str, List[TopQueryActionPlan]] = {}
        processed_ids = set()
        for seed in actions:
            seed_id = int(seed.id)
            if seed_id in processed_ids or not uses_authoritative_query_demotion(
                _safe_json_loads(seed.request_payload)
            ):
                continue
            metadata = _authoritative_query_demotion_group_metadata(seed)
            if metadata is None:
                processed_ids.add(seed_id)
                continue
            declared_ids = list(metadata["actionIds"])
            processed_ids.update(declared_ids)
            group = [actions_by_id.get(group_id) for group_id in declared_ids]
            group_key = f"request:{metadata['groupExternalRequestId']}"
            if (
                any(action is None for action in group)
                or requested_ids is not None and declared_ids != requested_ids
                or expected_group_key is not None and group_key != expected_group_key
                or int(metadata["leaderActionId"]) not in declared_ids
                or any(
                    str(action.action_type or "") != "query_demote"
                    or str(action.status or "") != "verifying"
                    or not uses_authoritative_query_demotion(
                        _safe_json_loads(action.request_payload)
                    )
                    or _authoritative_query_demotion_group_metadata(action) != metadata
                    or int(action.task_id) != task_id
                    or normalize_query_key(action.brand) != normalize_query_key(metadata["brand"])
                    or normalize_governance_query_key(action.brand, action.query_text)
                    != normalize_governance_query_key(metadata["brand"], metadata["normalizedQuery"])
                    for action in group
                )
            ):
                continue
            grouped_actions[group_key] = group

        for group in grouped_actions.values():
            now = _verification_lease_token(_verification_now())
            group_action_ids = [int(action.id) for action in group]
            claimed_until = (
                _verification_lease_token(expected_lease_expires_at)
                if expected_lease_expires_at is not None
                else None
            )
            if claimed_until is None:
                claimed_until = now + datetime.timedelta(
                    seconds=QUERY_DEMOTION_VERIFICATION_LEASE_SECONDS,
                )
                claimed = db.query(TopQueryActionPlan).filter(
                    TopQueryActionPlan.id.in_(group_action_ids),
                    TopQueryActionPlan.status == "verifying",
                    (
                        TopQueryActionPlan.execution_lease_expires_at.is_(None)
                        | (TopQueryActionPlan.execution_lease_expires_at <= now)
                    ),
                ).update({
                    TopQueryActionPlan.execution_lease_expires_at: claimed_until,
                }, synchronize_session=False)
                if claimed != len(group_action_ids):
                    db.rollback()
                    continue
                db.commit()
                group = db.query(TopQueryActionPlan).filter(
                    TopQueryActionPlan.id.in_(group_action_ids),
                    TopQueryActionPlan.status == "verifying",
                ).order_by(TopQueryActionPlan.id.asc()).all()
                if len(group) != len(group_action_ids):
                    continue

            snapshots = [
                snapshot if isinstance(snapshot, dict) else {}
                for snapshot in (_safe_json_loads(action.after_snapshot) for action in group)
            ]
            targets, target_error = query_demotion_verification_targets(group)
            started_at, deadline_at, attempts = _query_demotion_verification_window(snapshots, now)
            if target_error:
                search_effect = {
                    "status": "inconclusive",
                    "targets": [],
                    "observedRemainingIds": [],
                    "unverifiedIds": [],
                    "remainingIds": [],
                    "reason": target_error,
                    "resultCount": None,
                    "checkedProductCount": 0,
                    "checkedAt": _verification_iso(now),
                    "evidence": {"failureReason": target_error},
                }
            else:
                search_effect = build_query_demotion_search_effect(
                    verification,
                    targets,
                    checked_at=_verification_iso(now),
                )
            config_status = _query_demotion_group_config_status(snapshots)
            terminal_status = resolve_query_demotion_terminal_status(
                config_status,
                search_effect["status"],
            )
            previous_consecutive_passes = min([
                int(snapshot.get("verificationConsecutivePasses") or 0)
                for snapshot in snapshots
                if isinstance(snapshot.get("verificationConsecutivePasses"), int)
                and not isinstance(snapshot.get("verificationConsecutivePasses"), bool)
            ] or [0])
            attempt_passed = terminal_status == "verified"
            consecutive_passes = (
                previous_consecutive_passes + 1
                if attempt_passed
                else 0
            )
            if attempt_passed and consecutive_passes >= 2:
                next_status = "verified"
            elif now < deadline_at:
                next_status = "verifying"
            elif terminal_status == "verified":
                next_status = "verification_inconclusive"
            else:
                next_status = terminal_status
            pending_events = []
            group_write_succeeded = True
            for action, snapshot in zip(group, snapshots):
                updated_snapshot = {
                    **snapshot,
                    "searchEffect": search_effect,
                    "verificationStartedAt": _verification_iso(started_at),
                    "verificationDeadlineAt": _verification_iso(deadline_at),
                    "verificationAttempts": attempts,
                    "verificationConsecutivePasses": consecutive_passes,
                }
                if next_status != "verifying":
                    updated_snapshot["verificationCompletedAt"] = _verification_iso(now)
                    updated_snapshot["verificationResult"] = next_status
                updated = db.query(TopQueryActionPlan).filter(
                    TopQueryActionPlan.id == int(action.id),
                    TopQueryActionPlan.status == "verifying",
                    TopQueryActionPlan.execution_lease_expires_at == claimed_until,
                ).update({
                    TopQueryActionPlan.after_snapshot: json.dumps(updated_snapshot, ensure_ascii=False),
                    TopQueryActionPlan.status: next_status,
                    TopQueryActionPlan.execution_lease_expires_at: None,
                    TopQueryActionPlan.error_message: (
                        str(search_effect.get("reason") or "verification_failed")
                        if next_status == "verification_failed"
                        else None
                    ),
                }, synchronize_session=False)
                if updated != 1:
                    group_write_succeeded = False
                    break
                if next_status == "verifying":
                    continue
                event_type = {
                    "verified": "action_verified",
                    "verification_failed": "action_failed",
                    "verification_inconclusive": "action_inconclusive",
                }[next_status]
                pending_events.append(TopQueryAgentEvent(
                    task_id=task_id,
                    brand=action.brand,
                    query_text=action.query_text,
                    agent_name="VerificationExecutor",
                    stage="verification",
                    event_type=event_type,
                    summary=(
                        "降桶动作搜索验证通过"
                        if next_status == "verified"
                        else "降桶动作搜索验证失败"
                        if next_status == "verification_failed"
                        else "降桶动作搜索验证证据不足"
                    ),
                    output_payload=json.dumps({
                        "actionId": int(action.id),
                        "configStatus": config_status,
                        "searchEffect": search_effect,
                        "result": next_status,
                    }, ensure_ascii=False),
                    duration=0.0,
                    token_usage=0,
                ))
            if not group_write_succeeded:
                db.rollback()
                continue
            db.add_all(pending_events)
            db.commit()
    finally:
        db.close()




def _reconcile_executed_product_actions(
    task_id: int,
    verification: Dict[str, Any],
) -> None:
    _reconcile_query_demotion_verifying_actions(task_id, verification)
    after = verification.get("after") if isinstance(verification, dict) else {}
    products = after.get("products") if isinstance(after, dict) else []
    if not isinstance(products, list):
        return
    query_succeeded = after.get("query_succeeded") is True
    result_complete = after.get("result_complete") is True
    result_count = after.get("result_count")
    if not isinstance(result_count, int) or isinstance(result_count, bool) or result_count < 0:
        result_count = None
    failure_reason = str(after.get("failure_reason") or "").strip()
    if result_count is not None and result_count < len(products):
        query_succeeded = False
        result_complete = False
        result_count = None
        failure_reason = "invalid_search_response"
    elif result_complete and result_count != len(products):
        query_succeeded = False
        result_complete = False
        result_count = None
        failure_reason = "invalid_search_response"
    recalled_ids = {
        product_id
        for product in products
        for product_id in _verification_product_ids(product)
        if product_id
    }
    db = SessionLocal()
    try:
        actions = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
            TopQueryActionPlan.execution_mode == "auto",
            TopQueryActionPlan.action_type.in_(["product_blacklist", "query_demote"]),
            TopQueryActionPlan.status == "executed",
        ).order_by(TopQueryActionPlan.id.asc()).all()
        verified_at = datetime.datetime.now().isoformat()
        for action in actions:
            request_payload = _safe_json_loads(action.request_payload)
            if (
                str(action.action_type or "") == "query_demote"
                and uses_authoritative_query_demotion(request_payload)
            ):
                continue
            action_verification_started_at = time.perf_counter()
            targets = _verification_action_product_ids(request_payload)
            if not targets:
                continue
            observed_remaining_ids = [product_id for product_id in targets if product_id in recalled_ids]
            unseen_target_ids = [product_id for product_id in targets if product_id not in recalled_ids]
            if observed_remaining_ids:
                result = "verification_failed"
                event_type = "action_failed"
                reason = "target_observed"
            elif query_succeeded and result_complete:
                result = "verified"
                event_type = "action_verified"
                reason = "targets_absent_from_complete_result"
            else:
                result = "verification_inconclusive"
                event_type = "action_inconclusive"
                reason = failure_reason or "result_incomplete"
            snapshot = {
                "targets": targets,
                "observedRemainingIds": observed_remaining_ids,
                "unverifiedIds": unseen_target_ids if result == "verification_inconclusive" else [],
                "remainingIds": observed_remaining_ids,
                "result": result,
                "verifiedAt": verified_at,
                "reason": reason,
                "resultCount": result_count,
                "checkedProductCount": len(products),
            }
            claimed = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == int(action.id),
                TopQueryActionPlan.status == "executed",
            ).update({
                TopQueryActionPlan.status: result,
                TopQueryActionPlan.after_snapshot: json.dumps(snapshot, ensure_ascii=False),
            }, synchronize_session=False)
            if claimed != 1:
                continue
            db.add(TopQueryAgentEvent(
                task_id=task_id,
                brand=action.brand,
                query_text=action.query_text,
                agent_name="VerificationExecutor",
                stage="verification",
                event_type=event_type,
                summary=(
                    "动作验证证据不足"
                    if result == "verification_inconclusive"
                    else "动作目标已移出实时召回"
                    if result == "verified"
                    else "动作目标仍存在于实时召回"
                ),
                output_payload=json.dumps({"actionId": int(action.id), **snapshot}, ensure_ascii=False),
                duration=max(time.perf_counter() - action_verification_started_at, 0.0),
                token_usage=0,
            ))
        db.commit()
    finally:
        db.close()


def _verification_action_product_ids(payload: Any) -> List[str]:
    if not isinstance(payload, dict):
        return []
    normalized: List[str] = []
    for key in ("product_ids", "productIds", "skus", "sku_list", "product_id", "productId", "sku"):
        raw_value = payload.get(key)
        is_array = isinstance(raw_value, list)
        values = raw_value if is_array else [raw_value]
        for value in values:
            parts = [value] if is_array else re.split(r"[;,]", value) if isinstance(value, str) else [value]
            for part in parts:
                product_id = str(part or "").strip()
                if product_id and product_id not in normalized:
                    normalized.append(product_id)
    return normalized


def _verification_product_ids(product: Any) -> List[str]:
    if not isinstance(product, dict):
        return []
    normalized: List[str] = []
    for key in ("productId", "product_id", "skuId", "sku_id", "spuId", "spu_id", "spu"):
        product_id = str(product.get(key) or "").strip()
        if product_id and product_id not in normalized:
            normalized.append(product_id)
    return normalized


def _build_context_event_payload(context: Any) -> Dict[str, Any]:
    normalized = context if isinstance(context, dict) else {}
    search = normalized.get("search") if isinstance(normalized.get("search"), dict) else {}
    task = normalized.get("task") if isinstance(normalized.get("task"), dict) else {}
    products = [item for item in (normalized.get("products") or []) if isinstance(item, dict)]
    relevance = (
        normalized.get("relevance_diagnostics")
        if isinstance(normalized.get("relevance_diagnostics"), dict)
        else {}
    )
    open_factors = normalized.get("open_factors") if isinstance(normalized.get("open_factors"), dict) else {}

    metrics = {}
    for output_key, source_keys in {
        "pv": ("search_pv", "pv"),
        "ctr": ("ctr",),
        "cvr": ("cvr",),
        "avgCtr": ("avg_ctr", "avgCtr"),
        "avgCvr": ("avg_cvr", "avgCvr"),
    }.items():
        value = _first_present(task, *source_keys)
        if value is not None:
            metrics[output_key] = value

    product_samples = []
    inventory_rates = []
    for product in products:
        product_samples.append({
            "productId": _first_present(product, "product_id", "productId", "id"),
            "name": _first_present(product, "name", "product_name", "productName"),
            "rank": _first_present(product, "rank", "query_rank", "queryRank"),
        })

        size_inventory = product.get("size_inventory") if isinstance(product.get("size_inventory"), dict) else {}
        product_inventory_rates = [
            rate for rate in (
                _safe_float(product.get("broken_rate")),
                _safe_float(product.get("broken_size_rate")),
                _safe_float(size_inventory.get("broken_rate")),
            )
            if rate is not None
        ]
        if product_inventory_rates:
            inventory_rates.append(max(product_inventory_rates))

    matched_factors = []
    for factor in (open_factors.get("matched_factors") or [])[:10]:
        if isinstance(factor, dict):
            summary = {
                key: _bounded_context_summary_value(factor[key])
                for key in ("type", "reason", "status", "name", "source", "value")
                if _bounded_context_summary_value(factor.get(key)) is not None
            }
            if summary:
                matched_factors.append(summary)
        elif factor is not None:
            matched_factors.append(_bounded_context_summary_value(str(factor)))

    return {
        "schemaVersion": 2,
        "resultCount": _first_present(search, "result_count", "resultCount")
        if search
        else _first_present(normalized, "result_count", "resultCount"),
        "previousResultCount": _first_present(search, "previous_result_count", "previousResultCount")
        if search
        else _first_present(normalized, "previous_result_count", "previousResultCount"),
        "metrics": metrics,
        "productSummary": {"inspectedCount": len(products), "samples": product_samples[:5]},
        "relevanceSummary": {
            "checkedCount": _safe_int(_first_present(relevance, "checked_count", "checkedCount")),
            "mismatchCount": _safe_int(_first_present(relevance, "mismatch_count", "mismatchCount")),
            "checkFailedCount": _safe_int(
                _first_present(relevance, "check_failed_count", "checkFailedCount")
            ),
        },
        "inventorySummary": {
            "affectedCount": sum(1 for rate in inventory_rates if rate > 0),
            "maxBrokenRate": max(inventory_rates) if inventory_rates else None,
        },
        "worldKnowledgeSummary": {
            "status": str(open_factors.get("status") or "not_available"),
            "matchedFactors": matched_factors,
            "suggestedTags": [
                _bounded_context_summary_value(tag)
                for tag in (open_factors.get("suggested_tags") or [])[:10]
                if isinstance(tag, (str, int, float, bool))
            ],
            "season": open_factors.get("season"),
        },
        "downstream": "PlannerAgent",
    }


def _summarize_planner_stages(plan: Any) -> List[Dict[str, Any]]:
    strategy = getattr(plan, "strategy", None)
    stages = getattr(strategy, "stages", None) or []
    strategy_success_criteria = list(getattr(strategy, "success_criteria", None) or [])
    summaries = []
    for stage in stages:
        success_criteria = str(getattr(stage, "exit_condition", "") or "")
        if not success_criteria and len(stages) == 1 and len(strategy_success_criteria) == 1:
            success_criteria = str(strategy_success_criteria[0])
        summaries.append({
            "stageKey": str(getattr(stage, "stage_key", "") or ""),
            "executorKeys": list(getattr(stage, "executor_keys", None) or []),
            "priority": getattr(stage, "priority", 0),
            "reason": str(getattr(stage, "reason", "") or ""),
            "successCriteria": success_criteria,
        })
    return summaries


def _planner_downstream(plan: Any) -> Dict[str, List[str]]:
    executor_name_by_check = {
        "recall": "RecallExecutor",
        "relevance": "RelevanceNodeRetired",
        "open_factor": "OpenFactorExecutor",
    }
    check_keys = []
    strategy = getattr(plan, "strategy", None)
    for stage in (getattr(strategy, "stages", None) or []):
        for check_key in (getattr(stage, "executor_keys", None) or []):
            normalized_key = str(check_key)
            if normalized_key and normalized_key not in check_keys:
                check_keys.append(normalized_key)
    if not check_keys:
        for check in (getattr(plan, "checks", None) or []):
            check_key = str(getattr(check, "check_item", "") or "")
            if check_key and check_key not in check_keys:
                check_keys.append(check_key)
    return {
        "executorNames": [
            executor_name_by_check[check_key]
            for check_key in check_keys
            if check_key in executor_name_by_check
        ],
        "checkKeys": check_keys,
    }


def _first_present(payload: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _token_budget_metrics(token_usage_total: int, token_limit: int) -> Dict[str, Any]:
    return {
        "tokenUsage": 0,
        "tokenLimit": token_limit,
        "cumulativeTokenUsage": token_usage_total,
        "tokenBudgetExhausted": True,
        "fallbackReason": "token_budget_exhausted",
    }


def _token_budget_skipped_executor_result(
    check_item: str,
    token_usage_total: int,
    token_limit: int,
) -> ExecutorResult:
    return ExecutorResult(
        check_item=check_item,
        status="waiting_human_approval",
        root_cause="token_budget_exhausted",
        recommended_action="manual_review",
        evidence={
            "requires_manual_review": True,
            "reason": "token_budget_exhausted",
            "token_usage_total": token_usage_total,
            "token_limit": token_limit,
        },
        expected_impact="Token 预算耗尽，已跳过后续 LLM 判断",
        negative_impact="当前检查项未完成 Agent 判断，需人工复核",
        confidence=0.0,
        token_usage=0,
        llm_metrics=_token_budget_metrics(token_usage_total, token_limit),
    )


def _bounded_context_summary_value(value: Any, max_length: int = 200) -> Any:
    if isinstance(value, str):
        return value[:max_length]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def _sanitize_optional_telemetry_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    return _sanitize_telemetry_string(str(value))


def _executor_action_artifact(action: Any, action_id: Optional[int]) -> Dict[str, Any]:
    payload = action.payload if isinstance(getattr(action, "payload", None), dict) else {}
    product_ids = payload.get("product_ids")
    if not isinstance(product_ids, list):
        product_id = payload.get("product_id")
        product_ids = [product_id] if product_id else []
    return {
        "actionId": action_id,
        "type": str(getattr(action, "action_type", "") or ""),
        "mode": str(getattr(action, "execution_mode", "") or ""),
        "reason": str(getattr(action, "reason", "") or ""),
        "productIds": [str(product_id) for product_id in product_ids if product_id],
        "payloadPreview": _executor_action_payload_preview(payload),
    }


def _executor_action_payload_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    preview_keys = {
        "brand", "query", "keyword", "filters", "configs", "product_id", "product_ids",
        "check_key", "action_type", "params", "actionType", "requires_manual_review",
    }
    return {
        key: value
        for key, value in payload.items()
        if key in preview_keys
    }


def _executor_event_artifact(
    result: ExecutorResult,
    proposed_actions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    llm_metrics = _sanitize_llm_metrics(
        getattr(result, "llm_metrics", {}) or {}
    )
    action_ids = [item["actionId"] for item in proposed_actions if item.get("actionId") is not None]
    payload = {
        "schemaVersion": 2,
        "artifact": {
            "artifactType": "executor_decision",
            "checkKey": result.check_item,
        },
        "checkKey": result.check_item,
        "status": result.status,
        "rootCause": result.root_cause,
        "recommendedAction": result.recommended_action,
        "evidence": result.evidence,
        "conversionHypothesis": result.conversion_hypothesis,
        "expectedImpact": result.expected_impact,
        "negativeImpact": result.negative_impact,
        "confidence": result.confidence,
        "proposedActions": proposed_actions,
        "actionTypes": [item["type"] for item in proposed_actions],
        "downstream": {
            "actionPlanIds": action_ids,
            "kgIntentCount": len(result.kg_action_intents),
            "requiresHumanApproval": any(item.get("mode") == "human_approval" for item in proposed_actions),
            "nextStage": "planner_review" if result.kg_action_intents else "verification",
        },
    }
    if llm_metrics:
        payload["llmMetrics"] = llm_metrics
    return payload


def _kg_diagnostic_reasons(evidence: Any) -> tuple[List[str], List[str]]:
    if not isinstance(evidence, dict):
        return [], []
    missing_reasons: List[str] = []
    skipped_reasons: List[str] = []
    missing_count = int(evidence.get("missing_executable_payload_count") or 0)
    if missing_count:
        missing_reasons.append(f"missing_executable_payload:{missing_count}")
    for diagnostic in evidence.get("diagnostics") or []:
        if not isinstance(diagnostic, dict):
            continue
        for field in diagnostic.get("missingRequiredFields") or []:
            reason = f"missing_required_field:{field}"
            if reason not in missing_reasons:
                missing_reasons.append(reason)
        for reason in diagnostic.get("skippedReasons") or []:
            normalized = str(reason)
            if normalized and normalized not in skipped_reasons:
                skipped_reasons.append(normalized)
    return missing_reasons, skipped_reasons


def _sanitize_telemetry_string(value: str, max_length: int = 500) -> str:
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


def _sanitize_llm_metrics(value: Any) -> Any:
    allowed_keys = {
        "llmused",
        "source",
        "timeoutseconds",
        "phasetimeoutseconds",
        "attempttimeoutseconds",
        "configuredtimeoutseconds",
        "attempttimeoutcapseconds",
        "duration",
        "durationseconds",
        "totaldurationseconds",
        "originalrequestchars",
        "requestchars",
        "requestlimitchars",
        "requestcompacted",
        "responsechars",
        "factpackchars",
        "token",
        "tokens",
        "tokenusage",
        "tokenlimit",
        "cumulativetokenusage",
        "tokenbudgetexhausted",
        "budgetexhausted",
        "totaltokens",
        "inputtokens",
        "outputtokens",
        "prompttokens",
        "completiontokens",
        "model",
        "modelname",
        "modelrole",
        "errortype",
        "postprocessingerrortype",
        "errorcode",
        "fallback",
        "primary",
        "fallbackused",
        "fallbackavailable",
        "fallbackreason",
        "fallbackskippedreason",
        "usedfallbackmodel",
        "attempt",
        "attempttype",
        "attempts",
        "attemptcount",
        "phasemetrics",
        "phase",
        "phasekey",
        "phase1",
        "phase2",
        "structuredoutput",
        "complianceattempts",
        "compliancecorrected",
        "policyfactversion",
        "policyviolationcodes",
        "status",
        "success",
        "compact",
    }
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            normalized_key = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized_key in allowed_keys:
                sanitized[key] = _sanitize_llm_metrics(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_llm_metrics(item) for item in value]
    if isinstance(value, str):
        return _sanitize_telemetry_string(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_telemetry_string(str(value))


def _successful_event_telemetry_string(value: Any) -> Optional[str]:
    sanitized = _sanitize_optional_telemetry_string(value)
    if sanitized is None:
        return None
    return _normalize_nonterminal_provider_timeout_signature(sanitized)


def _successful_event_llm_metrics(value: Any) -> Any:
    return _normalize_successful_event_llm_metrics(_sanitize_llm_metrics(value))


def _normalize_successful_event_llm_metrics(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_successful_event_llm_metrics(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_successful_event_llm_metrics(item) for item in value]
    if isinstance(value, str):
        return _normalize_nonterminal_provider_timeout_signature(value)
    return value


def _normalize_nonterminal_provider_timeout_signature(value: str) -> str:
    return re.sub(
        r"\bAPITimeoutError(?:\s+Bearer\s+\[REDACTED\])?",
        "provider_timeout",
        value,
        flags=re.IGNORECASE,
    )


def _planner_config_from_snapshot(model_config_snapshot: NodeConfigSnapshot) -> ResolvedNodeConfig:
    if not isinstance(model_config_snapshot, NodeConfigSnapshot):
        raise NodeConfigError("planner model config snapshot is invalid")
    try:
        planner_config = model_config_snapshot.nodes["top_query.planner"]
    except (AttributeError, KeyError) as exc:
        raise NodeConfigError("planner model config snapshot is missing top_query.planner") from exc
    if planner_config.channel != "rd_llm":
        raise NodeConfigError("top_query.planner must use rd_llm channel")
    return planner_config


def _default_planner_llm(model_config_snapshot: Optional[NodeConfigSnapshot] = None) -> Any:
    planner_snapshot = model_config_snapshot or capture_snapshot(
        node_keys=("top_query.planner",),
    )
    planner_config = _planner_config_from_snapshot(planner_snapshot)
    if os.getenv("ENV", "test").lower() == "test" and os.getenv("TOP_QUERY_PLANNER_LLM_IN_TEST", "true").lower() == "false":
        return None
    timeout = _planner_timeout_seconds()
    primary = build_rd_chat_model(
        timeout=timeout,
        model_env="",
        default_model=planner_config.primary_model,
        temperature=0.1,
        max_retries=0,
    )
    if primary is None:
        return None
    fallback = None
    if planner_config.fallback_model and planner_config.fallback_model != planner_config.primary_model:
        fallback = build_rd_chat_model(
            timeout=timeout,
            model_env="",
            default_model=planner_config.fallback_model,
            temperature=0.1,
            max_retries=0,
        )
    return FallbackChatModel(primary=primary, fallback=fallback)


def _current_stage_check_items(plan: Any, stage_index: int = 0) -> set:
    stages = _plan_stages(plan)
    if stages:
        safe_index = max(0, min(stage_index, len(stages) - 1))
        return set(stages[safe_index].executor_keys)
    return {check.check_item for check in plan.checks}


def _current_stage_retry_check_items(plan: Any, stage_index: int = 0) -> set:
    stages = _plan_stages(plan)
    if not stages:
        return set()
    safe_index = max(0, min(stage_index, len(stages) - 1))
    return set(getattr(stages[safe_index], "retry_check_items", None) or [])


def _route_planned_work(state: TopQueryWorkflowState) -> str:
    if state.get("budget_stop") or state.get("failure_stop") or state.get("compliance_stop"):
        return "finalize"
    if state.get("retry_node") == "budget_gate":
        return "retry"
    return "execute" if _effective_current_stage_check_items(state) else "evaluate"


def _route_budget_gate(state: TopQueryWorkflowState) -> str:
    return "finalize" if state.get("budget_stop") else "plan"


def _route_after_execution(state: TopQueryWorkflowState) -> str:
    if state.get("budget_stop") or state.get("failure_stop") or state.get("compliance_stop"):
        return "finalize"
    if state.get("retry_node") in {"execute_actions", "verify"}:
        return str(state["retry_node"])
    if state.get("continue_stage"):
        return "execute_actions"
    return "planner_review"


def _is_safe_prewrite_action_failure(exc: Exception) -> bool:
    details = getattr(exc, "details", None)
    return (
        isinstance(details, dict)
        and details.get("write_succeeded") is False
        and details.get("execution_unknown") is not True
    )


def _effective_current_stage_check_items(state: TopQueryWorkflowState) -> set:
    current_stage_index = int(state.get("current_stage_index") or 0)
    check_items = _current_stage_check_items(state.get("plan"), current_stage_index)
    retry_check_items = _current_stage_retry_check_items(state.get("plan"), current_stage_index)
    executed_check_items = set(state.get("executed_check_items") or [])
    return {
        check_item for check_item in check_items
        if check_item not in executed_check_items or check_item in retry_check_items
    }


def _planned_check_items(plan: Any) -> set:
    if plan is None:
        return set()
    planned = {check.check_item for check in plan.checks}
    stages = _plan_stages(plan)
    if stages:
        for stage in stages:
            planned.update(stage.executor_keys)
    return planned


def _plan_stages(plan: Any) -> List[Any]:
    strategy = getattr(plan, "strategy", None)
    stages = getattr(strategy, "stages", None) if strategy else None
    return list(stages or [])


def _has_remaining_stages(state: TopQueryWorkflowState) -> bool:
    stages = _plan_stages(state.get("plan"))
    if not stages:
        return False
    current_stage_index = int(state.get("current_stage_index") or 0)
    return current_stage_index + 1 < len(stages)


def _context_with_recall_suppression(
    state: TopQueryWorkflowState,
    low_result_threshold: int = 4,
    drop_ratio_threshold: float = 0.3,
) -> Dict[str, Any]:
    context = state.get("context") or {}
    open_factor_result = next(
        (
            result for result in reversed(state.get("executor_results") or [])
            if isinstance(result, ExecutorResult) and result.check_item == "open_factor"
        ),
        None,
    )
    decision = _world_knowledge_decision_payload(
        getattr(open_factor_result, "evidence", {}) or {}
    )
    if str(decision.get("decision") or "") != "execute":
        return context
    updated_context = dict(context)
    result_count = _trusted_snapshot_result_count(updated_context)
    if result_count is None:
        updated_context["suppress_recall_check"] = False
        return updated_context
    recall_assessment = build_recall_assessment(
        result_count=result_count,
        low_result_threshold=low_result_threshold,
        drop_ratio_threshold=drop_ratio_threshold,
        seven_day_average=updated_context.get("seven_day_average"),
        history_sample_count=int(updated_context.get("history_sample_count") or 0),
    )
    updated_context["suppress_recall_check"] = recall_assessment["status"] == "healthy"
    return updated_context


def _normalize_disabled_action_result(result: ExecutorResult) -> ExecutorResult:
    if not result.actions:
        return result
    has_non_disabled_action = any(action.execution_mode != "disabled" for action in result.actions)
    if has_non_disabled_action or result.kg_action_intents:
        return result
    result.status = "failed"
    result.recommended_action = "disabled"
    result.evidence = {**(result.evidence or {}), "action_execution_disabled": True}
    result.expected_impact = "检查项或工具已禁止执行，未产生治理动作"
    return result


def _task_actions_payload(task_id: int) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        actions = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
        ).order_by(TopQueryActionPlan.id.asc()).all()
        return [
            {
                "id": action.id,
                "checkKey": action.check_key,
                "toolKey": action.tool_key,
                "actionType": action.action_type,
                "executionMode": action.execution_mode,
                "status": action.status,
                "reason": action.reason,
                "requestPayload": _safe_json_loads(action.request_payload),
                "responsePayload": _safe_json_loads(action.response_payload),
                "errorMessage": action.error_message,
            }
            for action in actions
        ]
    finally:
        db.close()


def _safe_json_loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        import json

        return json.loads(value)
    except Exception:
        return value


def _task_has_pending_manual_actions(task_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
            TopQueryActionPlan.execution_mode == "human_approval",
            TopQueryActionPlan.status.in_(["pending", "waiting_human_approval", "approved"]),
        ).first() is not None
    finally:
        db.close()


def _task_has_unsettled_actions(task_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
            TopQueryActionPlan.status.in_([
                "approved",
                "waiting_human_approval",
                "executing",
                "execution_unknown",
                "reconciling",
                "verifying",
                "rollback_pending",
            ]),
        ).first() is not None
    finally:
        db.close()


def _task_has_any_actions(task_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
        ).first() is not None
    finally:
        db.close()


def _task_inconclusive_action_blockers(task_id: int) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        actions = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
            TopQueryActionPlan.status == "verification_inconclusive",
        ).order_by(TopQueryActionPlan.id.asc()).all()
        blockers = []
        for action in actions:
            snapshot = _safe_json_loads(action.after_snapshot)
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            blockers.append({
                "actionId": int(action.id),
                "reason": str(snapshot.get("reason") or "result_incomplete"),
                "unverifiedIds": list(snapshot.get("unverifiedIds") or []),
            })
        return blockers
    finally:
        db.close()


def _task_has_executed_actions(task_id: int, action_ids: List[int]) -> bool:
    normalized_ids = [int(action_id) for action_id in action_ids if action_id is not None]
    if not normalized_ids:
        return False
    db = SessionLocal()
    try:
        return db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
            TopQueryActionPlan.id.in_(normalized_ids),
            TopQueryActionPlan.status.in_([
                "executed",
                "verified",
                "verification_failed",
                "completed",
            ]),
        ).first() is not None
    finally:
        db.close()


def _action_plan_status(action_id: int) -> str:
    db = SessionLocal()
    try:
        row = db.get(TopQueryActionPlan, action_id)
        return str(row.status or "") if row is not None else ""
    finally:
        db.close()


def _persisted_round_action_fingerprints(
    task_id: int,
    workflow_run_id: Optional[int],
    round_no: int,
) -> set[str]:
    if workflow_run_id is None:
        return set()
    db = SessionLocal()
    try:
        rows = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task_id,
            TopQueryActionPlan.workflow_run_id == workflow_run_id,
            TopQueryActionPlan.round_no == round_no,
        ).all()
        return {
            action_fingerprint(
                str(row.action_type or ""),
                str(row.check_key or ""),
                _safe_json_loads(row.request_payload) or {},
            )
            for row in rows
        }
    finally:
        db.close()


def _results_have_governance_signals(results: List[ExecutorResult]) -> bool:
    for result in results or []:
        if _result_field(result, "actions", []) or _result_field(result, "kg_action_intents", []):
            return True
        if _result_requires_manual_review(result):
            return True
    return False


def _results_require_manual_review(results: List[Any]) -> bool:
    return any(_result_requires_manual_review(result) for result in results or [])


def _display_status_for_step(status: str) -> str:
    if status == "pass":
        return "normal"
    if status == "fixed":
        return "fixed"
    if status == "waiting_human_approval":
        return "waiting_human_approval"
    return "still_problematic" if status in {"failed", "check_failed"} else "normal"


def _step_summary_from_result(result: ExecutorResult) -> str:
    evidence = result.evidence if isinstance(result.evidence, dict) else {}
    return str(result.recommended_action or "")


def _context_from_verification(context: Dict[str, Any], verification: Dict[str, Any]) -> Dict[str, Any]:
    after = verification.get("after") or {}
    updated = dict(context)
    products = _merge_after_products_with_previous_inventory(
        context.get("products") or [],
        after.get("products") or [],
    )
    updated["products"] = products
    updated["relevance_diagnostics"] = after.get("relevance_diagnostics") or {}
    result_count = _trusted_snapshot_result_count(after, success_key="query_succeeded")
    updated["result_count"] = result_count
    updated["result_count_known"] = result_count is not None
    updated.pop("tag_bucket_config", None)
    updated.pop("tagBucketConfig", None)
    updated.pop("open_factor_context", None)
    updated["open_factors"] = {}
    search = dict(updated.get("search") or {})
    search["success"] = after.get("query_succeeded")
    search["result_count"] = result_count
    search["result_count_known"] = result_count is not None
    search["result_complete"] = after.get("result_complete") is True
    search["failure_reason"] = after.get("failure_reason")
    updated["search"] = search
    return updated


def _trusted_snapshot_result_count(
    snapshot: Any,
    *,
    success_key: Optional[str] = None,
) -> Optional[int]:
    if not isinstance(snapshot, dict):
        return None
    if success_key and snapshot.get(success_key) is False:
        return None
    if snapshot.get("result_count_known") is False:
        return None
    result_count = snapshot.get("result_count")
    if (
        not isinstance(result_count, int)
        or isinstance(result_count, bool)
        or result_count < 0
    ):
        return None
    return result_count


def _verification_improved(
    before: Dict[str, Any],
    after: Dict[str, Any],
) -> Optional[bool]:
    comparisons: List[bool] = []
    before_count = _trusted_snapshot_result_count(before)
    after_count = _trusted_snapshot_result_count(after, success_key="query_succeeded")
    if before_count is not None and after_count is not None:
        comparisons.append(after_count > before_count)

    before_diagnostics = before.get("relevance_diagnostics")
    after_diagnostics = after.get("relevance_diagnostics")
    if isinstance(before_diagnostics, dict) and isinstance(after_diagnostics, dict):
        before_checked = before_diagnostics.get("checked_count")
        after_checked = after_diagnostics.get("checked_count")
        if (
            isinstance(before_checked, int)
            and not isinstance(before_checked, bool)
            and before_checked > 0
            and isinstance(after_checked, int)
            and not isinstance(after_checked, bool)
            and after_checked > 0
        ):
            before_mismatch = int(before_diagnostics.get("mismatch_count") or 0)
            after_mismatch = int(after_diagnostics.get("mismatch_count") or 0)
            comparisons.append(after_mismatch < before_mismatch)

    return any(comparisons) if comparisons else None


def _result_field(result: Any, field: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(field, default)
    return getattr(result, field, default)


def _result_requires_manual_review(result: Any) -> bool:
    recommended_action = str(_result_field(result, "recommended_action", "") or "").strip().lower()
    evidence = _result_field(result, "evidence", {}) or {}
    if recommended_action == "manual_review":
        return True
    if isinstance(evidence, Mapping) and evidence.get("requires_manual_review") is True:
        return True
    decision = _world_knowledge_decision_payload(evidence) if isinstance(evidence, Mapping) else {}
    return str(decision.get("decision") or "").strip().lower() == "manual_review"


def _merge_after_products_with_previous_inventory(
    previous_products: List[Dict[str, Any]],
    after_products: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    previous_by_id: Dict[str, Dict[str, Any]] = {}
    for product in previous_products or []:
        product_id = str(
            product.get("productId")
            or product.get("product_id")
            or product.get("skuId")
            or product.get("sku_id")
            or product.get("spu")
            or ""
        ).strip()
        if product_id:
            previous_by_id[product_id] = product

    merged_products: List[Dict[str, Any]] = []
    for product in after_products or []:
        item = dict(product)
        product_id = str(
            item.get("productId")
            or item.get("product_id")
            or item.get("skuId")
            or item.get("sku_id")
            or item.get("spu")
            or ""
        ).strip()
        previous = previous_by_id.get(product_id) or {}
        for key in ["broken_rate", "broken_size_rate", "size_inventory"]:
            if key not in item and key in previous:
                item[key] = previous.get(key)
        merged_products.append(item)
    return merged_products
