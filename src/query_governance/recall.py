"""Deterministic recall-result checks for the Query governance rewrite.

The recall node only evaluates the immutable S0 evidence and an injected
historical baseline.  It never calls an LLM or writes to the knowledge graph;
an anomaly is represented by a durable KG Agent task submitted through the
async-task service.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Mapping, Protocol, Sequence

from sqlalchemy import select

from src.config import get_brand_business_config

from .async_tasks import AsyncTaskStatus
from .models import NodeResult, QueryGovernanceTask, QuerySpec


RECALL_SCORE_VERSION = "v4_previous_day"


@dataclass(frozen=True)
class RecallHistoryBaseline:
    """Historical result-count evidence used by the deterministic rules."""

    previous_result_count: int | None = None
    seven_day_average: float | None = None
    history_sample_count: int = 0
    result_count_history: tuple[int, ...] = ()
    previous_snapshot: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.previous_result_count is not None and self.previous_result_count < 0:
            raise ValueError("previous_result_count cannot be negative")
        if self.seven_day_average is not None and self.seven_day_average < 0:
            raise ValueError("seven_day_average cannot be negative")
        if self.history_sample_count < 0:
            raise ValueError("history_sample_count cannot be negative")
        if any(int(value) < 0 for value in self.result_count_history):
            raise ValueError("result_count_history cannot contain negative values")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "RecallHistoryBaseline":
        if value is None:
            return cls()
        history = value.get("result_count_history") or ()
        if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
            history = ()
        normalized = tuple(max(int(item or 0), 0) for item in history)
        average = value.get("seven_day_average")
        previous = value.get("previous_result_count")
        return cls(
            previous_result_count=(int(previous) if previous is not None else None),
            seven_day_average=(float(average) if average is not None else None),
            history_sample_count=max(int(value.get("history_sample_count") or len(normalized)), 0),
            result_count_history=normalized,
            previous_snapshot=value.get("previous_snapshot"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "previous_result_count": self.previous_result_count,
            "seven_day_average": self.seven_day_average,
            "history_sample_count": self.history_sample_count,
            "result_count_history": list(self.result_count_history),
            "previous_snapshot": self.previous_snapshot,
        }


class RecallHistoryGateway(Protocol):
    def get_baseline(
        self,
        spec: QuerySpec,
        *,
        data_date: date | None = None,
        batch_id: str | None = None,
    ) -> RecallHistoryBaseline:
        """Return the same seven-day result-count baseline as the old rules."""


class SqlAlchemyRecallHistoryGateway:
    """Read historical S0 result counts from the new Query tables.

    This is an adapter, not part of the Agent Loop.  It keeps the old
    date-deduplication and seven-sample calculation while using the rewrite's
    durable task table, so the node does not depend on ``TopQueryTask``.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        task_model: type[Any] = QueryGovernanceTask,
    ) -> None:
        self._session_factory = session_factory
        self._task_model = task_model

    def get_baseline(
        self,
        spec: QuerySpec,
        *,
        data_date: date | None = None,
        batch_id: str | None = None,
    ) -> RecallHistoryBaseline:
        with self._session_factory() as session:
            query = select(self._task_model).where(
                self._task_model.brand == spec.brand,
                self._task_model.query_text == spec.query,
            )
            if data_date is not None:
                query = query.where(self._task_model.data_date < data_date)
            if batch_id:
                query = query.where(self._task_model.batch_id != batch_id)
            rows = session.execute(
                query.order_by(
                    self._task_model.data_date.desc(),
                    self._task_model.id.desc(),
                )
            ).scalars().all()

        history: list[int] = []
        seen_dates: set[object] = set()
        for row in rows:
            snapshot = self._decode_snapshot(row.s0_snapshot)
            if snapshot is None:
                continue
            count = snapshot.get("result_count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                continue
            date_key: object = row.data_date or f"id:{row.id}"
            if date_key in seen_dates:
                continue
            seen_dates.add(date_key)
            history.append(count)
            if len(history) >= 7:
                break

        # A comparison is valid only when the immediately preceding calendar
        # day has a persisted snapshot. Older rows may remain useful as
        # diagnostic history, but must never influence the conclusion.
        previous_count = None
        previous_snapshot = None
        if data_date is not None:
            previous_date = data_date - timedelta(days=1)
            for row in rows:
                if row.data_date != previous_date:
                    continue
                snapshot = self._decode_snapshot(row.s0_snapshot)
                count = snapshot.get("result_count") if snapshot is not None else None
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    previous_count = count
                    previous_snapshot = snapshot
                    break
        elif history:
            previous_count = history[0]

        average = round(sum(history) / len(history), 2) if history else None
        return RecallHistoryBaseline(
            previous_result_count=previous_count,
            seven_day_average=average,
            history_sample_count=len(history),
            result_count_history=tuple(history),
            previous_snapshot=previous_snapshot,
        )

    @staticmethod
    def _decode_snapshot(value: Any) -> Mapping[str, Any] | None:
        if value is None:
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None


def build_recall_assessment(
    *,
    result_count: int,
    low_result_threshold: int,
    drop_ratio_threshold: float,
    previous_result_count: int | None = None,
    seven_day_average: float | None = None,
    history_sample_count: int = 0,
) -> dict[str, Any]:
    """Assess recall supply against the immediately preceding day's snapshot."""

    current_count = max(int(result_count or 0), 0)
    low_threshold = max(int(low_result_threshold or 0), 1)
    drop_threshold = max(float(drop_ratio_threshold or 0), 0.0)
    previous_count = (
        max(int(previous_result_count), 0)
        if previous_result_count is not None
        else None
    )

    if current_count == 0:
        supply_score = 0
        supply_rule = "当前无召回结果，绝对供给不得分"
    elif current_count < low_threshold:
        supply_score = 9
        supply_rule = f"当前召回少于{low_threshold}件，绝对供给得9分"
    else:
        supply_score = 18
        supply_rule = f"当前召回达到{low_threshold}件，绝对供给得满分"

    drop_ratio: float | None = None
    comparison_status = "not_comparable"
    if supply_score < 18:
        stability_score = 0
        stability_rule = "绝对供给不足，不计算历史稳定性得分"
    elif previous_count is None:
        stability_score = 12
        stability_rule = "无前一日召回数，不判断召回数量骤降"
    elif previous_count <= 0:
        stability_score = 12
        stability_rule = "前一日无召回结果，不判断召回数量骤降"
    else:
        drop_ratio = round(max((previous_count - current_count) / previous_count, 0.0), 4)
        if drop_ratio <= drop_threshold:
            stability_score = 12
            comparison_status = "normal"
            stability_rule = f"较前一日下降比例未超过{drop_threshold:.0%}，历史稳定性得满分"
        elif drop_ratio <= drop_threshold + 0.2:
            stability_score = 8
            comparison_status = "drop"
            stability_rule = f"较前一日下降比例超过{drop_threshold:.0%}不超过20个百分点，扣4分"
        elif drop_ratio <= drop_threshold + 0.4:
            stability_score = 4
            comparison_status = "drop"
            stability_rule = f"较前一日下降比例超过{drop_threshold:.0%}至{drop_threshold + 0.4:.0%}，扣8分"
        else:
            stability_score = 0
            comparison_status = "drop"
            stability_rule = f"较前一日下降比例超过{drop_threshold + 0.4:.0%}，扣12分"

    reasons: list[str] = []
    if current_count == 0:
        reasons.append("no_result")
    elif current_count < low_threshold:
        reasons.append("low_result")
    if drop_ratio is not None and drop_ratio > drop_threshold:
        reasons.append("result_count_drop")

    final_score = supply_score + stability_score
    status = "needs_attention" if reasons else "healthy"
    if current_count >= low_threshold and "result_count_drop" in reasons:
        summary = f"本次召回 {current_count} 件，较前一日召回数 {previous_count} 件明显下降，需要交由 KG Agent 生成治理策略。"
    elif current_count >= low_threshold:
        summary = f"本次召回 {current_count} 件，数量充足，无需调整。"
    elif current_count == 0:
        summary = "本次未召回商品，需要交由 KG Agent 生成治理策略。"
    else:
        summary = f"本次召回 {current_count} 件，数量不足，需要交由 KG Agent 生成治理策略。"

    rationale = summary

    trace = {
        "version": RECALL_SCORE_VERSION,
        "source": "rule",
        "rationale": rationale,
        "finalScore": final_score,
        "maxScore": 30,
        "items": [
            {
                "key": "absolute_supply",
                "label": "绝对供给",
                "score": supply_score,
                "maxScore": 18,
                "deduction": 18 - supply_score,
                "rule": supply_rule,
                "evidence": [
                    {"label": "当前召回商品数", "value": current_count},
                    {"label": "数量判断基线", "value": low_threshold},
                ],
            },
            {
                "key": "historical_stability",
                "label": "历史稳定性",
                "score": stability_score,
                "maxScore": 12,
                "deduction": 12 - stability_score,
                "rule": stability_rule,
                "evidence": [
                    {
                        "label": "往期召回数",
                        "value": previous_count if previous_count is not None else "暂无",
                    },
                    {
                        "label": "下降比例",
                        "value": f"{drop_ratio:.1%}" if drop_ratio is not None else "未计算",
                    },
                    {"label": "骤降判断比例", "value": f"{drop_threshold:.0%}"},
                ],
            },
        ],
    }
    return {
        "score": final_score,
        "status": status,
        "summary": summary,
        "conclusion": (
            "结果少" if current_count < low_threshold
            else "结果骤降" if "result_count_drop" in reasons
            else "正常"
        ),
        "rationale": rationale,
        "reasons": reasons,
        "drop_ratio": drop_ratio,
        "comparison_status": comparison_status,
        "scoring_trace": trace,
    }


class RecallCheckRunner:
    """Run deterministic recall checks and enqueue KG work on anomalies."""

    def __init__(
        self,
        *,
        low_result_threshold: int | None = None,
        result_drop_threshold: float | None = None,
        config_provider: Callable[[str], Mapping[str, Any]] = get_brand_business_config,
        drop_assessor: Any | None = None,
        recall_rate_evaluator: Any | None = None,
    ) -> None:
        self.low_result_threshold = low_result_threshold
        self.result_drop_threshold = result_drop_threshold
        self.config_provider = config_provider
        self.drop_assessor = drop_assessor
        self.recall_rate_evaluator = recall_rate_evaluator

    def run(self, spec: QuerySpec, context: Mapping[str, Any]) -> NodeResult:
        s0 = context.get("s0")
        if not isinstance(s0, Mapping):
            return NodeResult.error("RECALL_EVIDENCE_UNAVAILABLE", "S0 召回证据不存在")
        request_status = s0.get("request_status", "success")
        result_complete = s0.get("result_complete", True)
        result_count_known = s0.get("result_count_known")
        result_count = s0.get("result_count")
        if request_status != "success" or (
            result_complete is False and result_count_known is not True
        ):
            return NodeResult.error(
                "RECALL_EVIDENCE_UNAVAILABLE",
                "S0 召回结果不完整，无法检查召回数量",
            )
        if not isinstance(result_count, int) or isinstance(result_count, bool) or result_count < 0:
            return NodeResult.error("RECALL_EVIDENCE_UNAVAILABLE", "S0 缺少有效召回数量")

        history_error = context.get("history_error")
        if history_error:
            return NodeResult.error("RECALL_HISTORY_FAILED", str(history_error))
        baseline = context.get("history_baseline")
        if isinstance(baseline, Mapping):
            baseline = RecallHistoryBaseline.from_mapping(baseline)
        elif not isinstance(baseline, RecallHistoryBaseline):
            baseline = RecallHistoryBaseline()

        evaluator = context.get("recall_rate_evaluator") or self.recall_rate_evaluator
        recall_rate_report: dict[str, Any] | None = None
        recall_rate_note: str | None = None
        recall_rate_config = self.config_provider(spec.brand) or {}
        recall_rate_enabled = recall_rate_config.get("query_governance_recall_rate_enabled") is True
        if not recall_rate_enabled:
            recall_rate_note = "品牌未启用AI召回率检测"
        elif evaluator is None:
            recall_rate_note = "AI召回率检测已启用但目录数据源未配置"
        if evaluator is not None and recall_rate_enabled:
            try:
                recall_rate_report = dict(evaluator.evaluate(spec, context))
            except Exception as exc:
                return NodeResult.error(
                    "RECALL_RATE_CHECK_FAILED",
                    str(exc).strip() or exc.__class__.__name__,
                )
            post_count = recall_rate_report.get("post_result_count")
            if isinstance(post_count, int) and not isinstance(post_count, bool) and post_count >= 0:
                result_count = post_count

        config = recall_rate_config
        low_threshold = self.low_result_threshold
        if low_threshold is None:
            low_threshold = int(config.get("top_query_low_result_threshold", 5))
        drop_threshold = self.result_drop_threshold
        if drop_threshold is None:
            drop_threshold = float(config.get("top_query_result_drop_threshold", 0.3))
        assessment = build_recall_assessment(
            result_count=result_count,
            low_result_threshold=low_threshold,
            drop_ratio_threshold=drop_threshold,
            previous_result_count=baseline.previous_result_count,
            seven_day_average=baseline.seven_day_average,
            history_sample_count=baseline.history_sample_count,
        )
        quantity_reasons = list(assessment["reasons"])
        all_reasons = list(quantity_reasons)
        summary = assessment["summary"]
        conclusion = assessment["conclusion"]
        status = assessment["status"]

        if recall_rate_report is not None and not recall_rate_report.get("is_full_recall", True):
            if "recall_rate_incomplete" not in all_reasons:
                all_reasons.append("recall_rate_incomplete")
            status = "needs_attention"
            alert_text = str(recall_rate_report.get("operator_alert") or "").strip()
            if alert_text:
                summary = f"{summary} {alert_text}" if summary else alert_text
            if conclusion == "正常":
                conclusion = "召回率未达100%"

        evidence: dict[str, Any] = {
            "result_count": result_count,
            "previous_result_count": baseline.previous_result_count,
            "seven_day_average": None,
            "comparison_baseline": "previous_result",
            "history_sample_count": baseline.history_sample_count,
            "comparison_status": assessment["comparison_status"],
            "result_count_history": list(baseline.result_count_history),
            "low_result_threshold": max(int(low_threshold or 0), 1),
            "result_drop_threshold": max(float(drop_threshold or 0), 0.0),
            "reasons": list(all_reasons),
            "summary": summary,
            "drop_ratio": assessment["drop_ratio"],
            "scoring_trace": assessment["scoring_trace"],
        }
        if recall_rate_report is not None:
            evidence["recall_rate"] = recall_rate_report
        if recall_rate_note is not None:
            evidence["recall_rate_status"] = recall_rate_note
        if "result_count_drop" in quantity_reasons and self.drop_assessor is not None:
            try:
                drop = self.drop_assessor.assess(spec, s0, baseline)
            except Exception:
                drop = {"status": "manual_review", "reason": "骤降前置核验失败"}
            evidence["drop_assessment"] = drop
            if drop["status"] in {"normal", "manual_review"}:
                quantity_reasons.remove("result_count_drop")
                all_reasons = [r for r in all_reasons if r != "result_count_drop"]
                evidence["reasons"] = list(all_reasons)
                if not quantity_reasons:
                    label = "正常消失" if drop["status"] == "normal" else "需人工复核"
                    drop_summary = (
                        f"本次召回 {result_count} 件，消失商品均已下架或被当前Query拉黑，无需图谱治理。"
                        if drop["status"] == "normal"
                        else str(drop.get("reason") or "骤降证据不足")
                    )
                    if recall_rate_report is not None and not recall_rate_report.get("is_full_recall", True):
                        alert_text = str(recall_rate_report.get("operator_alert") or "").strip()
                        drop_summary = f"{drop_summary} {alert_text}".strip()
                    out_payload: dict[str, Any] = {
                        "status": (
                            "needs_attention"
                            if (drop["status"] != "normal" or "recall_rate_incomplete" in all_reasons)
                            else "healthy"
                        ),
                        "summary": drop_summary,
                        "conclusion": label,
                        "reasons": list(all_reasons),
                        "evidence": evidence,
                        "async_task_id": None,
                        "review_required": drop["status"] == "manual_review" or "recall_rate_incomplete" in all_reasons,
                    }
                    if recall_rate_report is not None:
                        out_payload["recall_rate"] = recall_rate_report
                    return NodeResult.executed(out_payload)
        if not quantity_reasons:
            out_payload = {
                "status": status,
                "summary": summary,
                "conclusion": conclusion,
                "reasons": list(all_reasons),
                "evidence": evidence,
                "async_task_id": None,
            }
            if recall_rate_report is not None:
                out_payload["recall_rate"] = recall_rate_report
            return NodeResult.executed(out_payload)

        async_tasks = context.get("async_tasks")
        if async_tasks is None:
            out_payload = {
                "status": status,
                "summary": summary,
                "conclusion": conclusion,
                "reasons": list(all_reasons),
                "evidence": evidence,
                "async_task_id": None,
                "async_task_status": "not_configured",
            }
            if recall_rate_report is not None:
                out_payload["recall_rate"] = recall_rate_report
            return NodeResult.executed(out_payload)
        batch_id = context.get("batch_id") or spec.batch_id
        if not str(batch_id or "").strip():
            return NodeResult.error(
                "ASYNC_TASK_CONTEXT_MISSING",
                "召回异常缺少批次标识，无法投递 KG Agent 异步任务",
            )

        idempotency_key = f"kg:{batch_id}:{spec.brand}:{spec.query}:recall"
        try:
            submission = async_tasks.enqueue_for_query(
                batch_id=str(batch_id),
                brand=spec.brand,
                query_text=spec.query,
                trigger_source="recall_anomaly",
                evidence=evidence,
                idempotency_key=idempotency_key,
                langfuse_context=context.get("langfuse_trace_context"),
            )
        except Exception as exc:
            return NodeResult.error(
                "ASYNC_TASK_PERSIST_FAILED",
                str(exc).strip() or exc.__class__.__name__,
            )
        sub_status = getattr(submission, "status", None)
        status_value = getattr(sub_status, "value", sub_status)
        if status_value == AsyncTaskStatus.ERROR.value or getattr(submission, "error_code", None):
            return NodeResult.error(
                getattr(submission, "error_code", None) or "ASYNC_TASK_PERSIST_FAILED",
                getattr(submission, "error_message", None) or "KG Agent 异步任务投递失败",
            )
        out_payload = {
            "status": status,
            "summary": summary,
            "conclusion": conclusion,
            "reasons": list(all_reasons),
            "evidence": evidence,
            "async_task_id": getattr(submission, "task_id", None),
            "async_task_status": status_value,
            "async_task_created": bool(getattr(submission, "created", False)),
            "idempotency_key": idempotency_key,
        }
        if recall_rate_report is not None:
            out_payload["recall_rate"] = recall_rate_report
        return NodeResult.executed(out_payload)


__all__ = [
    "RECALL_SCORE_VERSION",
    "RecallHistoryBaseline",
    "RecallHistoryGateway",
    "SqlAlchemyRecallHistoryGateway",
    "build_recall_assessment",
    "RecallCheckRunner",
]
