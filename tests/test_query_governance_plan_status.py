from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.contracts import QueryGovernancePlan
from src.persistence.models import Base
from src.query_governance.models import QueryGovernanceActionPlan, QueryGovernanceTask
from src.query_governance.read_api import _governance_plans, _plan_status
from src.query_governance.reorder_actions import QueryReorderActionPlanRepository


@pytest.mark.parametrize(
    ("status", "executor_type", "expected_label"),
    [
        ("pending", None, "待执行"),
        ("executing", "ai_auto", "执行中"),
        ("verified", "ai_auto", "AI 已执行"),
        ("verified", "human", "手动已执行"),
        ("failed", "ai_auto", "AI 执行失败"),
        ("failed", "human", "手动执行失败"),
        ("execution_unknown", "ai_auto", "结果待确认"),
        ("rollback_pending", "ai_auto", "回滚中"),
        ("rolled_back", "ai_auto", "已回滚"),
        ("rollback_failed", "ai_auto", "回滚失败"),
        ("rejected", "human", "已取消"),
    ],
)
def test_governance_plan_uses_only_approved_display_statuses(
    status: str,
    executor_type: str | None,
    expected_label: str,
):
    assert _plan_status(status, executor_type=executor_type) == (status, expected_label)


@pytest.mark.parametrize(
    "retired_status",
    [
        "waiting_human_approval",
        "pending_confirmation",
        "approved",
        "running",
        "executed",
        "completed",
        "success",
        "error",
        "verification_failed",
    ],
)
def test_governance_plan_does_not_translate_retired_status_aliases(retired_status: str):
    with pytest.raises(ValueError, match="unsupported governance plan status"):
        _plan_status(retired_status, executor_type="ai_auto")


@pytest.mark.parametrize("status", ["verified", "failed"])
def test_terminal_governance_plan_requires_actual_executor(status: str):
    with pytest.raises(ValueError, match="requires executor_type"):
        _plan_status(status)


def test_governance_plan_persists_and_projects_actual_executor_provenance():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    executed_at = datetime(2026, 9, 17, 10, 30, 0)
    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="batch-1",
                brand="atlas",
                data_date=datetime(2026, 9, 17).date(),
                query_text="连衣裙",
                status="completed",
                s0_snapshot=json.dumps({"products": []}),
            )
            session.add(task)
            session.flush()
            action = QueryGovernanceActionPlan(
                task_id=task.id,
                batch_id=task.batch_id,
                brand=task.brand,
                query_text=task.query_text,
                node_key="reorder",
                action_type="query_reorder",
                idempotency_key="plan-1",
                status="verified",
                request_json=json.dumps({"execution_mode": "auto"}),
                response_json=json.dumps({"verified": True}),
                executor_type="ai_auto",
                executor_user_id=None,
                executed_at=executed_at,
                failure_stage=None,
            )
            session.add(action)
            session.commit()
            session.refresh(action)

            plans = _governance_plans(task, [], [action], [])

        assert plans[0]["status"] == "verified"
        assert plans[0]["statusLabel"] == "AI 已执行"
        assert plans[0]["executorType"] == "ai_auto"
        assert plans[0]["executorUserId"] is None
        assert plans[0]["executedAt"] == "2026-09-17T10:30:00"
        assert plans[0]["failureStage"] is None

        contract = QueryGovernancePlan.model_validate(plans[0])
        assert contract.executorType == "ai_auto"
        assert contract.executedAt == "2026-09-17T10:30:00"
    finally:
        engine.dispose()


def test_governance_plan_failure_uses_actual_human_executor():
    task = SimpleNamespace(id=1, s0_snapshot=json.dumps({"products": []}))
    action = SimpleNamespace(
        id=2,
        action_type="query_demote",
        status="failed",
        error_code="ACTION_EXECUTION_FAILED",
        error_message="降桶执行失败",
        request_json=json.dumps({"execution_mode": "human_approval"}),
        response_json=json.dumps({}),
        executor_type="human",
        executor_user_id="ops-user",
        executed_at=datetime(2026, 9, 17, 11, 0, 0),
        failure_stage="execution",
    )

    plan = _governance_plans(task, [], [action], [])[0]

    assert plan["statusLabel"] == "手动执行失败"
    assert plan["executorType"] == "human"
    assert plan["executorUserId"] == "ops-user"
    assert plan["failureStage"] == "execution"


def test_action_plan_repository_records_actual_human_executor():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="batch-human",
                brand="atlas",
                data_date=datetime(2026, 9, 17).date(),
                query_text="风衣",
                status="completed",
            )
            session.add(task)
            session.flush()
            action = QueryGovernanceActionPlan(
                task_id=task.id,
                batch_id=task.batch_id,
                brand=task.brand,
                query_text=task.query_text,
                node_key="reorder",
                action_type="query_reorder",
                idempotency_key="manual-plan-1",
                status="pending",
                request_json=json.dumps({"execution_mode": "human_approval"}),
            )
            session.add(action)
            session.commit()
            action_id = action.id

        QueryReorderActionPlanRepository(factory).record_result(
            action_id,
            status="verified",
            response={"verified": True},
            executor_type="human",
            executor_user_id="ops-user",
        )

        with factory() as session:
            action = session.get(QueryGovernanceActionPlan, action_id)
            assert action is not None
            assert action.status == "verified"
            assert action.executor_type == "human"
            assert action.executor_user_id == "ops-user"
            assert action.executed_at is not None
    finally:
        engine.dispose()
