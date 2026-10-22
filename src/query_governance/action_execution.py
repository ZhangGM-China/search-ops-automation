"""Unified execution, authoritative Mixed readback, and rollback boundary."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .models import ActionRecoveryResult, NodeResult, NodeStatus, QuerySpec
from .ports import SearchGateway
from .reorder_actions import QueryReorderActionGateway


class ActionExecutor(Protocol):
    def execute(
        self, spec: QuerySpec, action: Any, context: Mapping[str, Any]
    ) -> Any:
        """Perform one idempotent external write."""

    def rollback(
        self,
        spec: QuerySpec,
        action: Any,
        execution: Any,
        context: Mapping[str, Any],
    ) -> Any:
        """Restore the state captured before the write."""


class ActionVerifier(Protocol):
    def __call__(
        self,
        spec: QuerySpec,
        action: Any,
        products: list[Mapping[str, Any]],
        execution: Any,
    ) -> Mapping[str, Any]:
        """Validate the authoritative Mixed result against the action."""


class VerifiedActionGateway:
    """Execute an action once, verify Mixed, and fail closed with rollback."""

    def __init__(
        self,
        *,
        search: SearchGateway,
        executor: ActionExecutor,
        verifier: ActionVerifier,
    ) -> None:
        self.search = search
        self.executor = executor
        self.verifier = verifier

    def execute_and_verify(
        self,
        spec: QuerySpec,
        action: Any,
        context: Mapping[str, Any],
    ) -> NodeResult:
        execution: Any = None
        try:
            execution = self.executor.execute(spec, action, context)
        except Exception as exc:
            return self._failure_after_write(
                spec,
                action,
                execution,
                context,
                error_code="ACTION_EXECUTION_FAILED",
                error_message=self._error_text(exc),
            )

        try:
            products = list(self.search.mixed(spec.brand, spec.query))
        except Exception as exc:
            return self._failure_after_write(
                spec,
                action,
                execution,
                context,
                error_code="ACTION_READBACK_FAILED",
                error_message=self._error_text(exc),
                readback_error=self._error_text(exc),
            )

        try:
            verification = dict(
                self.verifier(spec, action, products, execution)
            )
        except Exception as exc:
            return self._failure_after_write(
                spec,
                action,
                execution,
                context,
                error_code="ACTION_VERIFICATION_FAILED",
                error_message=self._error_text(exc),
                readback_error=self._error_text(exc),
            )
        if verification.get("matched") is not True:
            return self._failure_after_write(
                spec,
                action,
                execution,
                context,
                error_code="ACTION_VERIFICATION_FAILED",
                error_message="Mixed 回读结果与期望排序不一致",
                verification=verification,
            )

        return NodeResult.executed(
            {
                "execution": execution,
                "verification": verification,
                "rollback": {"status": "not_required"},
            },
            action=action,
        )

    def recover_interrupted(
        self,
        spec: QuerySpec,
        node: str,
        action: Any,
        context: Mapping[str, Any],
    ) -> ActionRecoveryResult:
        del node, context
        try:
            products = list(self.search.mixed(spec.brand, spec.query))
            verification = dict(self.verifier(spec, action, products, None))
        except Exception as exc:
            return ActionRecoveryResult.unresolved(
                "ACTION_RECOVERY_READBACK_FAILED", self._error_text(exc)
            )

        if verification.get("matched") is True:
            return ActionRecoveryResult.completed(
                NodeResult.executed(
                    {
                        "execution": None,
                        "verification": verification,
                        "rollback": {"status": "not_required"},
                        "recovered": True,
                    },
                    action=action,
                )
            )
        if verification.get("safe_to_retry") is True:
            return ActionRecoveryResult.safe_to_retry()
        return ActionRecoveryResult.unresolved(
            "ACTION_RESULT_UNKNOWN",
            "Mixed 回读无法确认中断动作是否完整生效，未重复执行",
        )

    def _failure_after_write(
        self,
        spec: QuerySpec,
        action: Any,
        execution: Any,
        context: Mapping[str, Any],
        *,
        error_code: str,
        error_message: str,
        verification: Mapping[str, Any] | None = None,
        readback_error: str | None = None,
    ) -> NodeResult:
        evidence: dict[str, Any] = {"execution": execution}
        if verification is not None:
            evidence["verification"] = dict(verification)
        if readback_error is not None:
            evidence["readback_error"] = readback_error
        try:
            rollback = self.executor.rollback(
                spec, action, execution, context
            )
        except Exception as exc:
            rollback_evidence = {
                "status": "error",
                "error": self._error_text(exc),
            }
            evidence["rollback"] = rollback_evidence
            return NodeResult(
                status=NodeStatus.ERROR,
                result=evidence,
                error_code="ACTION_ROLLBACK_FAILED",
                error_message=f"{error_message}；回滚失败：{self._error_text(exc)}",
                action=action,
            )

        evidence["rollback"] = {"status": "executed", "result": rollback}
        return NodeResult(
            status=NodeStatus.ERROR,
            result=evidence,
            error_code=error_code,
            error_message=error_message,
            action=action,
        )

    @staticmethod
    def _error_text(error: Exception) -> str:
        text = str(error).strip()
        return text or error.__class__.__name__


class RoutingActionGateway:
    """Route deterministic actions to their node-specific durable gateways."""

    def __init__(self, gateways: Mapping[str, Any]) -> None:
        self.gateways = dict(gateways)

    def execute_and_verify(
        self, spec: QuerySpec, action: Any, context: Mapping[str, Any]
    ) -> NodeResult:
        action_type = str(action.get("type") or "") if isinstance(action, Mapping) else ""
        gateway = self.gateways.get(action_type)
        if gateway is None:
            return NodeResult.error(
                "ACTION_TYPE_UNSUPPORTED", f"不支持的治理动作类型: {action_type or 'unknown'}"
            )
        return gateway.execute_and_verify(spec, action, context)

    def defer(self, spec: QuerySpec, action: Any, context: Mapping[str, Any]) -> NodeResult:
        action_type = str(action.get("type") or "") if isinstance(action, Mapping) else ""
        gateway = self.gateways.get(action_type)
        defer = getattr(gateway, "defer", None) if gateway is not None else None
        if not callable(defer):
            return NodeResult.error(
                "ACTION_DEFER_UNSUPPORTED", f"不支持待确认治理动作类型: {action_type or 'unknown'}"
            )
        return defer(spec, action, context)

    def recover_interrupted(
        self, spec: QuerySpec, node: str, action: Any, context: Mapping[str, Any]
    ) -> ActionRecoveryResult:
        action_type = str(action.get("type") or "") if isinstance(action, Mapping) else ""
        gateway = self.gateways.get(action_type)
        if gateway is None or not hasattr(gateway, "recover_interrupted"):
            return ActionRecoveryResult.unresolved(
                "ACTION_RECOVERY_UNAVAILABLE", f"不支持恢复治理动作类型: {action_type or 'unknown'}"
            )
        return gateway.recover_interrupted(spec, node, action, context)

__all__ = [
    "ActionExecutor",
    "ActionVerifier",
    "QueryReorderActionGateway",
    "VerifiedActionGateway",
    "RoutingActionGateway",
]
