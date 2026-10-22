from __future__ import annotations

from dataclasses import dataclass, field

from src.query_governance.action_execution import VerifiedActionGateway
from src.query_governance.reorder_actions import QueryReorderActionVerifier
from src.query_governance.models import (
    ActionRecoveryStatus,
    NodeStatus,
    QuerySpec,
)


@dataclass
class _Search:
    products: list[dict] = field(
        default_factory=lambda: [
            {"product_id": "P-2", "rank": 1},
            {"product_id": "P-1", "rank": 2},
        ]
    )
    error: Exception | None = None
    events: list[str] = field(default_factory=list)

    def mixed(self, brand, query):
        self.events.append("mixed")
        if self.error is not None:
            raise self.error
        return self.products


@dataclass
class _Executor:
    events: list[str]
    rollback_error: Exception | None = None

    def execute(self, spec, action, context):
        self.events.append("execute")
        return {"request_id": "req-1", "written": True}

    def rollback(self, spec, action, execution, context):
        self.events.append("rollback")
        if self.rollback_error is not None:
            raise self.rollback_error
        return {"restored": True}


def _verifier(events):
    def verify(spec, action, products, execution):
        events.append("verify")
        actual = [item["product_id"] for item in products]
        expected = action["expected_product_ids"]
        return {
            "matched": actual[: len(expected)] == expected,
            "expected_product_ids": expected,
            "actual_product_ids": actual,
        }

    return verify


def _action():
    return {
        "type": "query_reorder",
        "expected_product_ids": ["P-2", "P-1"],
        "rollback_product_ids": ["P-1", "P-2"],
    }


def test_verified_action_gateway_executes_then_reads_mixed_and_verifies():
    events: list[str] = []
    search = _Search(events=events)
    gateway = VerifiedActionGateway(
        search=search,
        executor=_Executor(events),
        verifier=_verifier(events),
    )

    result = gateway.execute_and_verify(
        QuerySpec("q1", "atlas", "裙子"), _action(), {}
    )

    assert result.status is NodeStatus.EXECUTED
    assert events == ["execute", "mixed", "verify"]
    assert result.result == {
        "execution": {"request_id": "req-1", "written": True},
        "verification": {
            "matched": True,
            "expected_product_ids": ["P-2", "P-1"],
            "actual_product_ids": ["P-2", "P-1"],
        },
        "rollback": {"status": "not_required"},
    }


def test_verified_action_gateway_rolls_back_mixed_mismatch_and_records_evidence():
    events: list[str] = []
    search = _Search(
        products=[
            {"product_id": "P-1", "rank": 1},
            {"product_id": "P-2", "rank": 2},
        ],
        events=events,
    )
    gateway = VerifiedActionGateway(
        search=search,
        executor=_Executor(events),
        verifier=_verifier(events),
    )

    result = gateway.execute_and_verify(
        QuerySpec("q1", "atlas", "裙子"), _action(), {}
    )

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "ACTION_VERIFICATION_FAILED"
    assert events == ["execute", "mixed", "verify", "rollback"]
    assert result.result["verification"]["matched"] is False
    assert result.result["rollback"] == {
        "status": "executed",
        "result": {"restored": True},
    }


def test_verified_action_gateway_rolls_back_when_mixed_readback_fails():
    events: list[str] = []
    search = _Search(error=RuntimeError("Mixed unavailable"), events=events)
    gateway = VerifiedActionGateway(
        search=search,
        executor=_Executor(events),
        verifier=_verifier(events),
    )

    result = gateway.execute_and_verify(
        QuerySpec("q1", "atlas", "裙子"), _action(), {}
    )

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "ACTION_READBACK_FAILED"
    assert events == ["execute", "mixed", "rollback"]
    assert result.result["rollback"]["status"] == "executed"


def test_verified_action_gateway_exposes_rollback_failure():
    events: list[str] = []
    search = _Search(error=RuntimeError("Mixed unavailable"), events=events)
    gateway = VerifiedActionGateway(
        search=search,
        executor=_Executor(events, rollback_error=RuntimeError("restore failed")),
        verifier=_verifier(events),
    )

    result = gateway.execute_and_verify(
        QuerySpec("q1", "atlas", "裙子"), _action(), {}
    )

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "ACTION_ROLLBACK_FAILED"
    assert result.result["rollback"] == {
        "status": "error",
        "error": "restore failed",
    }


def test_verified_action_gateway_recovers_interrupted_action_from_mixed():
    events: list[str] = []
    search = _Search(events=events)
    gateway = VerifiedActionGateway(
        search=search,
        executor=_Executor(events),
        verifier=_verifier(events),
    )

    recovered = gateway.recover_interrupted(
        QuerySpec("q1", "atlas", "裙子"),
        "reorder",
        _action(),
        {},
    )

    assert recovered.status is ActionRecoveryStatus.COMPLETED
    assert recovered.result is not None
    assert recovered.result.action == _action()
    assert events == ["mixed", "verify"]


def test_reorder_verifier_rejects_when_low_stock_demotes_every_target_position():
    verifier = QueryReorderActionVerifier()
    action = {
        "expected_product_ids": ["P-LOW", "P-OK"],
        "expected_products": [{"product_id": "P-LOW", "stock": 3}, {"product_id": "P-OK", "stock": 20}],
    }

    result = verifier(
        QuerySpec("q1", "atlas", "裙子"),
        action,
        [{"product_id": "P-OK", "stock": 20}, {"product_id": "P-LOW", "stock": 3}],
        None,
    )

    assert result["matched"] is False
    assert result["partial"] is False
    assert result["matched_count"] == 0
    assert result["unmatched_positions"] == [1, 2]


def test_reorder_verifier_keeps_unknown_stock_mismatch_as_failure():
    verifier = QueryReorderActionVerifier()
    result = verifier(
        QuerySpec("q1", "atlas", "裙子"),
        {"expected_product_ids": ["P-1"]},
        [{"product_id": "P-2", "stock": 20}],
        None,
    )

    assert result["matched"] is False
    assert "normalization_reason" not in result


def test_reorder_verifier_does_not_hide_unrelated_product_mismatch():
    verifier = QueryReorderActionVerifier()
    result = verifier(
        QuerySpec("q1", "atlas", "裙子"),
        {
            "expected_product_ids": ["P-LOW", "P-OK"],
            "expected_products": [
                {"product_id": "P-LOW", "stock": 3},
                {"product_id": "P-OK", "stock": 20},
            ],
        },
        [
            {"product_id": "P-OTHER", "stock": 20},
            {"product_id": "P-LOW", "stock": 3},
            {"product_id": "P-OK", "stock": 20},
        ],
        None,
    )

    assert result["matched"] is False
