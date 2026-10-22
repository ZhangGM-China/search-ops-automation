import datetime
import json
import math
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from src.persistence import (
    SessionLocal,
    TopQueryActionPlan,
    TopQueryExperiment,
    TopQueryExperimentDailyMetric,
    TopQueryExperimentSample,
    TopQueryTask,
    TopQueryAgentEvent,
)
from src.governance.query_identity import normalize_governance_query_key


CALCULATION_VERSION = "top-query-ab-test-v2"
REPORT_TIMEZONE = "Asia/Shanghai"
EFFECT_EPSILON = 1e-12


class ExperimentAnalyticsService:
    def __init__(
        self,
        *,
        report_date: Optional[datetime.date] = None,
        clock: Optional[Callable[[], datetime.datetime]] = None,
    ):
        self._report_date = report_date
        self._clock = clock or (lambda: datetime.datetime.now(ZoneInfo(REPORT_TIMEZONE)))

    def get_report(self, experiment_id: int) -> Dict[str, Any]:
        return self._report_from(self._load_snapshot(experiment_id))

    def evaluate_snapshot_quality(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        return self._report_from(snapshot)["quality"]

    def get_export_data(self, experiment_id: int) -> Dict[str, Any]:
        return self.get_report(experiment_id)

    def _report_from(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        experiment = snapshot["experiment"]
        generated_at = self._report_now()
        report_date = self._report_date or generated_at.date()
        version = self._calculation_version(experiment)
        if version != CALCULATION_VERSION:
            raise ValueError("retired_calculation_version")
        baseline_evidence = self._baseline_evidence(snapshot, version)
        baseline_quality = self._baseline_quality(snapshot, version, baseline_evidence)
        baseline = self._baseline(snapshot["samples"], version)
        sample_identity = self._sample_identity(snapshot)
        metric_identity = self._metric_identity(snapshot, sample_identity)
        expected_dates = self._expected_observation_dates(experiment, report_date)
        metric_evidence = {
            int(row.id): self._metric_evidence(row, version)
            for row in snapshot["metrics"]
        }
        days = self._build_days(
            snapshot,
            version=version,
            baseline=baseline,
            baseline_quality=baseline_quality,
            metric_evidence=metric_evidence,
            sample_identity=sample_identity,
            metric_identity=metric_identity,
            expected_dates=expected_dates,
        )
        quality = self._overall_quality(days)
        if baseline_quality["status"] != "complete":
            quality = {**quality, "status": "failed", "error": baseline_quality["error"] or "invalid_baseline"}
        valid_rows = [
            row for row in snapshot["metrics"]
            if row.data_date in expected_dates
            and self._is_formal_metric(
                row,
                metric_evidence[int(row.id)],
                metric_identity[int(row.id)],
            )
        ]
        groups = {
            group: self._current_group_summary(
                [row for row in valid_rows if row.group_type == group],
                baseline[group],
                metric_evidence,
            )
            for group in ("treatment", "control")
        }
        effect = None
        if days and quality["status"] == "complete":
            effect = self._period_effect(groups, days)
        operations = self._sum_operations([day["operations"] for day in days]) if days else self._empty_operations()
        experiment_payload = self._experiment_payload(experiment)
        overview = {
            **experiment_payload,
            "quality": quality,
            "groups": groups,
            "effect": effect,
            "operations": operations,
        }
        queries = self._query_items(
            snapshot,
            version=version,
            baseline=baseline,
            metric_evidence=metric_evidence,
            metric_identity=metric_identity,
            expected_dates=expected_dates,
            overall_complete=quality["status"] == "complete",
        )
        daily_metrics = self._daily_metric_items(
            snapshot["metrics"], metric_evidence, metric_identity, expected_dates
        )
        actions = self._action_items(snapshot)
        dates = [datetime.date.fromisoformat(day["dataDate"]) for day in days]
        return {
            "experiment": experiment_payload,
            "calculationVersion": version,
            "generatedAt": generated_at.isoformat(),
            "timezone": REPORT_TIMEZONE,
            "reportDate": report_date.isoformat(),
            "baselineQuality": baseline_quality,
            "quality": quality,
            "groups": groups,
            "effect": effect,
            "operations": operations,
            "trend": [
                {
                    "dataDate": day["dataDate"],
                    "quality": day["quality"],
                    "groups": day["groups"],
                    "effect": day["effect"] if day["quality"]["status"] == "complete" else None,
                    "operations": day["operations"],
                }
                for day in days
            ],
            "queries": queries,
            "queryDailyMetrics": daily_metrics,
            "baselineEvidence": baseline_evidence,
            "actions": actions,
            "dataStartDate": dates[0].isoformat() if dates else None,
            "dataEndDate": dates[-1].isoformat() if dates else None,
            "dataDays": len(dates),
            "overview": overview,
        }

    def _report_now(self) -> datetime.datetime:
        value = self._clock()
        timezone = ZoneInfo(REPORT_TIMEZONE)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone)
        return value.astimezone(timezone)

    @staticmethod
    def _expected_observation_dates(
        experiment: Any,
        report_date: datetime.date,
    ) -> Set[datetime.date]:
        if (
            experiment.status not in {"running", "ended"}
            or not experiment.planned_start_date
            or not experiment.planned_end_date
        ):
            return set()
        start_date = experiment.planned_start_date - datetime.timedelta(days=1)
        end_date = experiment.planned_end_date - datetime.timedelta(days=1)
        if experiment.status == "running":
            end_date = min(end_date, report_date - datetime.timedelta(days=1))
        elif experiment.ended_at:
            ended_at = experiment.ended_at
            timezone = ZoneInfo(REPORT_TIMEZONE)
            if ended_at.tzinfo is None or ended_at.utcoffset() is None:
                ended_at = ended_at.replace(tzinfo=timezone)
            else:
                ended_at = ended_at.astimezone(timezone)
            end_date = min(
                experiment.planned_end_date,
                ended_at.date(),
            ) - datetime.timedelta(days=1)
        if end_date < start_date:
            return set()
        return {
            start_date + datetime.timedelta(days=offset)
            for offset in range((end_date - start_date).days + 1)
        }

    @staticmethod
    def _sample_identity(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        brand = snapshot["experiment"].brand
        samples_by_key: Dict[str, List[Any]] = defaultdict(list)
        for sample in snapshot["samples"]:
            key = normalize_governance_query_key(brand, sample.query_text)
            samples_by_key[key].append(sample)
        duplicate_keys = sorted(
            key for key, rows in samples_by_key.items() if not key or len(rows) != 1
        )
        unique_samples = {
            key: rows[0]
            for key, rows in samples_by_key.items()
            if key and len(rows) == 1
        }
        return {
            "status": (
                "complete"
                if len(snapshot["samples"]) == 40
                and len(unique_samples) == 40
                and not duplicate_keys
                else "failed"
            ),
            "samplesByKey": unique_samples,
            "expectedKeys": set(unique_samples),
            "duplicateKeys": duplicate_keys,
        }

    def _metric_identity(
        self,
        snapshot: Dict[str, Any],
        sample_identity: Dict[str, Any],
    ) -> Dict[int, Dict[str, Any]]:
        brand = snapshot["experiment"].brand
        rows_by_date: Dict[Optional[datetime.date], List[Any]] = defaultdict(list)
        for row in snapshot["metrics"]:
            rows_by_date[row.data_date].append(row)
        result: Dict[int, Dict[str, Any]] = {}
        for rows in rows_by_date.values():
            keys = [normalize_governance_query_key(brand, row.query_text) for row in rows]
            counts = {key: keys.count(key) for key in set(keys)}
            for row, key in zip(rows, keys):
                locked_sample = sample_identity["samplesByKey"].get(key)
                complete = bool(
                    sample_identity["status"] == "complete"
                    and locked_sample is not None
                    and counts.get(key) == 1
                    and str(row.group_type) == str(locked_sample.group_type)
                )
                result[int(row.id)] = {
                    "status": "complete" if complete else "failed",
                    "error": None if complete else "experiment_query_identity_mismatch",
                    "canonicalQueryKey": key or None,
                    "lockedQueryText": locked_sample.query_text if locked_sample is not None else None,
                    "lockedGroupType": locked_sample.group_type if locked_sample is not None else None,
                }
        return result

    @staticmethod
    def _day_identity_complete(
        rows: List[Any],
        sample_identity: Dict[str, Any],
        metric_identity: Dict[int, Dict[str, Any]],
    ) -> bool:
        if sample_identity["status"] != "complete":
            return False
        matched_keys = {
            metric_identity[int(row.id)]["canonicalQueryKey"]
            for row in rows
            if metric_identity[int(row.id)]["status"] == "complete"
        }
        return (
            len(rows) == 40
            and matched_keys == sample_identity["expectedKeys"]
            and all(
                metric_identity[int(row.id)]["status"] == "complete"
                for row in rows
            )
        )

    def _load_snapshot(self, experiment_id: int) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            experiment = db.get(TopQueryExperiment, experiment_id)
            if not experiment:
                raise ValueError(f"experiment not found: {experiment_id}")
            samples = db.query(TopQueryExperimentSample).filter_by(
                experiment_id=experiment_id
            ).order_by(
                TopQueryExperimentSample.group_type.asc(),
                TopQueryExperimentSample.sample_rank.asc(),
                TopQueryExperimentSample.id.asc(),
            ).all()
            metrics = db.query(TopQueryExperimentDailyMetric).filter_by(
                experiment_id=experiment_id
            ).order_by(
                TopQueryExperimentDailyMetric.data_date.asc(),
                TopQueryExperimentDailyMetric.group_type.asc(),
                TopQueryExperimentDailyMetric.id.asc(),
            ).all()
            task_ids = sorted({int(row.governance_task_id) for row in metrics if row.governance_task_id})
            tasks = db.query(TopQueryTask).filter(TopQueryTask.id.in_(task_ids)).all() if task_ids else []
            actions = db.query(TopQueryActionPlan).filter(TopQueryActionPlan.task_id.in_(task_ids)).order_by(
                TopQueryActionPlan.created_at.asc(), TopQueryActionPlan.id.asc()
            ).all() if task_ids else []
            events = db.query(TopQueryAgentEvent).filter(TopQueryAgentEvent.task_id.in_(task_ids)).all() if task_ids else []
            db.expunge(experiment)
            for row in samples + metrics + tasks + actions + events:
                db.expunge(row)
            return {"experiment": experiment, "samples": samples, "metrics": metrics, "tasks": tasks, "actions": actions, "events": events}
        finally:
            db.close()

    def _build_days(
        self,
        snapshot: Dict[str, Any],
        *,
        version: str,
        baseline: Dict[str, Any],
        baseline_quality: Dict[str, Any],
        metric_evidence: Dict[int, Dict[str, Any]],
        sample_identity: Dict[str, Any],
        metric_identity: Dict[int, Dict[str, Any]],
        expected_dates: Set[datetime.date],
    ) -> List[Dict[str, Any]]:
        rows_by_date: Dict[datetime.date, List[Any]] = defaultdict(list)
        for row in snapshot["metrics"]:
            if row.data_date:
                rows_by_date[row.data_date].append(row)
        actual_dates = set(rows_by_date)
        report_dates = sorted(actual_dates | expected_dates)
        return [
            self._build_day(
                data_date,
                rows_by_date.get(data_date, []),
                baseline,
                baseline_quality,
                snapshot,
                version,
                metric_evidence,
                sample_identity,
                metric_identity,
                (
                    "missing_observation_date"
                    if data_date in expected_dates and data_date not in actual_dates
                    else "unexpected_data_date"
                    if data_date in actual_dates and data_date not in expected_dates
                    else None
                ),
            )
            for data_date in report_dates
        ]

    def _build_day(
        self,
        data_date: datetime.date,
        rows: List[Any],
        baseline: Dict[str, Any],
        baseline_quality: Dict[str, Any],
        snapshot: Dict[str, Any],
        version: str,
        metric_evidence: Dict[int, Dict[str, Any]],
        sample_identity: Dict[str, Any],
        metric_identity: Dict[int, Dict[str, Any]],
        date_error: Optional[str],
    ) -> Dict[str, Any]:
        treatment = [row for row in rows if row.group_type == "treatment"]
        control = [row for row in rows if row.group_type == "control"]
        treatment_tasks = {int(row.governance_task_id) for row in treatment if row.governance_task_id}
        control_tasks = {int(row.governance_task_id) for row in control if row.governance_task_id}
        control_action_count = sum(int(row.action_count or 0) for row in control)
        contaminated = [row for row in rows if row.contamination_status != "clean"]
        source_failures = []
        for row in rows:
            payload = self._json_object(row.raw_payload)
            quality = payload.get("dataQuality")
            if isinstance(quality, dict) and quality.get("status") != "complete":
                source_failures.append(quality)
        observed = [row for row in rows if row.observed_status == "observed"]
        clean_observed = [
            row for row in rows
            if row.observed_status == "observed" and row.contamination_status == "clean"
        ]
        numerator_failures = [
            metric_evidence[int(row.id)]["error"]
            for row in clean_observed
            if metric_evidence[int(row.id)]["status"] != "complete"
        ]
        formal_rows = [
            row for row in rows
            if self._is_formal_metric(
                row,
                metric_evidence[int(row.id)],
                metric_identity[int(row.id)],
            )
        ]
        identity_complete = self._day_identity_complete(
            rows, sample_identity, metric_identity
        )
        task_by_id = {int(task.id): task for task in snapshot["tasks"]}
        ended = snapshot["experiment"].status == "ended"
        task_statuses = [
            str(row.governance_status or "").lower() if ended else str(
                getattr(task_by_id.get(int(row.governance_task_id or 0)), "status", row.governance_status) or ""
            ).lower()
            for row in treatment
        ]
        failed_tasks = [status for status in task_statuses if status == "failed"]
        unresolved_tasks = [status for status in task_statuses if status not in {"completed", "failed"}]
        action_summary = self._day_action_summary(treatment, snapshot)
        denominators_valid = all(
            sum(float(row.search_uv or 0) for row in formal_rows if row.group_type == group) > 0
            for group in ("treatment", "control")
        )
        complete = (
            version == CALCULATION_VERSION
            and date_error is None
            and identity_complete
            and len(rows) == 40
            and len(treatment) == 20
            and len(control) == 20
            and len(clean_observed) == 40
            and len(treatment_tasks) == 20
            and not control_tasks
            and control_action_count == 0
            and not contaminated
            and not source_failures
            and not numerator_failures
            and not failed_tasks
            and not unresolved_tasks
            and action_summary["failed"] == 0
            and action_summary["inconclusive"] == 0
            and action_summary["unresolved"] == 0
            and denominators_valid
            and baseline_quality["status"] == "complete"
        )
        error = None
        if date_error:
            error = date_error
        elif not identity_complete:
            error = "experiment_query_identity_mismatch"
        elif version != CALCULATION_VERSION:
            error = "unsupported_calculation_version"
        elif source_failures:
            error = source_failures[0].get("error") or "source_data_quality_failed"
        elif numerator_failures:
            error = numerator_failures[0]
        elif contaminated or control_tasks or control_action_count:
            error = "control_group_contamination"
        elif (
            failed_tasks
            or unresolved_tasks
            or action_summary["failed"]
            or action_summary["inconclusive"]
            or action_summary["unresolved"]
        ):
            error = "governance_not_resolved"
        elif not denominators_valid:
            error = "zero_search_uv_denominator"
        elif baseline_quality["status"] != "complete":
            error = baseline_quality["error"] or "invalid_baseline"
        elif not complete:
            error = "experiment_quality_gate_failed"
        quality = {
            "status": "complete" if complete else "failed",
            "error": error,
            "expectedSamples": 40,
            "observedSamples": len(observed),
            "cleanObservedSamples": len(clean_observed),
            "treatmentSamples": len(treatment),
            "controlSamples": len(control),
            "treatmentTaskCount": len(treatment_tasks),
            "controlTaskCount": len(control_tasks),
            "controlActionCount": control_action_count,
            "contaminationCount": len(contaminated),
            "failedTaskCount": len(failed_tasks),
            "unresolvedTaskCount": len(unresolved_tasks),
            "failedActionCount": action_summary["failed"],
            "inconclusiveActionCount": action_summary["inconclusive"],
            "unresolvedActionCount": action_summary["unresolved"],
            "invalidNumeratorCount": len(numerator_failures),
            "canonicalMatchedSamples": len(formal_rows),
            "baselineMissingKeys": baseline_quality["baselineMissingKeys"],
            "zeroBaselineGroups": baseline_quality["zeroBaselineGroups"],
        }
        groups = {
            group: self._current_group_summary(
                [row for row in formal_rows if row.group_type == group],
                baseline[group],
                metric_evidence,
            )
            for group in ("treatment", "control")
        }
        effect = None
        if complete:
            effect = self._effect(groups)
        return {
            "dataDate": data_date.isoformat(),
            "quality": quality,
            "groups": groups,
            "effect": effect,
            "operations": self._operations(treatment, action_summary),
        }

    def _day_action_summary(self, treatment: List[Any], snapshot: Dict[str, Any]) -> Dict[str, int]:
        if snapshot["experiment"].status == "ended":
            summaries = [self._json_object(row.raw_payload).get("governanceActionSummary") or {} for row in treatment]
            planned = sum(int(item.get("planned") or 0) for item in summaries)
            executed = sum(int(item.get("executed") or 0) for item in summaries)
            failed = sum(int(item.get("failed") or 0) for item in summaries)
            inconclusive = sum(int(item.get("inconclusive") or 0) for item in summaries)
            rejected = sum(int(item.get("rejected") or 0) for item in summaries)
            cancelled = sum(int(item.get("cancelled") or 0) for item in summaries)
            unresolved = sum(
                int(item.get("unresolved")) if item.get("unresolved") is not None
                else max(
                    0,
                    int(item.get("planned") or 0)
                    - int(item.get("executed") or 0)
                    - int(item.get("failed") or 0)
                    - int(item.get("inconclusive") or 0)
                    - int(item.get("rejected") or 0)
                    - int(item.get("cancelled") or 0),
                )
                for item in summaries
            )
            return {
                "planned": planned,
                "executed": executed,
                "failed": failed,
                "inconclusive": inconclusive,
                "rejected": rejected,
                "cancelled": cancelled,
                "unresolved": unresolved,
            }
        task_ids = {int(row.governance_task_id) for row in treatment if row.governance_task_id}
        actions = [row for row in snapshot["actions"] if int(row.task_id or 0) in task_ids]
        success = {"executed", "verified", "completed", "dry_run_executed", "rejected", "cancelled"}
        failed = sum(
            1 for row in actions
            if str(row.status or "").lower() in {"failed", "verification_failed"}
        )
        inconclusive = sum(
            1 for row in actions
            if str(row.status or "").lower() == "verification_inconclusive"
        )
        rejected = sum(1 for row in actions if str(row.status or "").lower() == "rejected")
        cancelled = sum(1 for row in actions if str(row.status or "").lower() == "cancelled")
        executed = sum(1 for row in actions if str(row.status or "").lower() in {"executed", "verified", "completed", "dry_run_executed"})
        unresolved = sum(
            1 for row in actions
            if str(row.status or "").lower() not in (
                success | {"failed", "verification_failed", "verification_inconclusive"}
            )
        )
        return {
            "planned": len(actions),
            "executed": executed,
            "failed": failed,
            "inconclusive": inconclusive,
            "rejected": rejected,
            "cancelled": cancelled,
            "unresolved": unresolved,
        }

    def _baseline(self, samples: List[Any], version: str) -> Dict[str, Any]:
        return {
            group: self._baseline_group_summary(
                [row for row in samples if row.group_type == group], version
            )
            for group in ("treatment", "control")
        }

    def _baseline_group_summary(self, rows: List[Any], version: str) -> Dict[str, Any]:
        if version == CALCULATION_VERSION:
            valid_rows = [
                row for row in rows
                if self._baseline_row_valid(
                    version,
                    row.baseline_observed_days,
                    row.baseline_search_uv,
                    row.baseline_search_pv,
                    row.baseline_click_uv,
                    row.baseline_purchase_uv,
                    row.baseline_ctr,
                    row.baseline_cvr,
                )
            ]
            search_uv = sum(float(row.baseline_search_uv) for row in valid_rows)
            search_pv = sum(float(row.baseline_search_pv) for row in valid_rows)
            click_uv = sum(float(row.baseline_click_uv) for row in valid_rows)
            purchase_uv = sum(float(row.baseline_purchase_uv) for row in valid_rows)
        else:
            valid_rows = []
            search_uv = search_pv = click_uv = purchase_uv = 0.0
        return {
            "sampleCount": len(rows),
            "baselineValidSampleCount": len(valid_rows),
            "searchUv": search_uv,
            "searchPv": search_pv,
            "clickUv": click_uv,
            "purchaseUv": purchase_uv,
            "ctr": click_uv / search_uv if search_uv else None,
            "cvr": purchase_uv / search_uv if search_uv else None,
        }

    def _baseline_evidence(self, snapshot: Dict[str, Any], version: str) -> List[Dict[str, Any]]:
        experiment = snapshot["experiment"]
        items = []
        for sample in snapshot["samples"]:
            if version == CALCULATION_VERSION:
                observed_days = sample.baseline_observed_days
                search_uv = sample.baseline_search_uv
                search_pv = sample.baseline_search_pv
                click_uv = sample.baseline_click_uv
                purchase_uv = sample.baseline_purchase_uv
                ctr = sample.baseline_ctr
                cvr = sample.baseline_cvr
                window_type = "seven_day_weighted"
                start_date = experiment.baseline_start_date
                end_date = experiment.baseline_end_date
            else:
                observed_days = sample.baseline_observed_days
                search_uv = sample.baseline_search_uv
                search_pv = sample.baseline_search_pv
                click_uv = sample.baseline_click_uv
                purchase_uv = sample.baseline_purchase_uv
                ctr = sample.baseline_ctr
                cvr = sample.baseline_cvr
                window_type = "unsupported"
                start_date = experiment.baseline_start_date
                end_date = experiment.baseline_end_date
            row_valid = self._baseline_row_valid(
                version,
                observed_days,
                search_uv,
                search_pv,
                click_uv,
                purchase_uv,
                ctr,
                cvr,
            )
            items.append({
                "queryText": sample.query_text,
                "groupType": sample.group_type,
                "sampleRank": sample.sample_rank,
                "sampleSearchUv": sample.sample_search_uv,
                "sampleSearchPv": sample.sample_search_pv,
                "sampleCtr": sample.sample_ctr,
                "sampleCvr": sample.sample_cvr,
                "sampleAvgCtr": sample.sample_avg_ctr,
                "sampleAvgCvr": sample.sample_avg_cvr,
                "sampleQueryPvShare": sample.sample_query_pv_share,
                "baselineWindowType": window_type,
                "baselineStartDate": start_date.isoformat() if start_date else None,
                "baselineEndDate": end_date.isoformat() if end_date else None,
                "baselineObservedDays": observed_days,
                "baselineSearchUv": search_uv,
                "baselineSearchPv": search_pv,
                "baselineClickUv": click_uv,
                "baselinePurchaseUv": purchase_uv,
                "baselineCtr": ctr,
                "baselineCvr": cvr,
                "qualityStatus": "complete" if row_valid else "failed",
                "qualityError": None if row_valid else "invalid_baseline_evidence",
            })
        return items

    def _baseline_quality(
        self,
        snapshot: Dict[str, Any],
        version: str,
        evidence: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        experiment = snapshot["experiment"]
        invalid = sorted(
            str(item["queryText"])
            for item in evidence
            if item["qualityStatus"] != "complete"
        )
        group_counts = {
            group: sum(1 for item in evidence if item["groupType"] == group)
            for group in ("treatment", "control")
        }
        zero_groups = [
            group for group in ("treatment", "control")
            if sum(float(item["baselineSearchUv"] or 0) for item in evidence if item["groupType"] == group) <= 0
        ]
        window_valid = True
        locked = True
        if version == CALCULATION_VERSION:
            window_valid = bool(
                experiment.baseline_start_date
                and experiment.baseline_end_date
                and experiment.planned_start_date
                and (experiment.baseline_end_date - experiment.baseline_start_date).days == 6
                and experiment.baseline_end_date
                == experiment.planned_start_date - datetime.timedelta(days=1)
                and experiment.baseline_start_date
                == experiment.planned_start_date - datetime.timedelta(days=7)
            )
            locked = experiment.baseline_locked_at is not None
        supported = version == CALCULATION_VERSION
        complete = (
            supported
            and len(evidence) == 40
            and group_counts == {"treatment": 20, "control": 20}
            and not invalid
            and not zero_groups
            and window_valid
            and locked
        )
        error = None
        if not supported:
            error = "unsupported_calculation_version"
        elif not complete:
            error = "invalid_baseline"
        return {
            "status": "complete" if complete else "failed",
            "error": error,
            "calculationVersion": version,
            "expectedSamples": 40,
            "sampleCount": len(evidence),
            "treatmentSamples": group_counts["treatment"],
            "controlSamples": group_counts["control"],
            "expectedDays": 7,
            "baselineStartDate": experiment.baseline_start_date.isoformat() if experiment.baseline_start_date else None,
            "baselineEndDate": experiment.baseline_end_date.isoformat() if experiment.baseline_end_date else None,
            "baselineLockedAt": experiment.baseline_locked_at.isoformat() if experiment.baseline_locked_at else None,
            "baselineMissingKeys": invalid,
            "zeroBaselineGroups": zero_groups,
            "windowValid": window_valid,
            "locked": locked,
        }

    def _baseline_row_valid(
        self,
        version: str,
        observed_days: Any,
        search_uv: Any,
        search_pv: Any,
        click_uv: Any,
        purchase_uv: Any,
        ctr: Any,
        cvr: Any,
    ) -> bool:
        if version != CALCULATION_VERSION:
            return False
        if observed_days != 7:
            return False
        values = (search_uv, search_pv, click_uv, purchase_uv, ctr, cvr)
        if not all(self._is_finite_number(value) and float(value) >= 0 for value in values):
            return False
        uv = float(search_uv)
        if uv <= 0 or float(click_uv) > uv or float(purchase_uv) > uv:
            return False
        return (
            math.isclose(
                float(ctr),
                float(click_uv) / uv,
                rel_tol=1e-9,
                abs_tol=EFFECT_EPSILON,
            )
            and math.isclose(
                float(cvr),
                float(purchase_uv) / uv,
                rel_tol=1e-9,
                abs_tol=EFFECT_EPSILON,
            )
        )

    def _effect(self, groups: Dict[str, Any]) -> Dict[str, Optional[float]]:
        return {
            "ctrNetChange": self._subtract(groups["treatment"]["ctrChange"], groups["control"]["ctrChange"]),
            "cvrNetChange": self._subtract(groups["treatment"]["cvrChange"], groups["control"]["cvrChange"]),
        }

    def _period_effect(
        self,
        groups: Dict[str, Any],
        days: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        effect = self._effect(groups)
        treatment_search_uv = groups["treatment"].get("currentSearchUv")
        return {
            **effect,
            "ctrPositiveDays": sum(
                1
                for day in days
                if day["quality"]["status"] == "complete"
                and self._is_positive_effect(day.get("effect"), "ctrNetChange")
            ),
            "cvrPositiveDays": sum(
                1
                for day in days
                if day["quality"]["status"] == "complete"
                and self._is_positive_effect(day.get("effect"), "cvrNetChange")
            ),
            "estimatedIncrementalClickUv": self._multiply(
                effect["ctrNetChange"], treatment_search_uv
            ),
            "estimatedIncrementalPurchaseUv": self._multiply(
                effect["cvrNetChange"], treatment_search_uv
            ),
        }

    @classmethod
    def _is_positive_effect(cls, effect: Any, field: str) -> bool:
        return (
            isinstance(effect, dict)
            and cls._is_finite_number(effect.get(field))
            and float(effect[field]) > EFFECT_EPSILON
        )

    def _current_group_summary(
        self,
        rows: List[Any],
        baseline: Dict[str, Any],
        metric_evidence: Dict[int, Dict[str, Any]],
    ) -> Dict[str, Any]:
        uv = sum(float(row.search_uv or 0) for row in rows)
        click_uv = sum(float(metric_evidence[int(row.id)]["clickUv"] or 0) for row in rows)
        purchase_uv = sum(float(metric_evidence[int(row.id)]["purchaseUv"] or 0) for row in rows)
        current_ctr = click_uv / uv if uv else None
        current_cvr = purchase_uv / uv if uv else None
        return {
            "sampleCount": int(baseline["sampleCount"]),
            "observedRows": len(rows),
            "searchUv": int(uv),
            "searchPv": sum(int(row.search_pv or 0) for row in rows),
            "clickUv": click_uv,
            "purchaseUv": purchase_uv,
            "currentSearchUv": int(uv),
            "currentSearchPv": sum(int(row.search_pv or 0) for row in rows),
            "currentClickUv": click_uv,
            "currentPurchaseUv": purchase_uv,
            "baselineSearchUv": int(baseline["searchUv"]),
            "baselineSearchPv": int(baseline["searchPv"]),
            "baselineClickUv": baseline["clickUv"],
            "baselinePurchaseUv": baseline["purchaseUv"],
            "baselineValidSampleCount": int(baseline["baselineValidSampleCount"]),
            "baselineCtr": baseline["ctr"],
            "baselineCvr": baseline["cvr"],
            "currentCtr": current_ctr,
            "currentCvr": current_cvr,
            "ctrChange": self._subtract(current_ctr, baseline["ctr"]),
            "cvrChange": self._subtract(current_cvr, baseline["cvr"]),
        }

    def _metric_evidence(self, row: Any, version: str) -> Dict[str, Any]:
        uv = row.search_uv
        if not self._is_finite_number(uv) or float(uv) < 0:
            click = self._metric_component_failure("invalid_metric_denominator", "invalid")
            purchase = self._metric_component_failure("invalid_metric_denominator", "invalid")
            return self._combined_metric_evidence(click, purchase)
        payload = self._json_object(row.raw_payload)
        click = self._metric_component(
            payload,
            ("click_uv", "clickUv"),
            float(uv),
            version,
        )
        purchase = self._metric_component(
            payload,
            ("purchase_uv", "pay_uv", "purchaseUv", "payUv"),
            float(uv),
            version,
        )
        return self._combined_metric_evidence(click, purchase)

    def _metric_component(
        self,
        payload: Dict[str, Any],
        keys: Tuple[str, ...],
        uv: float,
        version: str,
    ) -> Dict[str, Any]:
        value, source = self._raw_numerator(payload, keys)
        if version != CALCULATION_VERSION:
            return self._metric_component_failure("unsupported_calculation_version", "invalid")
        if source == "invalid":
            return self._metric_component_failure("invalid_metric_numerator", "invalid")
        if source == "missing":
            return self._metric_component_failure("missing_metric_numerator", "missing")
        if (
            not self._is_finite_number(value)
            or float(value) < 0
            or float(value) > uv
        ):
            return self._metric_component_failure("invalid_metric_numerator", "invalid")
        return {
            "status": "complete",
            "error": None,
            "value": float(value),
            "rate": float(value) / uv if uv > 0 else None,
            "numeratorSource": source,
        }

    @staticmethod
    def _metric_component_failure(error: str, source: str) -> Dict[str, Any]:
        return {
            "status": "failed",
            "error": error,
            "value": None,
            "rate": None,
            "numeratorSource": source,
        }

    @staticmethod
    def _combined_metric_evidence(
        click: Dict[str, Any],
        purchase: Dict[str, Any],
    ) -> Dict[str, Any]:
        components = (click, purchase)
        failures = [item for item in components if item["status"] != "complete"]
        if failures:
            errors = {str(item["error"]) for item in failures}
            if "unsupported_calculation_version" in errors:
                error = "unsupported_calculation_version"
            elif "invalid_metric_denominator" in errors:
                error = "invalid_metric_denominator"
            elif "invalid_metric_numerator" in errors:
                error = "invalid_metric_numerator"
            else:
                error = "missing_metric_numerator"
            if len(failures) == 1 and error == "missing_metric_numerator":
                source = "partial"
            elif len(failures) == 1:
                source = "invalid"
            elif errors == {"missing_metric_numerator"}:
                source = "missing"
            else:
                source = "invalid"
        else:
            error = None
            sources = {click["numeratorSource"], purchase["numeratorSource"]}
            source = next(iter(sources)) if len(sources) == 1 else "mixed"
        return {
            "status": "failed" if failures else "complete",
            "error": error,
            "clickUv": click["value"],
            "purchaseUv": purchase["value"],
            "ctr": click["rate"],
            "cvr": purchase["rate"],
            "numeratorSource": source,
            "clickNumeratorSource": click["numeratorSource"],
            "purchaseNumeratorSource": purchase["numeratorSource"],
            "clickNumeratorStatus": click["status"],
            "purchaseNumeratorStatus": purchase["status"],
            "clickNumeratorError": click["error"],
            "purchaseNumeratorError": purchase["error"],
        }

    @classmethod
    def _raw_numerator(cls, payload: Dict[str, Any], keys: Tuple[str, ...]) -> Tuple[Optional[float], str]:
        for key in keys:
            if key not in payload:
                continue
            value = payload.get(key)
            if not cls._is_finite_number(value):
                return None, "invalid"
            return float(value), "raw"
        return None, "missing"

    @staticmethod
    def _is_finite_number(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    @classmethod
    def _multiply(cls, left: Any, right: Any) -> Optional[float]:
        if not cls._is_finite_number(left) or not cls._is_finite_number(right):
            return None
        return float(left) * float(right)

    def _is_formal_metric(
        self,
        row: Any,
        evidence: Dict[str, Any],
        identity: Dict[str, Any],
    ) -> bool:
        source_quality = self._json_object(row.raw_payload).get("dataQuality")
        return (
            row.observed_status == "observed"
            and row.contamination_status == "clean"
            and evidence["status"] == "complete"
            and identity["status"] == "complete"
            and not (
                isinstance(source_quality, dict)
                and source_quality.get("status") != "complete"
            )
        )

    def _query_items(
        self,
        snapshot: Dict[str, Any],
        *,
        version: str,
        baseline: Dict[str, Any],
        metric_evidence: Dict[int, Dict[str, Any]],
        metric_identity: Dict[int, Dict[str, Any]],
        expected_dates: Set[datetime.date],
        overall_complete: bool,
    ) -> List[Dict[str, Any]]:
        metrics_by_query: Dict[str, List[Any]] = defaultdict(list)
        for metric in snapshot["metrics"]:
            identity = metric_identity[int(metric.id)]
            if identity["status"] == "complete" and metric.data_date in expected_dates:
                metrics_by_query[str(identity["canonicalQueryKey"])].append(metric)
        baseline_by_sample_id = {
            int(sample.id): item
            for sample, item in zip(
                snapshot["samples"], self._baseline_evidence(snapshot, version)
            )
        }
        valid_rows = [
            row for row in snapshot["metrics"]
            if row.data_date in expected_dates
            and self._is_formal_metric(
                row,
                metric_evidence[int(row.id)],
                metric_identity[int(row.id)],
            )
        ]
        period_group = {
            group: {
                "searchUv": sum(float(row.search_uv or 0) for row in valid_rows if row.group_type == group),
                "clickUv": sum(
                    float(metric_evidence[int(row.id)]["clickUv"] or 0)
                    for row in valid_rows if row.group_type == group
                ),
                "purchaseUv": sum(
                    float(metric_evidence[int(row.id)]["purchaseUv"] or 0)
                    for row in valid_rows if row.group_type == group
                ),
            }
            for group in ("treatment", "control")
        }
        items = []
        for sample in snapshot["samples"]:
            sample_key = normalize_governance_query_key(
                snapshot["experiment"].brand, sample.query_text
            )
            query_metrics = metrics_by_query.get(sample_key, [])
            valid_query_metrics = [
                row for row in query_metrics
                if self._is_formal_metric(
                    row,
                    metric_evidence[int(row.id)],
                    metric_identity[int(row.id)],
                )
            ]
            latest = query_metrics[-1] if query_metrics else None
            current_uv = sum(float(row.search_uv or 0) for row in valid_query_metrics)
            current_pv = sum(float(row.search_pv or 0) for row in valid_query_metrics)
            current_click = sum(
                float(metric_evidence[int(row.id)]["clickUv"] or 0)
                for row in valid_query_metrics
            )
            current_purchase = sum(
                float(metric_evidence[int(row.id)]["purchaseUv"] or 0)
                for row in valid_query_metrics
            )
            current_ctr = current_click / current_uv if current_uv else None
            current_cvr = current_purchase / current_uv if current_uv else None
            query_baseline = baseline_by_sample_id[int(sample.id)]
            baseline_ctr = query_baseline["baselineCtr"]
            baseline_cvr = query_baseline["baselineCvr"]
            group = str(sample.group_type)
            group_period_uv = period_group[group]["searchUv"]
            group_baseline_uv = float(baseline[group]["searchUv"] or 0)
            current_ctr_component = current_click / group_period_uv if group_period_uv else None
            current_cvr_component = current_purchase / group_period_uv if group_period_uv else None
            baseline_ctr_component = (
                float(query_baseline["baselineClickUv"] or 0) / group_baseline_uv
                if group_baseline_uv else None
            )
            baseline_cvr_component = (
                float(query_baseline["baselinePurchaseUv"] or 0) / group_baseline_uv
                if group_baseline_uv else None
            )
            ctr_contribution = self._subtract(current_ctr_component, baseline_ctr_component)
            cvr_contribution = self._subtract(current_cvr_component, baseline_cvr_component)
            sign = 1 if group == "treatment" else -1
            net_ctr_contribution = (
                sign * ctr_contribution
                if overall_complete and ctr_contribution is not None else None
            )
            net_cvr_contribution = (
                sign * cvr_contribution
                if overall_complete and cvr_contribution is not None else None
            )
            sources = {
                metric_evidence[int(row.id)]["numeratorSource"]
                for row in valid_query_metrics
            }
            numerator_source = (
                next(iter(sources)) if len(sources) == 1
                else "mixed" if sources
                else None
            )
            items.append({
                "queryText": sample.query_text,
                "groupType": sample.group_type,
                "sampleRank": sample.sample_rank,
                "baselineObservedDays": query_baseline["baselineObservedDays"],
                "baselineSearchUv": query_baseline["baselineSearchUv"],
                "baselineSearchPv": query_baseline["baselineSearchPv"],
                "baselineClickUv": query_baseline["baselineClickUv"],
                "baselinePurchaseUv": query_baseline["baselinePurchaseUv"],
                "baselineCtr": baseline_ctr,
                "baselineCvr": baseline_cvr,
                "latestDataDate": latest.data_date.isoformat() if latest and latest.data_date else None,
                "observedStatus": latest.observed_status if latest else "not_synced",
                "contaminationStatus": latest.contamination_status if latest else "clean",
                "contaminationReason": latest.contamination_reason if latest else None,
                "observedDays": len({row.data_date for row in valid_query_metrics if row.data_date}),
                "unobservedDays": sum(1 for row in query_metrics if row.observed_status != "observed"),
                "contaminatedDays": sum(1 for row in query_metrics if row.contamination_status != "clean"),
                "currentSearchUv": int(current_uv) if valid_query_metrics else None,
                "currentSearchPv": int(current_pv) if valid_query_metrics else None,
                "currentClickUv": current_click if valid_query_metrics else None,
                "currentPurchaseUv": current_purchase if valid_query_metrics else None,
                "currentCtr": current_ctr,
                "currentCvr": current_cvr,
                "ctrChange": self._subtract(current_ctr, baseline_ctr),
                "cvrChange": self._subtract(current_cvr, baseline_cvr),
                "numeratorSource": numerator_source,
                "dataQualityStatus": (
                    "complete"
                    if query_metrics and len(valid_query_metrics) == len(query_metrics)
                    else "failed"
                ),
                "groupContribution": {
                    "ctr": ctr_contribution if overall_complete else None,
                    "cvr": cvr_contribution if overall_complete else None,
                },
                "netCtrContribution": net_ctr_contribution,
                "netCvrContribution": net_cvr_contribution,
                "effectDirection": self._effect_direction(
                    net_ctr_contribution,
                    net_cvr_contribution,
                ),
                "governanceTaskId": latest.governance_task_id if latest else None,
                "governanceStatus": latest.governance_status if latest else None,
                "actionCount": latest.action_count if latest else 0,
                "tokenUsage": sum(int(row.token_usage or 0) for row in valid_query_metrics),
                "durationSeconds": sum(float(row.duration_seconds or 0) for row in valid_query_metrics),
            })
        items.sort(key=lambda item: (item["groupType"], item["sampleRank"] or 0, item["queryText"]))
        return items

    @classmethod
    def _effect_direction(cls, ctr_effect: Any, cvr_effect: Any) -> str:
        if not cls._is_finite_number(ctr_effect) or not cls._is_finite_number(cvr_effect):
            return "undetermined"
        ctr_sign = cls._effect_sign(float(ctr_effect))
        cvr_sign = cls._effect_sign(float(cvr_effect))
        if ctr_sign == 0 and cvr_sign == 0:
            return "neutral"
        if ctr_sign >= 0 and cvr_sign >= 0:
            return "positive"
        if ctr_sign <= 0 and cvr_sign <= 0:
            return "negative"
        return "mixed"

    @staticmethod
    def _effect_sign(value: float) -> int:
        if abs(value) <= EFFECT_EPSILON:
            return 0
        return 1 if value > 0 else -1

    def _daily_metric_items(
        self,
        metrics: List[Any],
        metric_evidence: Dict[int, Dict[str, Any]],
        metric_identity: Dict[int, Dict[str, Any]],
        expected_dates: Set[datetime.date],
    ) -> List[Dict[str, Any]]:
        items = []
        for row in metrics:
            evidence = metric_evidence[int(row.id)]
            identity = metric_identity[int(row.id)]
            formal = (
                row.data_date in expected_dates
                and self._is_formal_metric(row, evidence, identity)
            )
            source_quality = self._json_object(row.raw_payload).get("dataQuality")
            if identity["status"] != "complete":
                quality_status = "failed"
                quality_error = "experiment_query_identity_mismatch"
            elif row.data_date not in expected_dates:
                quality_status = "failed"
                quality_error = "unexpected_data_date"
            elif row.observed_status != "observed":
                quality_status = "failed"
                quality_error = "not_observed"
            elif row.contamination_status != "clean":
                quality_status = "failed"
                quality_error = row.contamination_reason or "contaminated"
            elif isinstance(source_quality, dict) and source_quality.get("status") != "complete":
                quality_status = "failed"
                quality_error = source_quality.get("error") or "source_data_quality_failed"
            else:
                quality_status = evidence["status"]
                quality_error = evidence["error"]
            items.append({
                "dataDate": row.data_date.isoformat() if row.data_date else None,
                "queryText": row.query_text,
                "groupType": row.group_type,
                "canonicalQueryKey": identity["canonicalQueryKey"],
                "lockedQueryText": identity["lockedQueryText"],
                "lockedGroupType": identity["lockedGroupType"],
                "identityStatus": identity["status"],
                "identityError": identity["error"],
                "observedStatus": row.observed_status,
                "contaminationStatus": row.contamination_status,
                "contaminationReason": row.contamination_reason,
                "searchUv": row.search_uv,
                "searchPv": row.search_pv,
                "clickUv": evidence["clickUv"],
                "purchaseUv": evidence["purchaseUv"],
                "ctr": evidence["ctr"],
                "cvr": evidence["cvr"],
                "numeratorSource": evidence["numeratorSource"],
                "clickNumeratorSource": evidence["clickNumeratorSource"],
                "purchaseNumeratorSource": evidence["purchaseNumeratorSource"],
                "clickNumeratorStatus": evidence["clickNumeratorStatus"],
                "purchaseNumeratorStatus": evidence["purchaseNumeratorStatus"],
                "clickNumeratorError": evidence["clickNumeratorError"],
                "purchaseNumeratorError": evidence["purchaseNumeratorError"],
                "dataQualityStatus": quality_status,
                "dataQualityError": quality_error,
                "includedInEffect": formal,
                "governanceTaskId": row.governance_task_id,
                "governanceStatus": row.governance_status,
                "actionCount": row.action_count,
                "autoActionCount": row.auto_action_count,
                "manualActionCount": row.manual_action_count,
                "tokenUsage": row.token_usage,
                "durationSeconds": row.duration_seconds,
            })
        return items

    def _action_items(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        if snapshot["experiment"].status == "ended":
            return self._frozen_action_items(snapshot["metrics"])
        metric_by_task = {
            int(row.governance_task_id): row
            for row in snapshot["metrics"] if row.governance_task_id
        }
        return [{
            "recordType": "action",
            "detailAvailable": True,
            "missingDetailReason": None,
            "dataDate": (
                metric_by_task[int(action.task_id)].data_date.isoformat()
                if action.task_id and int(action.task_id) in metric_by_task else None
            ),
            "groupType": (
                metric_by_task[int(action.task_id)].group_type
                if action.task_id and int(action.task_id) in metric_by_task else None
            ),
            "governanceStatus": (
                metric_by_task[int(action.task_id)].governance_status
                if action.task_id and int(action.task_id) in metric_by_task else None
            ),
            "id": int(action.id),
            "taskId": action.task_id,
            "brand": action.brand,
            "queryText": action.query_text,
            "checkKey": action.check_key,
            "actionType": action.action_type,
            "executionMode": action.execution_mode,
            "status": action.status,
            "reason": action.reason,
            "errorMessage": action.error_message,
            "createdAt": action.created_at.isoformat() if action.created_at else None,
        } for action in snapshot["actions"]]

    def _operations(self, treatment: List[Any], action_summary: Dict[str, int]) -> Dict[str, Any]:
        planned = action_summary["planned"]
        executed = action_summary["executed"]
        failed = action_summary["failed"]
        inconclusive = action_summary["inconclusive"]
        unresolved = action_summary["unresolved"]
        rejected = action_summary["rejected"]
        cancelled = action_summary["cancelled"]
        attempted = executed + failed
        return {
            "governanceCoverage": sum(1 for row in treatment if row.governance_task_id) / 20,
            "actionSuccessRate": executed / attempted if attempted else None,
            "plannedActions": planned,
            "executedActions": executed,
            "failedActions": failed,
            "inconclusiveActions": inconclusive,
            "unresolvedActions": unresolved,
            "rejectedActions": rejected,
            "cancelledActions": cancelled,
            "tokenUsage": sum(int(row.token_usage or 0) for row in treatment),
            "durationSeconds": sum(float(row.duration_seconds or 0) for row in treatment),
        }

    @staticmethod
    def _sum_operations(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        executed = sum(int(item["executedActions"]) for item in items)
        failed = sum(int(item["failedActions"]) for item in items)
        inconclusive = sum(int(item["inconclusiveActions"]) for item in items)
        unresolved = sum(int(item["unresolvedActions"]) for item in items)
        rejected = sum(int(item["rejectedActions"]) for item in items)
        cancelled = sum(int(item["cancelledActions"]) for item in items)
        attempted = executed + failed
        return {
            "governanceCoverage": sum(float(item["governanceCoverage"]) for item in items) / len(items) if items else 0.0,
            "actionSuccessRate": executed / attempted if attempted else None,
            "plannedActions": sum(int(item["plannedActions"]) for item in items),
            "executedActions": executed,
            "failedActions": failed,
            "inconclusiveActions": inconclusive,
            "unresolvedActions": unresolved,
            "rejectedActions": rejected,
            "cancelledActions": cancelled,
            "tokenUsage": sum(int(item["tokenUsage"]) for item in items),
            "durationSeconds": sum(float(item["durationSeconds"]) for item in items),
        }

    def _frozen_action_items(self, metrics: List[Any]) -> List[Dict[str, Any]]:
        items = []
        for row in metrics:
            payload = self._json_object(row.raw_payload)
            summary = payload.get("governanceActionSummary") or {}
            frozen_actions = payload.get("governanceActions")
            if isinstance(frozen_actions, list) and frozen_actions:
                for action in frozen_actions:
                    if not isinstance(action, dict):
                        continue
                    items.append({
                        "recordType": "action",
                        "detailAvailable": True,
                        "missingDetailReason": None,
                        "groupType": row.group_type,
                        **action,
                        "governanceStatus": row.governance_status,
                    })
                continue
            planned = int(summary.get("planned") or 0)
            if planned <= 0:
                continue
            executed = int(summary.get("executed") or 0)
            failed = int(summary.get("failed") or 0)
            inconclusive = int(summary.get("inconclusive") or 0)
            rejected = int(summary.get("rejected") or 0)
            cancelled = int(summary.get("cancelled") or 0)
            unresolved = (
                int(summary.get("unresolved")) if summary.get("unresolved") is not None
                else max(0, planned - executed - failed - inconclusive - rejected - cancelled)
            )
            items.append({
                "recordType": "summary",
                "detailAvailable": False,
                "missingDetailReason": "metric_without_action_details",
                "groupType": row.group_type,
                "dataDate": row.data_date.isoformat() if row.data_date else None,
                "taskId": row.governance_task_id,
                "brand": row.brand,
                "queryText": row.query_text,
                "governanceStatus": row.governance_status,
                "planned": planned,
                "executed": executed,
                "failed": failed,
                "inconclusive": inconclusive,
                "rejected": rejected,
                "cancelled": cancelled,
                "unresolved": unresolved,
            })
        return items

    @staticmethod
    def _empty_operations() -> Dict[str, Any]:
        return {
            "governanceCoverage": 0.0,
            "actionSuccessRate": None,
            "plannedActions": 0,
            "executedActions": 0,
            "failedActions": 0,
            "inconclusiveActions": 0,
            "unresolvedActions": 0,
            "rejectedActions": 0,
            "cancelledActions": 0,
            "tokenUsage": 0,
            "durationSeconds": 0.0,
        }

    @staticmethod
    def _overall_quality(days: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not days:
            return {
                "status": "failed", "error": "no_observations", "completeDays": 0,
                "failedDays": 0, "failedDates": [], "latestDayQuality": None,
            }
        failed = [day for day in days if day["quality"]["status"] != "complete"]
        return {
            "status": "failed" if failed else "complete",
            "error": failed[-1]["quality"].get("error") if failed else None,
            "completeDays": len(days) - len(failed),
            "failedDays": len(failed),
            "latestDataDate": days[-1]["dataDate"],
            "latestDay": days[-1]["quality"],
            "failedDates": [day["dataDate"] for day in failed],
            "latestDayQuality": days[-1]["quality"],
        }

    @staticmethod
    def _experiment_payload(experiment: Any) -> Dict[str, Any]:
        return {
            "experimentId": int(experiment.id),
            "brand": experiment.brand,
            "experimentName": experiment.experiment_name,
            "status": experiment.status,
            "sampleDataDate": experiment.sample_data_date.isoformat() if experiment.sample_data_date else None,
            "plannedStartDate": experiment.planned_start_date.isoformat() if experiment.planned_start_date else None,
            "plannedEndDate": experiment.planned_end_date.isoformat() if experiment.planned_end_date else None,
            "baselineStartDate": experiment.baseline_start_date.isoformat() if experiment.baseline_start_date else None,
            "baselineEndDate": experiment.baseline_end_date.isoformat() if experiment.baseline_end_date else None,
            "baselineLockedAt": experiment.baseline_locked_at.isoformat() if experiment.baseline_locked_at else None,
            "calculationVersion": ExperimentAnalyticsService._calculation_version(experiment),
            "startedAt": experiment.started_at.isoformat() if experiment.started_at else None,
            "endedAt": experiment.ended_at.isoformat() if experiment.ended_at else None,
        }

    @staticmethod
    def _calculation_version(experiment: Any) -> str:
        return str(experiment.calculation_version or "")

    @staticmethod
    def _json_object(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _subtract(left: Optional[float], right: Optional[float]) -> Optional[float]:
        return left - right if left is not None and right is not None else None
