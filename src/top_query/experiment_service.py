import datetime
import json
import logging
import math
import threading
from collections import defaultdict
from contextlib import ExitStack
from typing import Any, Dict, List, Optional
from sqlalchemy import and_, or_

from src.persistence import (
    SessionLocal,
    TopQueryActionPlan,
    TopQueryAgentEvent,
    TopQueryExperiment,
    TopQueryExperimentDailyMetric,
    TopQueryExperimentSample,
    TopQueryTask,
)

from .extractor import TopQueryDataExtractor
from src.governance.query_identity import (
    normalize_governance_query_key,
    normalize_query_key,
    normalize_query_text,
)
from src.governance.query_policy import (
    ExperimentValidationError,
    assert_queries_governance_allowed,
    assert_queries_governance_allowed_in_session,
    assert_query_governance_allowed,
)
from .task_lock import top_query_brand_governance_lock
from .task_identity import select_task_winner


EXPERIMENT_CALCULATION_VERSION = "top-query-ab-test-v2"


def validate_running_experiment_state(experiment: Dict[str, Any]) -> Optional[str]:
    calculation_version = str(experiment.get("calculationVersion") or "")
    if calculation_version != EXPERIMENT_CALCULATION_VERSION:
        return "unsupported_calculation_version"
    if not experiment.get("baselineLockedAt"):
        return "activation_baseline_invalid"

    samples = experiment.get("samples")
    if not isinstance(samples, list) or len(samples) != 40:
        return "activation_baseline_invalid"
    if sum(1 for sample in samples if sample.get("groupType") == "treatment") != 20:
        return "activation_baseline_invalid"
    if sum(1 for sample in samples if sample.get("groupType") == "control") != 20:
        return "activation_baseline_invalid"
    for sample in samples:
        try:
            observed_days = int(sample.get("baselineObservedDays"))
            search_uv = float(sample.get("baselineSearchUv"))
            search_pv = float(sample.get("baselineSearchPv"))
            click_uv = float(sample.get("baselineClickUv"))
            purchase_uv = float(sample.get("baselinePurchaseUv"))
            ctr = float(sample.get("baselineCtr"))
            cvr = float(sample.get("baselineCvr"))
        except (TypeError, ValueError):
            return "activation_baseline_invalid"
        if (
            observed_days != 7
            or not all(math.isfinite(value) for value in (
                search_uv,
                search_pv,
                click_uv,
                purchase_uv,
                ctr,
                cvr,
            ))
            or search_uv <= 0
            or search_pv < 0
            or click_uv < 0
            or purchase_uv < 0
            or ctr < 0
            or cvr < 0
            or not math.isclose(
                ctr,
                click_uv / search_uv,
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
            or not math.isclose(
                cvr,
                purchase_uv / search_uv,
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
        ):
            return "activation_baseline_invalid"
    return None


def build_experiment_quality(
    *,
    data_date: datetime.date,
    data_quality: Optional[Dict[str, Any]] = None,
    treatment_task_count: int = 0,
    control_task_count: int = 0,
    control_action_count: int = 0,
    contamination_count: int = 0,
    action_failed: int = 0,
    action_inconclusive: int = 0,
    existing_unresolved: int = 0,
    retry_count: int = 0,
    force_failed: bool = False,
    calculation_version: str = EXPERIMENT_CALCULATION_VERSION,
) -> Dict[str, Any]:
    source = data_quality or {}
    expected_valid = isinstance(source.get("expected"), (int, float)) and not isinstance(source.get("expected"), bool)
    observed_valid = isinstance(source.get("observed"), (int, float)) and not isinstance(source.get("observed"), bool)
    expected = int(source["expected"]) if expected_valid else 40
    observed = int(source["observed"]) if observed_valid else 0
    missing_keys = source.get("missingKeys")
    missing_keys_valid = isinstance(missing_keys, list)
    missing_keys = list(missing_keys) if missing_keys_valid else []
    unresolved = max(0, 20 - int(treatment_task_count)) + max(0, int(existing_unresolved))
    quality = {
        "status": "complete" if (
            not force_failed
            and source.get("status") == "complete"
            and expected_valid
            and observed_valid
            and missing_keys_valid
        ) else "failed",
        "expected": expected,
        "observed": observed,
        "expectedSamples": expected,
        "observedSamples": observed,
        "missingKeys": missing_keys,
        "treatmentTaskCount": int(treatment_task_count),
        "controlTaskCount": int(control_task_count),
        "controlActionCount": int(control_action_count),
        "contaminationCount": int(contamination_count),
        "actionFailed": int(action_failed),
        "actionInconclusive": int(action_inconclusive),
        "unresolved": unresolved,
        "retryCount": int(retry_count),
        "dataDate": data_date.isoformat(),
        "calculationVersion": calculation_version,
    }
    if not experiment_quality_gate_passed(quality):
        quality["status"] = "failed"
    return quality


def experiment_quality_gate_passed(quality: object) -> bool:
    if not isinstance(quality, dict) or quality.get("status") != "complete":
        return False

    def exact_count(key: str, expected: int) -> bool:
        value = quality.get(key)
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value == expected

    missing_keys = quality.get("missingKeys")
    return (
        isinstance(missing_keys, list)
        and not missing_keys
        and exact_count("expectedSamples", 40)
        and exact_count("observedSamples", 40)
        and exact_count("treatmentTaskCount", 20)
        and exact_count("controlTaskCount", 0)
        and exact_count("controlActionCount", 0)
        and exact_count("contaminationCount", 0)
        and exact_count("actionFailed", 0)
        and exact_count("actionInconclusive", 0)
        and exact_count("unresolved", 0)
    )


class TopQueryExperimentService:
    CALCULATION_VERSION = EXPERIMENT_CALCULATION_VERSION
    _activation_locks: Dict[int, threading.Lock] = {}
    _activation_locks_guard = threading.Lock()

    def __init__(self, extractor: Optional[TopQueryDataExtractor] = None):
        self.extractor = extractor or TopQueryDataExtractor()

    def create_or_replace_draft(
        self,
        brand: str,
        created_by: str,
        experiment_name: str,
        treatment_queries: List[str],
        control_queries: List[str],
        today: Optional[datetime.date] = None,
        planned_end_date: Optional[datetime.date] = None,
    ) -> Dict[str, Any]:
        today = today or datetime.date.today()
        sample_data_date = today - datetime.timedelta(days=1)
        planned_start_date = today + datetime.timedelta(days=1)
        planned_end_date = planned_end_date or (planned_start_date + datetime.timedelta(days=6))
        baseline_start_date = planned_start_date - datetime.timedelta(days=7)
        baseline_end_date = planned_start_date - datetime.timedelta(days=1)
        db = SessionLocal()
        try:
            active = db.query(TopQueryExperiment).filter(
                TopQueryExperiment.brand == brand,
                TopQueryExperiment.status.in_(["scheduled", "running"]),
            ).first()
            if active:
                state_label = "运行中" if active.status == "running" else "已排期"
                raise ExperimentValidationError(f"当前品牌存在{state_label}的实验，不能替换样本")
        finally:
            db.close()
        snapshot = self.extractor.get_query_daily_snapshot_strict(brand, sample_data_date)
        if snapshot.get("status") != "complete":
            if snapshot.get("status") == "partial":
                raise ExperimentValidationError("基线快照不完整")
            raise ExperimentValidationError(f"基线快照抽取失败: {snapshot.get('error') or '未知错误'}")
        query_map = {
            normalize_query_key(row.get("query")): row
            for row in snapshot.get("items") or []
            if normalize_query_key(row.get("query"))
        }
        normalized_treatment = self._normalize_query_list(treatment_queries)
        normalized_control = self._normalize_query_list(control_queries)
        self._validate_sample_membership(brand, query_map, normalized_treatment, normalized_control)
        baseline_map = query_map

        db = SessionLocal()
        try:
            draft = db.query(TopQueryExperiment).filter(
                TopQueryExperiment.brand == brand,
                TopQueryExperiment.status == "draft",
            ).order_by(TopQueryExperiment.id.desc()).first()
            if not draft:
                draft = TopQueryExperiment(
                    brand=brand,
                    status="draft",
                )
                db.add(draft)
                db.flush()
            draft.experiment_name = experiment_name # type: ignore
            draft.sample_data_date = sample_data_date # type: ignore
            draft.planned_start_date = planned_start_date # type: ignore
            draft.planned_end_date = planned_end_date # type: ignore
            draft.baseline_start_date = baseline_start_date # type: ignore
            draft.baseline_end_date = baseline_end_date # type: ignore
            draft.baseline_locked_at = None # type: ignore
            draft.calculation_version = EXPERIMENT_CALCULATION_VERSION # type: ignore
            draft.created_by = created_by # type: ignore

            db.query(TopQueryExperimentSample).filter(
                TopQueryExperimentSample.experiment_id == draft.id
            ).delete()

            for group_type, queries in (
                ("treatment", normalized_treatment),
                ("control", normalized_control),
            ):
                for query_text in queries:
                    query_key = normalize_query_key(query_text)
                    row = baseline_map[query_key]
                    locked_query_text = normalize_query_text(row.get("query"))
                    db.add(TopQueryExperimentSample(
                        experiment_id=draft.id,
                        brand=brand,
                        query_text=locked_query_text,
                        group_type=group_type,
                        sample_rank=int(row.get("query_rank") or 0),
                        sample_search_uv=int(float(row.get("search_uv") or 0)),
                        sample_search_pv=int(float(row.get("search_pv") or 0)),
                        sample_ctr=float(row.get("ctr") or 0),
                        sample_cvr=float(row.get("cvr") or 0),
                        sample_avg_ctr=float(row.get("avg_ctr") or 0),
                        sample_avg_cvr=float(row.get("avg_cvr") or 0),
                        sample_query_pv_share=float(row.get("query_pv_share") or 0),
                        baseline_observed_days=None,
                        baseline_search_uv=None,
                        baseline_search_pv=None,
                        baseline_click_uv=None,
                        baseline_purchase_uv=None,
                        baseline_ctr=None,
                        baseline_cvr=None,
                    ))

            db.commit()
            return self.get_experiment_detail(int(draft.id))
        finally:
            db.close()

    def schedule_experiment(
        self,
        experiment_id: int,
        today: Optional[datetime.date] = None,
    ) -> Dict[str, Any]:
        today = today or datetime.date.today()
        db = SessionLocal()
        try:
            experiment = self._get_experiment(db, experiment_id)
            if experiment.status != "draft":
                raise ExperimentValidationError(
                    f"实验只能从 draft 进入 scheduled，当前状态: {experiment.status}"
                )
            samples = self._get_samples(db, experiment_id)
            self._validate_lifecycle_ready(experiment, samples, draft_date=today)
            self._validate_balancing(samples)
            conflict = db.query(TopQueryExperiment).filter(
                TopQueryExperiment.brand == experiment.brand,
                TopQueryExperiment.id != experiment.id,
                TopQueryExperiment.status.in_(["scheduled", "running"]),
            ).first()
            if conflict:
                raise ExperimentValidationError("当前品牌已有排期中或运行中的实验")
            experiment.status = "scheduled" # type: ignore
            db.commit()
            return self.get_experiment_detail(experiment_id)
        finally:
            db.close()

    def activate_scheduled_experiment(
        self,
        experiment_id: int,
        *,
        execution_date: datetime.date,
        governance_service=None,
    ) -> Dict[str, Any]:
        with self._activation_locks_guard:
            activation_lock = self._activation_locks.setdefault(
                int(experiment_id),
                threading.Lock(),
            )
        with activation_lock:
            return self._activate_scheduled_experiment_locked(
                experiment_id,
                execution_date=execution_date,
                governance_service=governance_service,
            )

    def _activate_scheduled_experiment_locked(
        self,
        experiment_id: int,
        *,
        execution_date: datetime.date,
        governance_service=None,
    ) -> Dict[str, Any]:
        data_date = execution_date - datetime.timedelta(days=1)
        db = SessionLocal()
        try:
            experiment = self._get_experiment(db, experiment_id)
            if (
                experiment.status == "running"
                and self._calculation_version(experiment) == EXPERIMENT_CALCULATION_VERSION
                and experiment.baseline_locked_at is not None
            ):
                return self._build_reused_activation(db, experiment, data_date)
            if experiment.status != "scheduled":
                return self._activation_failure(
                    experiment_id,
                    str(experiment.brand),
                    "activation_state_conflict",
                )
            if self._calculation_version(experiment) != EXPERIMENT_CALCULATION_VERSION:
                return self._activation_failure(
                    experiment_id,
                    str(experiment.brand),
                    "activation_legacy_not_supported",
                )
            samples = self._get_samples(db, experiment_id)
            try:
                self._validate_lifecycle_ready(
                    experiment,
                    samples,
                    draft_date=execution_date - datetime.timedelta(days=1),
                )
            except ExperimentValidationError:
                return self._activation_failure(
                    experiment_id,
                    str(experiment.brand),
                    "activation_sample_invalid",
                )
            brand = str(experiment.brand)
            planned_start_date = experiment.planned_start_date
            baseline_start_date = experiment.baseline_start_date
            baseline_end_date = experiment.baseline_end_date
            sample_queries = [str(sample.query_text) for sample in samples]
        finally:
            db.close()

        if not planned_start_date or not baseline_start_date or not baseline_end_date:
            return self._activation_failure(experiment_id, brand, "baseline_window_incomplete")
        expected_baseline_start = planned_start_date - datetime.timedelta(days=7)
        expected_baseline_end = planned_start_date - datetime.timedelta(days=1)
        if (
            baseline_start_date != expected_baseline_start
            or baseline_end_date != expected_baseline_end
        ):
            return self._activation_failure(experiment_id, brand, "baseline_window_incomplete")

        try:
            baseline_result = self.extractor.get_query_baseline_window_strict(
                brand,
                planned_start_date,
                sample_queries,
            )
        except Exception as exc:
            logging.error(
                "experiment baseline extraction failed error_type=%s",
                type(exc).__name__,
            )
            return self._activation_failure(
                experiment_id,
                brand,
                "baseline_source_query_failed",
            )
        baseline_rows, baseline_error = self._validate_baseline_evidence(
            brand,
            sample_queries,
            baseline_start_date,
            baseline_end_date,
            baseline_result,
        )
        if baseline_error:
            return self._activation_failure(experiment_id, brand, baseline_error)

        try:
            daily_result = self.extractor.get_query_daily_metrics_strict(
                brand,
                data_date,
                sample_queries,
            )
        except Exception as exc:
            logging.error(
                "experiment daily snapshot extraction failed error_type=%s",
                type(exc).__name__,
            )
            return self._activation_failure(
                experiment_id,
                brand,
                "daily_snapshot_incomplete",
            )
        daily_rows, daily_error = self._validate_daily_evidence(
            brand,
            sample_queries,
            daily_result,
        )
        if daily_error:
            return self._activation_failure(experiment_id, brand, daily_error)

        if governance_service is None:
            from .service import TopQueryGovernanceService
            governance_service = TopQueryGovernanceService(extractor=self.extractor)

        lock_stack = ExitStack()
        try:
            lock_stack.enter_context(top_query_brand_governance_lock(brand))
            db = SessionLocal()
        except Exception:
            lock_stack.close()
            raise
        try:
            claimed = db.query(TopQueryExperiment).filter(
                TopQueryExperiment.id == experiment_id,
                TopQueryExperiment.status == "scheduled",
                TopQueryExperiment.calculation_version == EXPERIMENT_CALCULATION_VERSION,
                TopQueryExperiment.baseline_locked_at.is_(None),
            ).update({
                TopQueryExperiment.status: "running",
                TopQueryExperiment.started_at: datetime.datetime.combine(
                    execution_date,
                    datetime.time.min,
                ),
            }, synchronize_session=False)
            if claimed != 1:
                db.rollback()
                current = self._get_experiment(db, experiment_id)
                if current.status == "running" and current.baseline_locked_at is not None:
                    return self._build_reused_activation(db, current, data_date)
                return self._activation_failure(experiment_id, brand, "activation_state_conflict")

            db.expire_all()
            experiment = self._get_experiment(db, experiment_id)
            samples = self._get_samples(db, experiment_id)
            sample_by_key = {
                normalize_governance_query_key(brand, sample.query_text): sample
                for sample in samples
            }
            if len(samples) != 40 or len(sample_by_key) != 40:
                raise ExperimentValidationError("invalid_experiment_sample_shape")
            if any(
                value is not None
                for sample in samples
                for value in (
                    sample.baseline_observed_days,
                    sample.baseline_search_uv,
                    sample.baseline_search_pv,
                    sample.baseline_click_uv,
                    sample.baseline_purchase_uv,
                    sample.baseline_ctr,
                    sample.baseline_cvr,
                )
            ):
                raise ExperimentValidationError("baseline_already_partially_written")

            for query_key, sample in sample_by_key.items():
                baseline = baseline_rows[query_key]
                sample.baseline_observed_days = 7 # type: ignore
                sample.baseline_search_uv = int(baseline["searchUv"]) # type: ignore
                sample.baseline_search_pv = int(baseline["searchPv"]) # type: ignore
                sample.baseline_click_uv = float(baseline["clickUv"]) # type: ignore
                sample.baseline_purchase_uv = float(baseline["purchaseUv"]) # type: ignore
                sample.baseline_ctr = float(baseline["clickUv"]) / float(baseline["searchUv"]) # type: ignore
                sample.baseline_cvr = float(baseline["purchaseUv"]) / float(baseline["searchUv"]) # type: ignore

            locked_daily_rows = []
            for query_key, sample in sample_by_key.items():
                row = dict(daily_rows[query_key])
                row["query"] = str(sample.query_text)
                locked_daily_rows.append(row)
            self._persist_initial_snapshot_metrics(
                db,
                experiment,
                samples,
                data_date,
                locked_daily_rows,
            )
            treatment_rows = [
                row
                for row in locked_daily_rows
                if sample_by_key[normalize_governance_query_key(brand, row.get("query"))].group_type == "treatment"
            ]
            control_rows = [
                row
                for row in locked_daily_rows
                if sample_by_key[normalize_governance_query_key(brand, row.get("query"))].group_type == "control"
            ]
            task_result = governance_service.create_experiment_tasks_in_session(
                db,
                experiment_id=experiment_id,
                brand=brand,
                data_date=data_date,
                treatment_rows=treatment_rows,
            )
            task_ids = list(task_result.get("task_ids") or [])
            if len(task_ids) != 20 or len(set(task_ids)) != 20:
                raise ExperimentValidationError("activation_task_count_invalid")
            experiment.baseline_locked_at = datetime.datetime.now() # type: ignore
            db.flush()
            snapshot = self._activation_snapshot(
                experiment_id,
                brand,
                data_date,
                treatment_rows,
                control_rows,
            )
            db.commit()
            return {
                "status": "success",
                "reused": False,
                "experimentId": experiment_id,
                "brand": brand,
                "dataDate": data_date.isoformat(),
                "taskIds": task_ids,
                "snapshot": snapshot,
            }
        except Exception as exc:
            db.rollback()
            logging.error(
                "experiment activation transaction failed error_type=%s",
                type(exc).__name__,
            )
            return self._activation_failure(experiment_id, brand, "activation_write_failed")
        finally:
            try:
                db.close()
            finally:
                lock_stack.close()

    @staticmethod
    def _activation_failure(experiment_id: int, brand: str, error: str) -> Dict[str, Any]:
        return {
            "status": "partial_failed",
            "experimentId": experiment_id,
            "brand": brand,
            "error": error,
            "taskIds": [],
        }

    @staticmethod
    def _activation_snapshot(
        experiment_id: int,
        brand: str,
        data_date: datetime.date,
        treatment_rows: List[Dict[str, Any]],
        control_rows: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "experimentId": experiment_id,
            "brand": brand,
            "dataDate": data_date.isoformat(),
            "dataQuality": {
                "status": "complete",
                "sourceStatus": "complete",
                "expected": 40,
                "observed": 40,
                "missingKeys": [],
                "error": None,
            },
            "treatmentRows": treatment_rows,
            "controlRows": control_rows,
        }

    @classmethod
    def _validate_baseline_evidence(
        cls,
        brand: str,
        sample_queries: List[str],
        baseline_start_date: datetime.date,
        baseline_end_date: datetime.date,
        result: Dict[str, Any],
    ) -> tuple[Dict[str, Dict[str, Any]], Optional[str]]:
        source_error = str(result.get("error") or "")
        if result.get("status") != "complete":
            if source_error in {
                "baseline_source_query_failed",
                "baseline_zero_search_uv",
                "baseline_invalid_metric",
                "baseline_window_incomplete",
            }:
                return {}, source_error
            return {}, "baseline_window_incomplete"
        if (
            result.get("baselineStartDate") != baseline_start_date.isoformat()
            or result.get("baselineEndDate") != baseline_end_date.isoformat()
            or result.get("expectedDays") != 7
        ):
            return {}, "baseline_window_incomplete"
        expected_keys = {
            normalize_governance_query_key(brand, query)
            for query in sample_queries
        }
        row_by_key: Dict[str, Dict[str, Any]] = {}
        for row in result.get("items") or []:
            key = normalize_governance_query_key(brand, row.get("query"))
            if not key or key not in expected_keys or key in row_by_key:
                return {}, "baseline_window_incomplete"
            if row.get("observedDays") != 7:
                return {}, "baseline_window_incomplete"
            try:
                search_uv = float(row.get("searchUv"))
                search_pv = float(row.get("searchPv"))
                click_uv = float(row.get("clickUv"))
                purchase_uv = float(row.get("purchaseUv"))
            except (TypeError, ValueError):
                return {}, "baseline_invalid_metric"
            if search_uv == 0:
                return {}, "baseline_zero_search_uv"
            if (
                not all(math.isfinite(value) for value in (search_uv, search_pv, click_uv, purchase_uv))
                or search_uv < 0
                or search_pv < 0
                or click_uv < 0
                or purchase_uv < 0
                or not search_uv.is_integer()
                or not search_pv.is_integer()
            ):
                return {}, "baseline_invalid_metric"
            row_by_key[key] = row
        if len(expected_keys) != 40 or set(row_by_key) != expected_keys:
            return {}, "baseline_window_incomplete"
        return row_by_key, None

    @classmethod
    def _validate_daily_evidence(
        cls,
        brand: str,
        sample_queries: List[str],
        result: Dict[str, Any],
    ) -> tuple[Dict[str, Dict[str, Any]], Optional[str]]:
        if result.get("status") != "complete":
            return {}, "daily_snapshot_incomplete"
        expected_keys = {
            normalize_governance_query_key(brand, query)
            for query in sample_queries
        }
        row_by_key: Dict[str, Dict[str, Any]] = {}
        for row in result.get("items") or []:
            key = normalize_governance_query_key(brand, row.get("query"))
            if not key or key not in expected_keys or key in row_by_key:
                return {}, "daily_snapshot_incomplete"
            try:
                values = [
                    float(row.get("search_uv")),
                    float(row.get("search_pv")),
                    float(row.get("click_uv")),
                    float(row.get("purchase_uv")),
                    float(row.get("ctr")),
                    float(row.get("cvr")),
                ]
            except (TypeError, ValueError):
                return {}, "daily_snapshot_incomplete"
            if (
                not all(math.isfinite(value) and value >= 0 for value in values)
                or values[0] <= 0
                or not values[0].is_integer()
                or not values[1].is_integer()
            ):
                return {}, "daily_snapshot_incomplete"
            row_by_key[key] = row
        if len(expected_keys) != 40 or set(row_by_key) != expected_keys:
            return {}, "daily_snapshot_incomplete"
        return row_by_key, None

    def _build_reused_activation(
        self,
        db,
        experiment: TopQueryExperiment,
        data_date: datetime.date,
    ) -> Dict[str, Any]:
        experiment_id = int(experiment.id)
        brand = str(experiment.brand)
        samples = self._get_samples(db, experiment_id)
        sample_by_key = {
            normalize_governance_query_key(brand, sample.query_text): sample
            for sample in samples
        }
        treatment_keys = {
            key for key, sample in sample_by_key.items() if sample.group_type == "treatment"
        }
        control_keys = set(sample_by_key) - treatment_keys
        state_error = validate_running_experiment_state(
            self._serialize_experiment(experiment, samples)
        )
        if state_error:
            return self._activation_failure(experiment_id, brand, state_error)
        if len(sample_by_key) != 40 or len(treatment_keys) != 20 or len(control_keys) != 20:
            return self._activation_failure(experiment_id, brand, "activation_baseline_invalid")
        metrics = db.query(TopQueryExperimentDailyMetric).filter(
            TopQueryExperimentDailyMetric.experiment_id == experiment_id,
            TopQueryExperimentDailyMetric.data_date == data_date,
        ).all()
        metric_by_key = {
            normalize_governance_query_key(brand, metric.query_text): metric
            for metric in metrics
        }
        if (
            len(metrics) != 40
            or set(metric_by_key) != set(sample_by_key)
            or any(
                metric.observed_status != "observed"
                or metric.contamination_status != "clean"
                for metric in metrics
            )
        ):
            return self._activation_failure(experiment_id, brand, "activation_snapshot_invalid")
        active_tasks = [
            task
            for task in db.query(TopQueryTask).filter(
                TopQueryTask.brand == brand,
                TopQueryTask.data_date == data_date,
            ).all()
            if str(task.status or "") != "superseded"
        ]
        task_by_key = {
            normalize_governance_query_key(brand, task.query_text): task
            for task in active_tasks
        }
        if len(active_tasks) != 20 or set(task_by_key) != treatment_keys:
            return self._activation_failure(experiment_id, brand, "activation_task_count_invalid")

        rows_by_key: Dict[str, Dict[str, Any]] = {}
        for key, metric in metric_by_key.items():
            try:
                payload = json.loads(metric.raw_payload or "{}")
            except json.JSONDecodeError:
                payload = {}
            payload.pop("experimentSnapshot", None)
            payload.update({
                "query": str(metric.query_text),
                "query_rank": int(metric.query_rank or 0),
                "search_uv": int(metric.search_uv or 0),
                "search_pv": int(metric.search_pv or 0),
                "ctr": float(metric.ctr or 0),
                "cvr": float(metric.cvr or 0),
            })
            rows_by_key[key] = payload
        treatment_rows = [
            rows_by_key[normalize_governance_query_key(brand, sample.query_text)]
            for sample in samples
            if sample.group_type == "treatment"
        ]
        control_rows = [
            rows_by_key[normalize_governance_query_key(brand, sample.query_text)]
            for sample in samples
            if sample.group_type == "control"
        ]
        return {
            "status": "success",
            "reused": True,
            "experimentId": experiment_id,
            "brand": brand,
            "dataDate": data_date.isoformat(),
            "taskIds": [
                int(task_by_key[normalize_governance_query_key(brand, sample.query_text)].id)
                for sample in samples
                if sample.group_type == "treatment"
            ],
            "snapshot": self._activation_snapshot(
                experiment_id,
                brand,
                data_date,
                treatment_rows,
                control_rows,
            ),
        }

    def transition_scheduled_experiments(
        self,
        execution_date: datetime.date,
        governance_service=None,
        brands: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            query = db.query(TopQueryExperiment).filter(
                or_(
                    and_(
                        TopQueryExperiment.status == "scheduled",
                        TopQueryExperiment.planned_start_date <= execution_date,
                    ),
                    and_(
                        TopQueryExperiment.status == "running",
                        TopQueryExperiment.planned_start_date == execution_date,
                        TopQueryExperiment.calculation_version == EXPERIMENT_CALCULATION_VERSION,
                        TopQueryExperiment.baseline_locked_at.is_not(None),
                    ),
                ),
            )
            if brands is not None:
                if not brands:
                    rows = []
                else:
                    rows = query.filter(TopQueryExperiment.brand.in_(brands)).all()
            else:
                rows = query.all()
            due = [
                {
                    "id": int(row.id),
                    "brand": str(row.brand),
                    "calculationVersion": self._calculation_version(row),
                }
                for row in rows
            ]
        finally:
            db.close()

        ids: List[int] = []
        failures: List[Dict[str, Any]] = []
        activations: List[Dict[str, Any]] = []
        for item in due:
            experiment_id = int(item["id"])
            if item["calculationVersion"] != EXPERIMENT_CALCULATION_VERSION:
                failures.append({
                    "experimentId": experiment_id,
                    "brand": item["brand"],
                    "error": "unsupported_calculation_version",
                })
                continue
            activation = self.activate_scheduled_experiment(
                experiment_id,
                execution_date=execution_date,
                governance_service=governance_service,
            )
            if activation.get("status") == "success":
                ids.append(experiment_id)
                activations.append(activation)
            else:
                failures.append({
                    "experimentId": experiment_id,
                    "brand": item["brand"],
                    "error": activation.get("error") or "activation_write_failed",
                })
        return {"activatedIds": ids, "activations": activations, "failures": failures}

    def end_experiment(self, experiment_id: int, ended_at: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            experiment = self._get_experiment(db, experiment_id)
            if experiment.status == "ended":
                return self.get_experiment_detail(experiment_id)
            if experiment.status != "running":
                raise ExperimentValidationError(
                    f"实验只能从 running 进入 ended，当前状态: {experiment.status}"
                )
            experiment.status = "ended" # type: ignore
            experiment.ended_at = ended_at or datetime.datetime.now() # type: ignore
            db.commit()
            return self.get_experiment_detail(experiment_id)
        finally:
            db.close()

    def cancel_experiment(self, experiment_id: int) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            experiment = self._get_experiment(db, experiment_id)
            if experiment.status == "cancelled":
                return self.get_experiment_detail(experiment_id)
            if experiment.status == "running":
                raise ExperimentValidationError("运行中实验不能取消，请按实验结束流程处理")
            if experiment.status not in {"draft", "scheduled"}:
                raise ExperimentValidationError(
                    f"实验只能在运行前取消，当前状态: {experiment.status}"
                )
            experiment.status = "cancelled" # type: ignore
            db.commit()
            return self.get_experiment_detail(experiment_id)
        finally:
            db.close()

    def get_active_experiment_for_brand(
        self,
        brand: str,
        execution_date: Optional[datetime.date] = None,
    ) -> Optional[Dict[str, Any]]:
        execution_date = execution_date or datetime.date.today()
        db = SessionLocal()
        try:
            experiment = db.query(TopQueryExperiment).filter(
                TopQueryExperiment.brand == brand,
                TopQueryExperiment.status == "running",
                TopQueryExperiment.planned_start_date <= execution_date,
            ).order_by(TopQueryExperiment.id.desc()).first()
            if not experiment:
                return None
            samples = self._get_samples(db, int(experiment.id))
            return self._serialize_experiment(experiment, samples)
        finally:
            db.close()

    def get_treatment_query_scope(
        self,
        brand: str,
        execution_date: Optional[datetime.date] = None,
    ) -> Optional[Dict[str, Any]]:
        active_experiment = self.get_active_experiment_for_brand(brand, execution_date=execution_date)
        if not active_experiment:
            return None
        queries = {
            str(sample.get("queryText") or "").strip()
            for sample in active_experiment.get("samples", [])
            if sample.get("groupType") == "treatment" and str(sample.get("queryText") or "").strip()
        }
        return {
            "experimentId": active_experiment.get("id"),
            "queries": queries,
        }

    def get_query_experiment_scope(
        self,
        brand: str,
        query_text: str,
        execution_date: Optional[datetime.date] = None,
    ) -> Optional[Dict[str, Any]]:
        active_experiment = self.get_active_experiment_for_brand(brand, execution_date=execution_date)
        if not active_experiment:
            return None
        query_key = normalize_governance_query_key(brand, query_text)
        for sample in active_experiment.get("samples", []):
            if normalize_governance_query_key(brand, sample.get("queryText")) == query_key:
                return {
                    "experimentId": active_experiment.get("id"),
                    "groupType": sample.get("groupType"),
                    "queryText": sample.get("queryText"),
                }
        return None

    def list_brand_experiments(
        self,
        brand: str,
        *,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            query = db.query(TopQueryExperiment).filter(
                TopQueryExperiment.brand == brand
            )
            if status:
                query = query.filter(TopQueryExperiment.status == status)
            total = query.count()
            rows = query.order_by(TopQueryExperiment.id.desc()).offset(
                (page - 1) * page_size
            ).limit(page_size).all()
            current = None
            history = []
            for row in rows:
                samples = self._get_samples(db, int(row.id))
                payload = self._serialize_experiment(row, samples)
                if current is None and row.status in {"draft", "scheduled", "running"}:
                    current = payload
                history.append(payload)
            return {
                "brand": brand,
                "current": current,
                "items": history,
                "total": total,
                "page": page,
                "pageSize": page_size,
            }
        finally:
            db.close()

    def list_history_summaries(
        self,
        brand: str,
        *,
        status: Optional[str] = None,
        start_date: Optional[datetime.date] = None,
        end_date: Optional[datetime.date] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            query = db.query(TopQueryExperiment).filter(TopQueryExperiment.brand == brand)
            if status:
                query = query.filter(TopQueryExperiment.status == status)
            if start_date:
                query = query.filter(TopQueryExperiment.planned_start_date >= start_date)
            if end_date:
                query = query.filter(TopQueryExperiment.planned_start_date <= end_date)
            total = query.count()
            rows = query.order_by(TopQueryExperiment.id.desc()).offset(
                (page - 1) * page_size
            ).limit(page_size).all()
            experiment_ids = [int(row.id) for row in rows]
            samples = db.query(TopQueryExperimentSample).filter(
                TopQueryExperimentSample.experiment_id.in_(experiment_ids)
            ).all() if experiment_ids else []
            metrics = db.query(TopQueryExperimentDailyMetric).filter(
                TopQueryExperimentDailyMetric.experiment_id.in_(experiment_ids)
            ).order_by(
                TopQueryExperimentDailyMetric.experiment_id.asc(),
                TopQueryExperimentDailyMetric.data_date.asc(),
                TopQueryExperimentDailyMetric.id.asc(),
            ).all() if experiment_ids else []
            task_ids = sorted({int(metric.governance_task_id) for metric in metrics if metric.governance_task_id})
            tasks = db.query(TopQueryTask).filter(TopQueryTask.id.in_(task_ids)).all() if task_ids else []
            actions = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.task_id.in_(task_ids)
            ).all() if task_ids else []
            counts: Dict[int, Dict[str, int]] = defaultdict(lambda: {"treatment": 0, "control": 0})
            samples_by_experiment: Dict[int, List[TopQueryExperimentSample]] = defaultdict(list)
            for sample in samples:
                counts[int(sample.experiment_id)][str(sample.group_type)] += 1
                samples_by_experiment[int(sample.experiment_id)].append(sample)
            metrics_by_experiment: Dict[int, List[TopQueryExperimentDailyMetric]] = defaultdict(list)
            for metric in metrics:
                metrics_by_experiment[int(metric.experiment_id)].append(metric)
            task_experiment_ids = {
                int(metric.governance_task_id): int(metric.experiment_id)
                for metric in metrics if metric.governance_task_id
            }
            tasks_by_experiment: Dict[int, List[TopQueryTask]] = defaultdict(list)
            actions_by_experiment: Dict[int, List[TopQueryActionPlan]] = defaultdict(list)
            for task in tasks:
                experiment_id = task_experiment_ids.get(int(task.id))
                if experiment_id is not None:
                    tasks_by_experiment[experiment_id].append(task)
            for action in actions:
                experiment_id = task_experiment_ids.get(int(action.task_id or 0))
                if experiment_id is not None:
                    actions_by_experiment[experiment_id].append(action)
            from .experiment_analytics import ExperimentAnalyticsService
            analytics = ExperimentAnalyticsService()
            quality_by_experiment = {
                int(row.id): analytics.evaluate_snapshot_quality({
                    "experiment": row,
                    "samples": samples_by_experiment[int(row.id)],
                    "metrics": metrics_by_experiment[int(row.id)],
                    "tasks": tasks_by_experiment[int(row.id)],
                    "actions": actions_by_experiment[int(row.id)],
                    "events": [],
                })
                for row in rows
            }
            items = []
            for row in rows:
                row_counts = counts[int(row.id)]
                items.append({
                    "id": int(row.id),
                    "brand": row.brand,
                    "experimentName": row.experiment_name,
                    "status": row.status,
                    "sampleDataDate": row.sample_data_date.isoformat() if row.sample_data_date else None,
                    "plannedStartDate": row.planned_start_date.isoformat() if row.planned_start_date else None,
                    "plannedEndDate": row.planned_end_date.isoformat() if row.planned_end_date else None,
                    "startedAt": row.started_at.isoformat() if row.started_at else None,
                    "endedAt": row.ended_at.isoformat() if row.ended_at else None,
                    "createdBy": row.created_by,
                    "treatmentCount": row_counts["treatment"],
                    "controlCount": row_counts["control"],
                    "quality": quality_by_experiment[int(row.id)],
                })
            current = next((item for item in items if item["status"] in {"draft", "scheduled", "running"}), None)
            return {"brand": brand, "current": current, "items": items, "total": total, "page": page, "pageSize": page_size}
        finally:
            db.close()

    def get_sample_pool(self, brand: str, today: Optional[datetime.date] = None, limit: int = 150) -> Dict[str, Any]:
        today = today or datetime.date.today()
        sample_data_date = today - datetime.timedelta(days=1)
        snapshot = self.extractor.get_query_daily_snapshot_strict(brand, sample_data_date)
        if snapshot.get("status") != "complete":
            raise ExperimentValidationError(f"样本池快照抽取失败: {snapshot.get('error') or '未知错误'}")
        merged_queries = []
        seen_governance_keys = set()
        for row in snapshot.get("items") or []:
            governance_key = normalize_governance_query_key(brand, row.get("query"))
            if not governance_key or governance_key in seen_governance_keys:
                continue
            seen_governance_keys.add(governance_key)
            merged_queries.append(row)
            if len(merged_queries) >= limit:
                break
        return {
            "brand": brand,
            "sampleDataDate": sample_data_date.isoformat(),
            "items": [
                {
                    "queryText": str(row.get("query") or "").strip(),
                    "governanceKey": normalize_governance_query_key(brand, row.get("query")),
                    "queryRank": int(row.get("query_rank") or 0),
                    "searchUv": int(float(row.get("search_uv") or 0)),
                    "searchPv": int(float(row.get("search_pv") or 0)),
                    "ctr": float(row.get("ctr") or 0),
                    "cvr": float(row.get("cvr") or 0),
                    "avgCtr": float(row.get("avg_ctr") or 0),
                    "avgCvr": float(row.get("avg_cvr") or 0),
                    "queryPvShare": float(row.get("query_pv_share") or 0),
                }
                for row in merged_queries
                if str(row.get("query") or "").strip()
            ],
        }

    def prepare_daily_snapshot(self, experiment_id: int, data_date: datetime.date) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            experiment = self._get_experiment(db, experiment_id)
            samples = self._get_samples(db, experiment_id)
            treatment = [sample for sample in samples if sample.group_type == "treatment"]
            control = [sample for sample in samples if sample.group_type == "control"]
            sample_by_key = {
                normalize_query_key(sample.query_text): sample
                for sample in samples
                if normalize_query_key(sample.query_text)
            }
            sample_shape_valid = (
                len(treatment) == 20
                and len(control) == 20
                and len(samples) == 40
                and len(sample_by_key) == 40
            )
            sample_queries = [str(sample.query_text) for sample in samples]
            if sample_shape_valid:
                extraction = self.extractor.get_query_daily_metrics_strict(
                    str(experiment.brand),
                    data_date,
                    sample_queries,
                )
            else:
                extraction = {
                    "status": "failed",
                    "items": [],
                    "missingKeys": sorted(sample_by_key),
                    "error": "invalid_experiment_sample_shape",
                }

            row_by_key: Dict[str, Dict[str, Any]] = {}
            duplicate_row_keys = set()
            for row in extraction.get("items") or []:
                key = normalize_query_key(row.get("query"))
                if not key or key not in sample_by_key:
                    continue
                if key in row_by_key:
                    duplicate_row_keys.add(key)
                    continue
                locked_row = dict(row)
                locked_row["query"] = str(sample_by_key[key].query_text)
                row_by_key[key] = locked_row

            missing_keys = sorted(set(sample_by_key) - set(row_by_key))
            source_status = str(extraction.get("status") or "failed")
            complete = (
                sample_shape_valid
                and source_status == "complete"
                and len(row_by_key) == 40
                and not duplicate_row_keys
                and not missing_keys
            )
            data_quality = {
                "status": "complete" if complete else "failed",
                "sourceStatus": source_status,
                "expected": 40,
                "observed": len(row_by_key),
                "missingKeys": missing_keys,
                "error": extraction.get("error"),
            }
            treatment_rows = [
                row_by_key[normalize_query_key(sample.query_text)]
                for sample in treatment
                if normalize_query_key(sample.query_text) in row_by_key
            ] if complete else []
            control_rows = [
                row_by_key[normalize_query_key(sample.query_text)]
                for sample in control
                if normalize_query_key(sample.query_text) in row_by_key
            ] if complete else []
            snapshot = {
                "experimentId": experiment_id,
                "brand": str(experiment.brand),
                "dataDate": data_date.isoformat(),
                "dataQuality": data_quality,
                "treatmentRows": treatment_rows,
                "controlRows": control_rows,
            }
            if self._ensure_day_finalized_marker(db, experiment, samples, data_date):
                self._append_prepare_quality_history(db, experiment_id, data_date, data_quality)
                db.commit()
                return snapshot
            if not complete:
                self._persist_failed_snapshot_metrics(db, experiment, samples, data_date, data_quality)
                db.commit()
                return snapshot
            self._persist_initial_snapshot_metrics(
                db,
                experiment,
                samples,
                data_date,
                treatment_rows + control_rows,
            )
            db.commit()
            return snapshot
        finally:
            db.close()

    @staticmethod
    def _persist_initial_snapshot_metrics(
        db,
        experiment: TopQueryExperiment,
        samples: List[TopQueryExperimentSample],
        data_date: datetime.date,
        rows: List[Dict[str, Any]],
    ) -> None:
        row_by_key = {
            normalize_query_key(row.get("query")): row
            for row in rows
            if normalize_query_key(row.get("query"))
        }
        for sample in samples:
            row = row_by_key[normalize_query_key(sample.query_text)]
            metric = db.query(TopQueryExperimentDailyMetric).filter(
                TopQueryExperimentDailyMetric.experiment_id == experiment.id,
                TopQueryExperimentDailyMetric.query_text == sample.query_text,
                TopQueryExperimentDailyMetric.data_date == data_date,
            ).first()
            is_new = metric is None
            if is_new:
                metric = TopQueryExperimentDailyMetric(
                    experiment_id=experiment.id,
                    brand=experiment.brand,
                    query_text=sample.query_text,
                    group_type=sample.group_type,
                    data_date=data_date,
                )
                db.add(metric)
            preserve_finalized = not is_new and TopQueryExperimentService._has_finalized_marker(metric)
            metric.observed_status = "observed" # type: ignore
            metric.query_rank = int(row.get("query_rank") or 0) # type: ignore
            metric.search_uv = int(float(row.get("search_uv") or 0)) # type: ignore
            metric.search_pv = int(float(row.get("search_pv") or 0)) # type: ignore
            metric.ctr = float(row.get("ctr") or 0) # type: ignore
            metric.cvr = float(row.get("cvr") or 0) # type: ignore
            if not preserve_finalized:
                metric.contamination_status = "clean" # type: ignore
                metric.contamination_reason = None # type: ignore
                metric.contaminated_from_date = None # type: ignore
                metric.governance_task_id = None # type: ignore
                metric.governance_status = None # type: ignore
                metric.action_count = 0 # type: ignore
                metric.auto_action_count = 0 # type: ignore
                metric.manual_action_count = 0 # type: ignore
            payload = dict(row)
            payload["experimentSnapshot"] = {
                "finalized": False,
                "snapshotPhase": "prepared",
                "experimentId": int(experiment.id),
                "brand": str(experiment.brand),
                "dataDate": data_date.isoformat(),
                "prepareQualityHistory": [],
            }
            metric.raw_payload = json.dumps(payload, ensure_ascii=False) # type: ignore

    @staticmethod
    def _has_finalized_marker(metric: TopQueryExperimentDailyMetric) -> bool:
        try:
            payload = json.loads(metric.raw_payload or "{}")
        except json.JSONDecodeError:
            return False
        marker = payload.get("experimentSnapshot") or {}
        if not isinstance(marker, dict):
            return False
        metric_data_date = metric.data_date.isoformat() if metric.data_date else None
        return (
            marker.get("finalized") is True
            and marker.get("snapshotPhase") == "finalized"
            and marker.get("experimentId") == metric.experiment_id
            and marker.get("brand") == metric.brand
            and marker.get("dataDate") == metric_data_date
        )

    @staticmethod
    def _append_metric_prepare_quality(
        metric: TopQueryExperimentDailyMetric,
        data_quality: Dict[str, Any],
    ) -> None:
        try:
            payload = json.loads(metric.raw_payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        marker = payload.setdefault("experimentSnapshot", {})
        history = marker.get("prepareQualityHistory")
        if not isinstance(history, list):
            history = []
        history.append(dict(data_quality))
        marker["prepareQualityHistory"] = history[-20:]
        payload["experimentSnapshot"] = marker
        metric.raw_payload = json.dumps(payload, ensure_ascii=False) # type: ignore

    @classmethod
    def _append_prepare_quality_history(
        cls,
        db,
        experiment_id: int,
        data_date: datetime.date,
        data_quality: Dict[str, Any],
    ) -> None:
        metrics = db.query(TopQueryExperimentDailyMetric).filter(
            TopQueryExperimentDailyMetric.experiment_id == experiment_id,
            TopQueryExperimentDailyMetric.data_date == data_date,
        ).all()
        for metric in metrics:
            cls._append_metric_prepare_quality(metric, data_quality)

    @classmethod
    def _ensure_day_finalized_marker(
        cls,
        db,
        experiment: TopQueryExperiment,
        samples: List[TopQueryExperimentSample],
        data_date: datetime.date,
    ) -> bool:
        metrics = db.query(TopQueryExperimentDailyMetric).filter(
            TopQueryExperimentDailyMetric.experiment_id == experiment.id,
            TopQueryExperimentDailyMetric.data_date == data_date,
        ).all()
        if len(metrics) != 40 or len(samples) != 40:
            return False
        if all(cls._has_finalized_marker(metric) for metric in metrics):
            return True

        sample_keys = {normalize_query_key(sample.query_text) for sample in samples}
        metric_keys = {normalize_query_key(metric.query_text) for metric in metrics}
        treatment_keys = {
            normalize_query_key(sample.query_text)
            for sample in samples
            if sample.group_type == "treatment"
        }
        control_count = sum(1 for sample in samples if sample.group_type == "control")
        if (
            len(sample_keys) != 40
            or metric_keys != sample_keys
            or len(treatment_keys) != 20
            or control_count != 20
            or any(metric.observed_status != "observed" for metric in metrics)
            or any(
                value is None
                for metric in metrics
                for value in (metric.search_uv, metric.search_pv, metric.ctr, metric.cvr)
            )
        ):
            return False
        active_tasks = [
            task
            for task in db.query(TopQueryTask).filter(
                TopQueryTask.brand == experiment.brand,
                TopQueryTask.data_date == data_date,
            ).all()
            if str(task.status or "").lower() != "superseded"
        ]
        active_task_keys = [normalize_query_key(task.query_text) for task in active_tasks]
        if (
            len(active_tasks) != 20
            or any(not key for key in active_task_keys)
            or len(set(active_task_keys)) != 20
            or set(active_task_keys) != treatment_keys
        ):
            return False
        task_by_key = {
            normalize_query_key(task.query_text): task
            for task in active_tasks
        }
        metric_by_key = {
            normalize_query_key(metric.query_text): metric
            for metric in metrics
        }
        if any(
            metric_by_key[key].governance_task_id != int(task_by_key[key].id)
            for key in treatment_keys
        ):
            return False

        for metric in metrics:
            try:
                payload = json.loads(metric.raw_payload or "{}")
            except json.JSONDecodeError:
                payload = {}
            payload["experimentSnapshot"] = {
                "finalized": True,
                "snapshotPhase": "finalized",
                "experimentId": int(experiment.id),
                "brand": str(experiment.brand),
                "dataDate": data_date.isoformat(),
                "prepareQualityHistory": [],
            }
            metric.raw_payload = json.dumps(payload, ensure_ascii=False) # type: ignore
        db.flush()
        return True

    @staticmethod
    def _persist_failed_snapshot_metrics(
        db,
        experiment: TopQueryExperiment,
        samples: List[TopQueryExperimentSample],
        data_date: datetime.date,
        data_quality: Dict[str, Any],
    ) -> None:
        metrics = []
        for sample in samples:
            metric = db.query(TopQueryExperimentDailyMetric).filter(
                TopQueryExperimentDailyMetric.experiment_id == experiment.id,
                TopQueryExperimentDailyMetric.query_text == sample.query_text,
                TopQueryExperimentDailyMetric.data_date == data_date,
            ).first()
            if not metric:
                metric = TopQueryExperimentDailyMetric(
                    experiment_id=experiment.id,
                    brand=experiment.brand,
                    query_text=sample.query_text,
                    group_type=sample.group_type,
                    data_date=data_date,
                )
                db.add(metric)
            elif TopQueryExperimentService._has_finalized_marker(metric):
                TopQueryExperimentService._append_metric_prepare_quality(metric, data_quality)
                continue
            metric.observed_status = "data_quality_failed" # type: ignore
            metric.contamination_status = "clean" # type: ignore
            metric.contamination_reason = None # type: ignore
            metric.contaminated_from_date = None # type: ignore
            metric.governance_task_id = None # type: ignore
            metric.governance_status = None # type: ignore
            metric.action_count = 0 # type: ignore
            metric.auto_action_count = 0 # type: ignore
            metric.manual_action_count = 0 # type: ignore
            metric.raw_payload = json.dumps({
                "dataQuality": data_quality,
                "experimentSnapshot": {
                    "finalized": False,
                    "snapshotPhase": "prepare_failed",
                    "experimentId": int(experiment.id),
                    "brand": str(experiment.brand),
                    "dataDate": data_date.isoformat(),
                    "prepareQualityHistory": [dict(data_quality)],
                },
            }, ensure_ascii=False) # type: ignore
            metrics.append(metric)
        db.flush()
        for metric in metrics:
            metric.query_rank = None # type: ignore
            metric.search_uv = None # type: ignore
            metric.search_pv = None # type: ignore
            metric.ctr = None # type: ignore
            metric.cvr = None # type: ignore

    @staticmethod
    def _merge_query_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[str, Dict[str, Any]] = {}
        for index, row in enumerate(rows):
            query_key = normalize_query_key(row.get("query"))
            if not query_key:
                continue
            item = grouped.setdefault(query_key, {
                "query": normalize_query_text(row.get("query")),
                "first_index": index,
                "search_uv": 0.0,
                "search_pv": 0.0,
                "click_uv": 0.0,
                "purchase_uv": 0.0,
                "avg_click_uv": 0.0,
                "avg_purchase_uv": 0.0,
            })
            uv = float(row.get("search_uv") or 0)
            item["search_uv"] += uv
            item["search_pv"] += float(row.get("search_pv") or 0)
            item["click_uv"] += float(row.get("ctr") or 0) * uv
            item["purchase_uv"] += float(row.get("cvr") or 0) * uv
            item["avg_click_uv"] += float(row.get("avg_ctr") or 0) * uv
            item["avg_purchase_uv"] += float(row.get("avg_cvr") or 0) * uv

        merged = sorted(grouped.values(), key=lambda item: (-item["search_pv"], item["first_index"]))
        total_pv = sum(float(item["search_pv"]) for item in merged)
        for rank, item in enumerate(merged, start=1):
            uv = float(item["search_uv"])
            item.update({
                "query_rank": rank,
                "ctr": float(item["click_uv"]) / uv if uv else 0.0,
                "cvr": float(item["purchase_uv"]) / uv if uv else 0.0,
                "avg_ctr": float(item["avg_click_uv"]) / uv if uv else 0.0,
                "avg_cvr": float(item["avg_purchase_uv"]) / uv if uv else 0.0,
                "query_pv_share": float(item["search_pv"]) / total_pv if total_pv else 0.0,
            })
        return merged

    def sync_daily_metrics(
        self,
        experiment_id: int,
        data_date: datetime.date,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            experiment = self._get_experiment(db, experiment_id)
            samples = self._get_samples(db, experiment_id)
            sample_map = {normalize_governance_query_key(experiment.brand, sample.query_text): sample for sample in samples}
            treatment_count = sum(1 for sample in samples if sample.group_type == "treatment")
            control_count = sum(1 for sample in samples if sample.group_type == "control")
            sample_shape_valid = (
                len(samples) == 40
                and len(sample_map) == 40
                and treatment_count == 20
                and control_count == 20
            )
            sample_queries = [sample.query_text for sample in samples]
            if snapshot is not None:
                identity_matches = (
                    snapshot.get("experimentId") == experiment_id
                    and snapshot.get("brand") == experiment.brand
                    and snapshot.get("dataDate") == data_date.isoformat()
                )
                if not identity_matches:
                    data_quality = {
                        "status": "failed",
                        "sourceStatus": "failed",
                        "expected": 40,
                        "observed": 0,
                        "missingKeys": sorted(sample_map),
                        "error": "snapshot_identity_mismatch",
                    }
                    self._persist_failed_snapshot_metrics(db, experiment, samples, data_date, data_quality)
                    db.commit()
                    return {
                        "experimentId": experiment_id,
                        "dataDate": data_date.isoformat(),
                        "observedCount": 0,
                        "sampleCount": len(samples),
                        "dataQuality": data_quality,
                        "experimentQuality": self._build_early_experiment_quality(
                            db, experiment, samples, data_date, data_quality
                        ),
                    }
                data_quality = snapshot.get("dataQuality") or {}
                if data_quality.get("status") != "complete":
                    return {
                        "experimentId": experiment_id,
                        "dataDate": data_date.isoformat(),
                        "dataQuality": data_quality,
                        "experimentQuality": self._build_early_experiment_quality(
                            db, experiment, samples, data_date, data_quality
                        ),
                    }
                extraction = None
                treatment_rows = snapshot.get("treatmentRows") or []
                control_rows = snapshot.get("controlRows") or []
                rows = treatment_rows + control_rows
            elif hasattr(self.extractor, "get_query_daily_metrics_strict"):
                extraction = self.extractor.get_query_daily_metrics_strict(
                    experiment.brand,
                    data_date,
                    sample_queries,
                )
                rows = extraction.get("items") or []
            else:
                extraction = None
                rows = self.extractor.get_query_daily_metrics(
                    experiment.brand,
                    data_date,
                    sample_queries,
                )
            row_map = {
                normalize_governance_query_key(experiment.brand, row.get("query")): row
                for row in rows
                if normalize_governance_query_key(experiment.brand, row.get("query"))
            }
            row_keys = [normalize_governance_query_key(experiment.brand, row.get("query")) for row in rows if normalize_governance_query_key(experiment.brand, row.get("query"))]
            expected_keys = set(sample_map)
            observed_keys = set(row_keys) & expected_keys
            snapshot_query_valid = True
            if snapshot is not None:
                treatment_keys = {
                    normalize_governance_query_key(experiment.brand, sample.query_text)
                    for sample in samples
                    if sample.group_type == "treatment"
                }
                control_keys = expected_keys - treatment_keys
                snapshot_treatment_keys = [
                    normalize_governance_query_key(experiment.brand, row.get("query"))
                    for row in treatment_rows
                    if normalize_governance_query_key(experiment.brand, row.get("query"))
                ]
                snapshot_control_keys = [
                    normalize_governance_query_key(experiment.brand, row.get("query"))
                    for row in control_rows
                    if normalize_governance_query_key(experiment.brand, row.get("query"))
                ]
                snapshot_query_valid = (
                    len(row_keys) == 40
                    and len(set(row_keys)) == 40
                    and set(row_keys) == expected_keys
                    and len(snapshot_treatment_keys) == 20
                    and set(snapshot_treatment_keys) == treatment_keys
                    and len(snapshot_control_keys) == 20
                    and set(snapshot_control_keys) == control_keys
                )
            source_status = str(extraction.get("status") or "failed") if extraction is not None else (
                "complete" if len(row_map) == len(sample_map) else "partial"
            )
            if (
                source_status != "complete"
                or not sample_shape_valid
                or set(row_map) != expected_keys
                or not snapshot_query_valid
            ):
                data_quality = {
                    "status": "failed",
                    "sourceStatus": source_status,
                    "expected": 40,
                    "observed": len(observed_keys),
                    "missingKeys": sorted(expected_keys - observed_keys),
                    "error": (
                        "invalid_experiment_sample_shape"
                        if not sample_shape_valid
                        else "snapshot_query_mismatch"
                        if snapshot is not None and not snapshot_query_valid
                        else extraction.get("error") if extraction is not None else None
                    ),
                }
                self._persist_failed_snapshot_metrics(db, experiment, samples, data_date, data_quality)
                db.commit()
                return {
                    "experimentId": experiment_id,
                    "dataDate": data_date.isoformat(),
                    "observedCount": len(observed_keys),
                    "sampleCount": len(samples),
                    "dataQuality": data_quality,
                    "experimentQuality": self._build_early_experiment_quality(
                        db, experiment, samples, data_date, data_quality
                    ),
                }
            governance_tasks = db.query(TopQueryTask).filter(
                TopQueryTask.brand == experiment.brand,
                TopQueryTask.data_date == data_date,
            ).all()
            governance_tasks = [
                task for task in governance_tasks
                if str(task.status or "") != "superseded"
                and normalize_governance_query_key(experiment.brand, task.query_text) in sample_map
            ]
            all_task_ids = [int(task.id) for task in governance_tasks if task.id]
            action_task_ids = {
                int(task_id) for (task_id,) in db.query(TopQueryActionPlan.task_id).filter(
                    TopQueryActionPlan.task_id.in_(all_task_ids)
                ).distinct().all()
            } if all_task_ids else set()
            grouped_tasks: Dict[str, List[TopQueryTask]] = defaultdict(list)
            for task in governance_tasks:
                grouped_tasks[normalize_governance_query_key(experiment.brand, task.query_text)].append(task)
            task_by_query = {
                key: select_task_winner(tasks, action_task_ids)
                for key, tasks in grouped_tasks.items()
            }
            query_task_ids = [int(task.id) for task in task_by_query.values() if task and task.id]
            action_summary_by_task: Dict[int, Dict[str, int]] = {}
            frozen_actions_by_task: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
            action_summary = {
                "planned": 0,
                "automatic": 0,
                "manual": 0,
                "executed": 0,
                "failed": 0,
                "inconclusive": 0,
                "rejected": 0,
                "cancelled": 0,
                "unresolved": 0,
            }
            if query_task_ids:
                action_rows = db.query(TopQueryActionPlan).filter(
                    TopQueryActionPlan.task_id.in_(query_task_ids)
                ).order_by(TopQueryActionPlan.created_at.asc(), TopQueryActionPlan.id.asc()).all()
                for action in action_rows:
                    task_id = int(action.task_id)
                    execution_mode = action.execution_mode
                    status = action.status
                    frozen_actions_by_task[task_id].append({
                        "id": int(action.id),
                        "taskId": task_id,
                        "brand": action.brand,
                        "queryText": action.query_text,
                        "dataDate": data_date.isoformat(),
                        "checkKey": action.check_key,
                        "actionType": action.action_type,
                        "executionMode": execution_mode,
                        "status": status,
                        "reason": action.reason,
                        "errorMessage": action.error_message,
                        "createdAt": action.created_at.isoformat() if action.created_at else None,
                    })
                    task_summary = action_summary_by_task.setdefault(int(task_id), {
                        "planned": 0,
                        "automatic": 0,
                        "manual": 0,
                        "executed": 0,
                        "failed": 0,
                        "inconclusive": 0,
                        "rejected": 0,
                        "cancelled": 0,
                        "unresolved": 0,
                    })
                    task_summary["planned"] += 1
                    action_summary["planned"] += 1
                    if execution_mode == "auto":
                        task_summary["automatic"] += 1
                        action_summary["automatic"] += 1
                    elif execution_mode == "human_approval":
                        task_summary["manual"] += 1
                        action_summary["manual"] += 1
                    if status in {"executed", "verified", "completed"}:
                        task_summary["executed"] += 1
                        action_summary["executed"] += 1
                    elif status == "dry_run_executed":
                        task_summary["executed"] += 1
                        action_summary["executed"] += 1
                    elif status in {"failed", "verification_failed"}:
                        task_summary["failed"] += 1
                        action_summary["failed"] += 1
                    elif status == "verification_inconclusive":
                        task_summary["inconclusive"] += 1
                        action_summary["inconclusive"] += 1
                    elif status == "rejected":
                        task_summary["rejected"] += 1
                        action_summary["rejected"] += 1
                    elif status == "cancelled":
                        task_summary["cancelled"] += 1
                        action_summary["cancelled"] += 1
                    else:
                        task_summary["unresolved"] += 1
                        action_summary["unresolved"] += 1

            treatment_keys = {
                normalize_governance_query_key(experiment.brand, sample.query_text)
                for sample in samples
                if sample.group_type == "treatment"
            }
            control_keys = {
                normalize_governance_query_key(experiment.brand, sample.query_text)
                for sample in samples
                if sample.group_type == "control"
            }
            treatment_tasks = {
                key: task for key, task in task_by_query.items() if key in treatment_keys
            }
            control_tasks = {
                key: task for key, task in task_by_query.items() if key in control_keys
            }
            control_action_count = sum(
                int(action_summary_by_task.get(int(task.id), {}).get("planned") or 0)
                for task in control_tasks.values()
                if task.id
            )
            treatment_task_ids = {
                int(task.id) for task in treatment_tasks.values() if task.id
            }
            unresolved_treatment_tasks = sum(
                1
                for key in treatment_keys
                if key not in treatment_tasks
                or str(treatment_tasks[key].status or "").lower() != "completed"
            )
            unresolved_actions = sum(
                int(summary.get("unresolved") or 0)
                for task_id, summary in action_summary_by_task.items()
                if task_id in treatment_task_ids
            )

            event_usage_by_task: Dict[int, Dict[str, float]] = defaultdict(
                lambda: {"tokenUsage": 0.0, "durationSeconds": 0.0, "retryCount": 0.0}
            )
            event_rows = db.query(
                TopQueryAgentEvent.task_id,
                TopQueryAgentEvent.token_usage,
                TopQueryAgentEvent.duration,
                TopQueryAgentEvent.event_type,
            ).filter(TopQueryAgentEvent.task_id.in_(query_task_ids)).all() if query_task_ids else []
            for task_id, token_usage, duration, event_type in event_rows:
                usage = event_usage_by_task[int(task_id)]
                usage["tokenUsage"] += float(token_usage or 0)
                usage["durationSeconds"] += float(duration or 0)
                if event_type == "replan_requested":
                    usage["retryCount"] += 1

            observed = 0
            contamination_keys: List[str] = []
            for sample in samples:
                metric = db.query(TopQueryExperimentDailyMetric).filter(
                    TopQueryExperimentDailyMetric.experiment_id == experiment_id,
                    TopQueryExperimentDailyMetric.query_text == sample.query_text,
                    TopQueryExperimentDailyMetric.data_date == data_date,
                ).first()
                if not metric:
                    metric = TopQueryExperimentDailyMetric(
                        experiment_id=experiment_id,
                        brand=experiment.brand,
                        query_text=sample.query_text,
                        group_type=sample.group_type,
                        data_date=data_date,
                    )
                    db.add(metric)
                sample_key = normalize_governance_query_key(experiment.brand, sample.query_text)
                row = row_map[sample_key]
                task = task_by_query.get(sample_key)
                observed += 1
                metric.observed_status = "observed" # type: ignore
                metric.query_rank = int(row.get("query_rank") or 0) # type: ignore
                metric.search_uv = int(float(row.get("search_uv") or 0)) # type: ignore
                metric.search_pv = int(float(row.get("search_pv") or 0)) # type: ignore
                metric.ctr = float(row.get("ctr") or 0) # type: ignore
                metric.cvr = float(row.get("cvr") or 0) # type: ignore
                try:
                    existing_payload = json.loads(metric.raw_payload or "{}")
                except json.JSONDecodeError:
                    existing_payload = {}
                existing_marker = existing_payload.get("experimentSnapshot") or {}
                payload = dict(row)
                payload["experimentSnapshot"] = {
                    "finalized": True,
                    "snapshotPhase": "finalized",
                    "experimentId": experiment_id,
                    "brand": str(experiment.brand),
                    "dataDate": data_date.isoformat(),
                    "prepareQualityHistory": existing_marker.get("prepareQualityHistory") or [],
                }
                task_action_summary = action_summary_by_task.get(int(task.id), {
                    "planned": 0,
                    "automatic": 0,
                    "manual": 0,
                    "executed": 0,
                    "failed": 0,
                    "inconclusive": 0,
                    "rejected": 0,
                    "cancelled": 0,
                    "unresolved": 0,
                }) if task and task.id else {
                    "planned": 0,
                    "automatic": 0,
                    "manual": 0,
                    "executed": 0,
                    "failed": 0,
                    "inconclusive": 0,
                    "rejected": 0,
                    "cancelled": 0,
                    "unresolved": 0,
                }
                payload["governanceActionSummary"] = task_action_summary
                payload["governanceActions"] = (
                    frozen_actions_by_task.get(int(task.id), []) if task and task.id else []
                )
                metric.raw_payload = json.dumps(payload, ensure_ascii=False) # type: ignore

                if sample.group_type == "treatment":
                    task_usage = event_usage_by_task.get(int(task.id), {}) if task and task.id else {}
                    metric.contamination_status = "clean" # type: ignore
                    metric.contamination_reason = None # type: ignore
                    metric.contaminated_from_date = None # type: ignore
                    metric.governance_task_id = int(task.id) if task and task.id else None # type: ignore
                    metric.governance_status = str(task.status) if task and task.status else None # type: ignore
                    metric.action_count = task_action_summary["planned"] # type: ignore
                    metric.auto_action_count = task_action_summary["automatic"] # type: ignore
                    metric.manual_action_count = task_action_summary["manual"] # type: ignore
                    metric.token_usage = int(task_usage.get("tokenUsage") or 0) # type: ignore
                    metric.duration_seconds = float(task_usage.get("durationSeconds") or 0) # type: ignore
                else:
                    contamination_status = "clean"
                    contamination_reason = None
                    contaminated_from_date = None
                    experiment_first_data_date = None
                    if experiment.planned_start_date:
                        experiment_first_data_date = experiment.planned_start_date - datetime.timedelta(days=1)
                    if task and experiment_first_data_date and task.data_date and task.data_date >= experiment_first_data_date:
                        contamination_status = "contaminated"
                        contamination_reason = f"Top query task created: {task.id}"
                        contaminated_from_date = data_date
                        contamination_keys.append(sample_key)
                    metric.contamination_status = contamination_status # type: ignore
                    metric.contamination_reason = contamination_reason # type: ignore
                    metric.contaminated_from_date = contaminated_from_date # type: ignore
                    metric.governance_task_id = int(task.id) if task and task.id else None # type: ignore
                    metric.governance_status = str(task.status) if task and task.status else None # type: ignore
                    metric.action_count = 0 # type: ignore
                    metric.auto_action_count = 0 # type: ignore
                    metric.manual_action_count = 0 # type: ignore
                    metric.token_usage = 0 # type: ignore
                    metric.duration_seconds = 0 # type: ignore

            data_quality = (snapshot or {}).get("dataQuality") or {
                "status": "complete",
                "expected": 40,
                "observed": observed,
                "missingKeys": [],
            }
            if contamination_keys:
                data_quality = {
                    "status": "failed",
                    "sourceStatus": "complete",
                    "expected": 40,
                    "observed": observed,
                    "missingKeys": [],
                    "error": "control_group_contamination",
                    "contaminationCount": len(set(contamination_keys)),
                    "contaminationKeys": sorted(set(contamination_keys)),
                }
                db.flush()
                for metric in db.query(TopQueryExperimentDailyMetric).filter(
                    TopQueryExperimentDailyMetric.experiment_id == experiment_id,
                    TopQueryExperimentDailyMetric.data_date == data_date,
                ).all():
                    payload = json.loads(metric.raw_payload or "{}")
                    payload["dataQuality"] = data_quality
                    metric.raw_payload = json.dumps(payload, ensure_ascii=False) # type: ignore
            db.commit()
            unresolved_count = unresolved_treatment_tasks + unresolved_actions
            retry_count = int(sum(
                event_usage_by_task[task_id]["retryCount"]
                for task_id in treatment_task_ids
            ))
            experiment_quality = build_experiment_quality(
                data_date=data_date,
                data_quality=data_quality,
                treatment_task_count=len(treatment_tasks),
                control_task_count=len(control_tasks),
                control_action_count=control_action_count,
                contamination_count=len(set(contamination_keys)),
                action_failed=int(action_summary.get("failed") or 0),
                action_inconclusive=int(action_summary.get("inconclusive") or 0),
                existing_unresolved=max(0, unresolved_count - max(0, 20 - len(treatment_tasks))),
                retry_count=retry_count,
                calculation_version=self._calculation_version(experiment),
            )

            return {
                "experimentId": experiment_id,
                "dataDate": data_date.isoformat(),
                "observedCount": observed,
                "sampleCount": len(samples),
                "dataQuality": data_quality,
                "actionSummary": action_summary,
                "experimentQuality": experiment_quality,
            }
        finally:
            db.close()

    @staticmethod
    def _build_early_experiment_quality(
        db,
        experiment: TopQueryExperiment,
        samples: List[TopQueryExperimentSample],
        data_date: datetime.date,
        data_quality: Dict[str, Any],
    ) -> Dict[str, Any]:
        sample_by_key = {
            normalize_governance_query_key(experiment.brand, sample.query_text): sample
            for sample in samples
        }
        tasks = [
            task for task in db.query(TopQueryTask).filter(
                TopQueryTask.brand == experiment.brand,
                TopQueryTask.data_date == data_date,
            ).all()
            if str(task.status or "").lower() != "superseded"
            and normalize_governance_query_key(experiment.brand, task.query_text) in sample_by_key
        ]
        grouped: Dict[str, List[TopQueryTask]] = defaultdict(list)
        for task in tasks:
            grouped[normalize_governance_query_key(experiment.brand, task.query_text)].append(task)
        task_ids = [int(task.id) for task in tasks if task.id]
        action_task_ids = {
            int(task_id) for (task_id,) in db.query(TopQueryActionPlan.task_id).filter(
                TopQueryActionPlan.task_id.in_(task_ids)
            ).distinct().all()
        } if task_ids else set()
        task_by_key = {key: select_task_winner(rows, action_task_ids) for key, rows in grouped.items()}
        treatment_keys = {
            key for key, sample in sample_by_key.items() if sample.group_type == "treatment"
        }
        control_keys = {
            key for key, sample in sample_by_key.items() if sample.group_type == "control"
        }
        treatment_tasks = {key: task for key, task in task_by_key.items() if key in treatment_keys}
        control_tasks = {key: task for key, task in task_by_key.items() if key in control_keys}
        winner_ids = [int(task.id) for task in task_by_key.values() if task.id]
        actions = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id.in_(winner_ids)
        ).all() if winner_ids else []
        control_task_ids = {int(task.id) for task in control_tasks.values() if task.id}
        treatment_task_ids = {int(task.id) for task in treatment_tasks.values() if task.id}
        success_action_statuses = {"executed", "verified", "completed", "dry_run_executed", "rejected", "cancelled"}
        failed_actions = sum(
            1 for action in actions
            if str(action.status or "").lower() in {"failed", "verification_failed"}
        )
        inconclusive_actions = sum(
            1 for action in actions
            if str(action.status or "").lower() == "verification_inconclusive"
        )
        unresolved_actions = sum(
            1 for action in actions
            if int(action.task_id or 0) in treatment_task_ids
            and str(action.status or "").lower() not in (
                success_action_statuses
                | {"failed", "verification_failed", "verification_inconclusive"}
            )
        )
        nonterminal_tasks = sum(
            1 for task in treatment_tasks.values()
            if str(task.status or "").lower() != "completed"
        )
        retry_count = db.query(TopQueryAgentEvent).filter(
            TopQueryAgentEvent.task_id.in_(list(treatment_task_ids)),
            TopQueryAgentEvent.event_type == "replan_requested",
        ).count() if treatment_task_ids else 0
        return build_experiment_quality(
            data_date=data_date,
            data_quality=data_quality,
            treatment_task_count=len(treatment_tasks),
            control_task_count=len(control_tasks),
            control_action_count=sum(1 for action in actions if int(action.task_id or 0) in control_task_ids),
            contamination_count=len(control_tasks),
            action_failed=failed_actions,
            action_inconclusive=inconclusive_actions,
            existing_unresolved=nonterminal_tasks + unresolved_actions,
            retry_count=retry_count,
            force_failed=True,
            calculation_version=TopQueryExperimentService._calculation_version(experiment),
        )

    def get_experiment_detail(
        self,
        experiment_id: int,
        as_of_date: Optional[datetime.date] = None,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            experiment = self._get_experiment(db, experiment_id)
            samples = self._get_samples(db, experiment_id)
            detail = self._serialize_experiment(experiment, samples)
            metrics = db.query(TopQueryExperimentDailyMetric).filter(
                TopQueryExperimentDailyMetric.experiment_id == experiment_id,
            )
            if as_of_date is not None:
                metrics = metrics.filter(TopQueryExperimentDailyMetric.data_date <= as_of_date)
            metric_rows = metrics.order_by(
                TopQueryExperimentDailyMetric.data_date.asc(),
                TopQueryExperimentDailyMetric.id.asc(),
            ).all()
            latest_metric_by_query: Dict[str, TopQueryExperimentDailyMetric] = {}
            metrics_by_group: Dict[str, List[TopQueryExperimentDailyMetric]] = defaultdict(list)
            for row in metric_rows:
                latest_metric_by_query[str(row.query_text)] = row
                if row.observed_status in {"observed", "not_observed"}:
                    metrics_by_group[str(row.group_type)].append(row)

            items = []
            for sample in samples:
                latest = latest_metric_by_query.get(str(sample.query_text))
                items.append({
                    "queryText": sample.query_text,
                    "groupType": sample.group_type,
                    "sampleRank": sample.sample_rank,
                    "sampleSearchPv": sample.sample_search_pv,
                    "sampleSearchUv": sample.sample_search_uv,
                    "sampleCtr": sample.sample_ctr,
                    "sampleCvr": sample.sample_cvr,
                    "observedStatus": latest.observed_status if latest else "not_synced",
                    "contaminationStatus": latest.contamination_status if latest else "clean",
                    "contaminationReason": latest.contamination_reason if latest else None,
                    "latestSearchPv": latest.search_pv if latest else None,
                    "latestSearchUv": latest.search_uv if latest else None,
                    "latestCtr": latest.ctr if latest else None,
                    "latestCvr": latest.cvr if latest else None,
                    "governanceTaskId": latest.governance_task_id if latest else None,
                    "governanceStatus": latest.governance_status if latest else None,
                })
            detail["items"] = items
            latest_quality = None
            for row in reversed(metric_rows):
                try:
                    payload = json.loads(row.raw_payload or "{}")
                except json.JSONDecodeError:
                    continue
                if isinstance(payload.get("dataQuality"), dict):
                    latest_quality = payload["dataQuality"]
                    break
            detail["dataQuality"] = latest_quality
            detail["groupSummary"] = {} if latest_quality and latest_quality.get("status") != "complete" else {
                group_type: self._build_group_summary(rows)
                for group_type, rows in metrics_by_group.items()
            }
            return detail
        finally:
            db.close()

    @staticmethod
    def _normalize_query_list(queries: List[str]) -> List[str]:
        normalized = []
        seen = set()
        for query in queries:
            value = normalize_query_text(query)
            query_key = normalize_query_key(value)
            if not query_key or query_key in seen:
                continue
            seen.add(query_key)
            normalized.append(value)
        return normalized

    @staticmethod
    def _validate_sample_membership(
        brand: str,
        query_map: Dict[str, Dict[str, Any]],
        treatment_queries: List[str],
        control_queries: List[str],
    ) -> None:
        if not treatment_queries or not control_queries:
            raise ExperimentValidationError("实验组和对照组都不能为空")
        treatment_keys = {normalize_governance_query_key(brand, query) for query in treatment_queries}
        control_keys = {normalize_governance_query_key(brand, query) for query in control_queries}
        if len(treatment_keys) != len(treatment_queries) or len(control_keys) != len(control_queries):
            raise ExperimentValidationError("同一治理身份的 Query 不能在同一组重复选择")
        overlap = treatment_keys & control_keys
        if overlap:
            raise ExperimentValidationError("同一个 Query 不能同时进入实验组和对照组")
        missing = [
            query for query in treatment_queries + control_queries
            if normalize_query_key(query) not in query_map
        ]
        if missing:
            raise ExperimentValidationError(f"所选 Query 不在上一日 Top Query 快照池中: {missing[0]}")

    def _validate_balancing(self, samples: List[TopQueryExperimentSample]) -> None:
        treatment = [item for item in samples if item.group_type == "treatment"]
        control = [item for item in samples if item.group_type == "control"]
        if len(treatment) != len(control):
            raise ExperimentValidationError("实验组与对照组数量必须相同")
        if len(treatment) < 5 or len(treatment) > 50:
            raise ExperimentValidationError("单组样本量必须在 5 到 50 之间")
        rank_diff = abs(self._avg([item.sample_rank for item in treatment]) - self._avg([item.sample_rank for item in control]))
        if rank_diff > 5:
            raise ExperimentValidationError("排名不均衡")
        pv_diff_ratio = self._diff_ratio(
            sum(int(item.sample_search_pv or 0) for item in treatment),
            sum(int(item.sample_search_pv or 0) for item in control),
        )
        if pv_diff_ratio > 0.2:
            raise ExperimentValidationError("PV 不均衡")
        uv_diff_ratio = self._diff_ratio(
            sum(int(item.sample_search_uv or 0) for item in treatment),
            sum(int(item.sample_search_uv or 0) for item in control),
        )
        if uv_diff_ratio > 0.2:
            raise ExperimentValidationError("UV 不均衡")

    @staticmethod
    def _validate_lifecycle_ready(
        experiment: TopQueryExperiment,
        samples: List[TopQueryExperimentSample],
        draft_date: Optional[datetime.date] = None,
    ) -> None:
        treatment = [item for item in samples if item.group_type == "treatment"]
        control = [item for item in samples if item.group_type == "control"]
        canonical_keys = {
            normalize_governance_query_key(experiment.brand, item.query_text)
            for item in samples
            if normalize_governance_query_key(experiment.brand, item.query_text)
        }
        if len(treatment) != 20 or len(control) != 20 or len(samples) != 40 or len(canonical_keys) != 40:
            raise ExperimentValidationError("实验必须固定为 20 个实验组 Query 和 20 个对照组 Query")
        if not experiment.planned_start_date or not experiment.planned_end_date:
            raise ExperimentValidationError("计划开始日期和计划结束日期不能为空")
        if experiment.planned_end_date < experiment.planned_start_date:
            raise ExperimentValidationError("计划结束日期不能早于计划开始日期")
        expected_sample_date = (
            draft_date - datetime.timedelta(days=1)
            if draft_date is not None
            else experiment.planned_start_date - datetime.timedelta(days=2)
        )
        expected_start_date = (
            draft_date + datetime.timedelta(days=1)
            if draft_date is not None
            else experiment.planned_start_date
        )
        if (
            experiment.sample_data_date != expected_sample_date
            or experiment.planned_start_date != expected_start_date
        ):
            raise ExperimentValidationError("实验样本池日期已失效，请基于当前 T-1 样本池重新建立草稿")

    @staticmethod
    def _avg(values: List[Optional[int]]) -> float:
        items = [float(value or 0) for value in values]
        return sum(items) / len(items) if items else 0.0

    @staticmethod
    def _diff_ratio(left: int, right: int) -> float:
        total = left + right
        if total <= 0:
            return 0.0
        return abs(left - right) / total

    @staticmethod
    def _get_experiment(db, experiment_id: int) -> TopQueryExperiment:
        experiment = db.get(TopQueryExperiment, experiment_id)
        if not experiment:
            raise ExperimentValidationError(f"实验不存在: {experiment_id}")
        return experiment

    @staticmethod
    def _calculation_version(experiment: TopQueryExperiment) -> str:
        return str(experiment.calculation_version or "")

    @staticmethod
    def _get_samples(db, experiment_id: int) -> List[TopQueryExperimentSample]:
        return db.query(TopQueryExperimentSample).filter(
            TopQueryExperimentSample.experiment_id == experiment_id
        ).order_by(
            TopQueryExperimentSample.group_type.asc(),
            TopQueryExperimentSample.sample_rank.asc(),
            TopQueryExperimentSample.id.asc(),
        ).all()

    def _serialize_experiment(
        self,
        experiment: TopQueryExperiment,
        samples: List[TopQueryExperimentSample],
    ) -> Dict[str, Any]:
        return {
            "id": int(experiment.id),
            "brand": experiment.brand,
            "experimentName": experiment.experiment_name,
            "status": experiment.status,
            "sampleDataDate": experiment.sample_data_date.isoformat() if experiment.sample_data_date else None,
            "plannedStartDate": experiment.planned_start_date.isoformat() if experiment.planned_start_date else None,
            "plannedEndDate": experiment.planned_end_date.isoformat() if experiment.planned_end_date else None,
            "baselineStartDate": experiment.baseline_start_date.isoformat() if experiment.baseline_start_date else None,
            "baselineEndDate": experiment.baseline_end_date.isoformat() if experiment.baseline_end_date else None,
            "baselineLockedAt": experiment.baseline_locked_at.isoformat() if experiment.baseline_locked_at else None,
            "calculationVersion": self._calculation_version(experiment),
            "startedAt": experiment.started_at.isoformat() if experiment.started_at else None,
            "endedAt": experiment.ended_at.isoformat() if experiment.ended_at else None,
            "createdBy": experiment.created_by,
            "samples": [
                {
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
                    "baselineObservedDays": sample.baseline_observed_days,
                    "baselineSearchUv": sample.baseline_search_uv,
                    "baselineSearchPv": sample.baseline_search_pv,
                    "baselineClickUv": sample.baseline_click_uv,
                    "baselinePurchaseUv": sample.baseline_purchase_uv,
                    "baselineCtr": sample.baseline_ctr,
                    "baselineCvr": sample.baseline_cvr,
                }
                for sample in samples
            ],
        }

    @staticmethod
    def _build_group_summary(rows: List[TopQueryExperimentDailyMetric]) -> Dict[str, Any]:
        valid_rows = [row for row in rows if row.observed_status == "observed" and row.contamination_status != "contaminated"]
        return {
            "observedCount": sum(1 for row in rows if row.observed_status == "observed"),
            "cleanCount": len(valid_rows),
            "contaminatedCount": sum(1 for row in rows if row.contamination_status == "contaminated"),
            "avgCtr": round(sum(float(row.ctr or 0) for row in valid_rows) / len(valid_rows), 6) if valid_rows else 0,
            "avgCvr": round(sum(float(row.cvr or 0) for row in valid_rows) / len(valid_rows), 6) if valid_rows else 0,
            "totalSearchPv": sum(int(row.search_pv or 0) for row in valid_rows),
            "totalSearchUv": sum(int(row.search_uv or 0) for row in valid_rows),
        }
