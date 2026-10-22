import datetime
import hashlib
import json
from contextlib import ExitStack
from typing import Any, Callable, Dict, Iterable, List, Optional

from src.persistence import (
    OperationLog,
    SessionLocal,
    TopQueryActionPlan,
    TopQueryContextSnapshot,
    TopQueryExperimentDailyMetric,
    TopQueryTask,
    TopQueryWorkflowRun,
)

from .experiment_service import (
    ExperimentValidationError,
    assert_queries_governance_allowed_in_session,
)
from src.governance.query_identity import normalize_governance_query_key
from .task_lock import (
    TopQueryTaskLockBusy,
    top_query_brand_governance_lock,
    top_query_task_lock,
)


MANIFEST_SCHEMA_VERSION = "zha41.false-zero.v2"
MANIFEST_MAX_AGE = datetime.timedelta(hours=24)
MANIFEST_FUTURE_SKEW = datetime.timedelta(minutes=5)
SAFE_UNEXECUTED_ACTION_STATUSES = {
    "pending",
    "proposed",
    "waiting_human_approval",
    "approved",
}


class RemediationValidationError(ValueError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_manifest_digest(manifest: Dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "digest"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_json_object(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_datetime(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _parse_datetime(value: Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return _normalize_datetime(value)
    if not isinstance(value, str) or not value.strip():
        raise RemediationValidationError("manifest fixedAt is required")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RemediationValidationError("manifest fixedAt is invalid") from exc
    return _normalize_datetime(parsed)


def _iso_datetime(value: Optional[datetime.datetime]) -> Optional[str]:
    return _normalize_datetime(value).isoformat() if value else None


def _is_search_operation_log(log: OperationLog) -> bool:
    if str(log.module or "").strip() != "OperationalService":
        return False
    action = str(log.action or "").strip().lower()
    return action == "post mixed" or action.endswith("/api/search/mixed")


class FalseZeroRemediator:
    def __init__(
        self,
        *,
        session_factory=SessionLocal,
        now_provider: Optional[Callable[[], datetime.datetime]] = None,
    ):
        self.session_factory = session_factory
        self.now_provider = now_provider or datetime.datetime.now

    def build_manifest(
        self,
        *,
        fixed_at: datetime.datetime,
        task_ids: Optional[Iterable[int]] = None,
    ) -> Dict[str, Any]:
        fixed_at = _normalize_datetime(fixed_at)
        selected_ids = sorted({int(task_id) for task_id in task_ids}) if task_ids is not None else None
        db = self.session_factory()
        try:
            query = db.query(TopQueryTask).filter(
                TopQueryTask.result_count == 0,
                TopQueryTask.status != "superseded",
            )
            if selected_ids is not None:
                query = query.filter(TopQueryTask.id.in_(selected_ids))
            tasks = query.order_by(TopQueryTask.id.asc()).all()
            items = [self._classify_task(db, task, fixed_at) for task in tasks]
            manifest = {
                "schemaVersion": MANIFEST_SCHEMA_VERSION,
                "changeTicket": "ZHA-41",
                "generatedAt": _iso_datetime(self.now_provider()),
                "fixedAt": _iso_datetime(fixed_at),
                "databaseIdentity": self._database_identity(db),
                "items": items,
                "summary": {
                    "total": len(items),
                    "confirmed": sum(item["evidenceClass"] == "confirmed" for item in items),
                    "ambiguous": sum(item["evidenceClass"] == "ambiguous" for item in items),
                    "eligible": sum(item["eligibility"] == "eligible" for item in items),
                    "blocked": sum(item["eligibility"] == "blocked" for item in items),
                },
            }
            manifest["digest"] = compute_manifest_digest(manifest)
            return manifest
        finally:
            db.close()

    def apply_manifest(
        self,
        manifest: Dict[str, Any],
        *,
        mode: str,
        operator: str,
        max_tasks: int,
        confirm_digest: str,
    ) -> Dict[str, Any]:
        if mode not in {"rerun", "invalidate"}:
            raise RemediationValidationError("mode must be rerun or invalidate")
        if not str(operator or "").strip():
            raise RemediationValidationError("operator is required")
        if not isinstance(max_tasks, int) or isinstance(max_tasks, bool) or max_tasks <= 0:
            raise RemediationValidationError("max_tasks must be a positive integer")
        if not isinstance(manifest, dict):
            raise RemediationValidationError("manifest must be an object")
        if manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
            raise RemediationValidationError("unsupported manifest schema")
        if manifest.get("changeTicket") != "ZHA-41":
            raise RemediationValidationError("change ticket mismatch")
        expected_digest = compute_manifest_digest(manifest)
        if manifest.get("digest") != expected_digest or confirm_digest != expected_digest:
            raise RemediationValidationError("manifest digest mismatch")
        generated_at = _parse_datetime(manifest.get("generatedAt"))
        now = _normalize_datetime(self.now_provider())
        if generated_at > now + MANIFEST_FUTURE_SKEW:
            raise RemediationValidationError("manifest generatedAt is in the future")
        if now - generated_at > MANIFEST_MAX_AGE:
            raise RemediationValidationError("manifest expired")
        fixed_at = _parse_datetime(manifest.get("fixedAt"))
        items = manifest.get("items")
        if not isinstance(items, list):
            raise RemediationValidationError("manifest items must be a list")
        if len(items) > max_tasks:
            raise RemediationValidationError("manifest item count exceeds max_tasks")
        if any(item.get("eligibility") != "eligible" for item in items if isinstance(item, dict)):
            raise RemediationValidationError("manifest contains blocked items")
        if any(
            mode not in item.get("allowedModes", [])
            for item in items
            if isinstance(item, dict)
        ):
            raise RemediationValidationError(f"manifest mode not allowed: {mode}")
        normalized_items: List[Dict[str, Any]] = []
        task_ids: List[int] = []
        for item in items:
            if not isinstance(item, dict):
                raise RemediationValidationError("manifest item must be an object")
            task_id = int(item.get("taskId"))
            if task_id in task_ids:
                raise RemediationValidationError(f"duplicate source task: {task_id}")
            normalized_items.append(item)
            task_ids.append(task_id)

        applied_task_ids: List[int] = []
        replacement_task_ids: List[int] = []
        try:
            brand_db = self.session_factory()
            try:
                brands = sorted({
                    str(brand or "").strip().lower()
                    for (brand,) in brand_db.query(TopQueryTask.brand).filter(
                        TopQueryTask.id.in_(task_ids),
                    ).all()
                })
            finally:
                brand_db.close()

            with ExitStack() as locks:
                for brand in brands:
                    locks.enter_context(top_query_brand_governance_lock(brand))
                for task_id in sorted(task_ids):
                    locks.enter_context(top_query_task_lock(task_id))

                db = self.session_factory()
                try:
                    if manifest.get("databaseIdentity") != self._database_identity(db):
                        raise RemediationValidationError("database identity mismatch")
                    locked_tasks = db.query(TopQueryTask).filter(
                        TopQueryTask.id.in_(task_ids),
                    ).with_for_update().all()
                    tasks_by_id = {int(task.id): task for task in locked_tasks}
                    for item, task_id in zip(normalized_items, task_ids):
                        task = tasks_by_id.get(task_id)
                        if task is None:
                            raise RemediationValidationError(f"source task missing: {task_id}")
                        current_digest = self._source_state_digest(
                            db,
                            task,
                            lock_rows=True,
                        )
                        if current_digest != item.get("sourceStateDigest"):
                            raise RemediationValidationError(f"source state changed: {task_id}")

                    for task_id in task_ids:
                        task = tasks_by_id[task_id]
                        try:
                            assert_queries_governance_allowed_in_session(
                                db,
                                str(task.brand or ""),
                                [str(task.query_text or "")],
                            )
                        except ExperimentValidationError as exc:
                            raise RemediationValidationError(
                                f"running_experiment_control_sample:{task_id}"
                            ) from exc

                    for task_id in task_ids:
                        replacement = self._apply_task(
                            db,
                            tasks_by_id[task_id],
                            manifest_digest=expected_digest,
                            fixed_at=fixed_at,
                            mode=mode,
                            operator=str(operator).strip(),
                        )
                        applied_task_ids.append(task_id)
                        replacement_task_ids.append(int(replacement.id))
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()
        except TopQueryTaskLockBusy as exc:
            raise RemediationValidationError(
                f"source task busy: {exc.task_id}"
            ) from exc

        return {
            "mode": mode,
            "manifestDigest": expected_digest,
            "appliedTaskIds": applied_task_ids,
            "replacementTaskIds": replacement_task_ids,
        }

    def _classify_task(
        self,
        db,
        task: TopQueryTask,
        fixed_at: datetime.datetime,
    ) -> Dict[str, Any]:
        blockers: List[str] = []
        missing_fields = [
            field
            for field, value in (
                ("brand", task.brand),
                ("query_text", task.query_text),
                ("data_date", task.data_date),
            )
            if value in (None, "")
        ]
        if missing_fields:
            blockers.append(f"missing_required_fields:{','.join(missing_fields)}")

        raw_payload = _parse_json_object(task.raw_payload)
        if raw_payload is None:
            blockers.append("invalid_raw_payload")

        latest_snapshot = db.query(TopQueryContextSnapshot).filter(
            TopQueryContextSnapshot.task_id == task.id,
            TopQueryContextSnapshot.context_type == "before_search",
            TopQueryContextSnapshot.created_at <= fixed_at,
        ).order_by(TopQueryContextSnapshot.id.desc()).first()
        snapshot_payload = _parse_json_object(latest_snapshot.payload) if latest_snapshot else None
        empty_before_snapshot = bool(
            isinstance(snapshot_payload, dict)
            and snapshot_payload.get("result_count") == 0
            and snapshot_payload.get("products") == []
        )
        previous_result_count = (
            snapshot_payload.get("previous_result_count")
            if isinstance(snapshot_payload, dict)
            else None
        )
        legacy_positive_previous_result_count = bool(
            isinstance(snapshot_payload, dict)
            and "success" not in snapshot_payload
            and "result_count_known" not in snapshot_payload
            and "result_complete" not in snapshot_payload
            and isinstance(previous_result_count, int)
            and not isinstance(previous_result_count, bool)
            and previous_result_count > 0
        )

        created_at = _normalize_datetime(task.created_at) if task.created_at else datetime.datetime.combine(
            task.data_date or fixed_at.date(),
            datetime.time.min,
        )
        operation_window_start = created_at - datetime.timedelta(minutes=15)
        operation_logs = db.query(
            OperationLog.id,
            OperationLog.module,
            OperationLog.action,
            OperationLog.status,
            OperationLog.error_message,
            OperationLog.created_at,
        ).filter(
            OperationLog.brand == task.brand,
            OperationLog.query_text == task.query_text,
            OperationLog.created_at >= operation_window_start,
            OperationLog.created_at <= fixed_at,
        ).order_by(OperationLog.id.asc()).all()
        search_operation_logs = [
            log for log in operation_logs if _is_search_operation_log(log)
        ]
        successful_logs = [
            log for log in search_operation_logs
            if str(log.status or "").lower() in {"success", "ok", "completed"}
        ]
        failed_logs = [
            log for log in search_operation_logs
            if log not in successful_logs
            and (
                str(log.status or "").lower() in {
                    "failed", "failure", "error", "rate_limited", "timeout"
                }
                or bool(log.error_message)
            )
        ]
        created_before_fix = created_at < fixed_at
        evidence_class = (
            "confirmed"
            if empty_before_snapshot
            and created_before_fix
            and (
                legacy_positive_previous_result_count
                or (bool(failed_logs) and not successful_logs)
            )
            else "ambiguous"
        )
        if evidence_class != "confirmed":
            blockers.append("evidence_not_confirmed")

        actions = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task.id,
        ).order_by(TopQueryActionPlan.id.asc()).all()
        for action in actions:
            status = str(action.status or "").lower()
            if status not in SAFE_UNEXECUTED_ACTION_STATUSES:
                blockers.append(f"action_already_executed:{action.id}:{status or 'unknown'}")

        active_workflow = db.query(TopQueryWorkflowRun).filter(
            TopQueryWorkflowRun.task_id == task.id,
            TopQueryWorkflowRun.active_slot == "active",
        ).first()
        if active_workflow is not None:
            blockers.append(f"active_workflow:{active_workflow.id}")

        if task.brand and task.query_text:
            try:
                assert_queries_governance_allowed_in_session(
                    db,
                    str(task.brand),
                    [str(task.query_text)],
                )
            except ExperimentValidationError:
                blockers.append("running_experiment_control_sample")

        experiment_metric_ids = [
            int(row.id)
            for row in db.query(TopQueryExperimentDailyMetric.id).filter(
                TopQueryExperimentDailyMetric.governance_task_id == task.id,
            ).order_by(TopQueryExperimentDailyMetric.id.asc()).all()
        ]
        experiment_reference = bool(experiment_metric_ids) or (
            isinstance(raw_payload, dict) and raw_payload.get("experiment_id") is not None
        )

        if task.brand and task.query_text and task.data_date:
            query_key = normalize_governance_query_key(task.brand, task.query_text)
            variants = db.query(TopQueryTask).filter(
                TopQueryTask.brand == task.brand,
                TopQueryTask.data_date == task.data_date,
                TopQueryTask.id != task.id,
                TopQueryTask.status != "superseded",
            ).all()
            duplicate_ids = [
                int(variant.id)
                for variant in variants
                if normalize_governance_query_key(task.brand, variant.query_text) == query_key
            ]
            if duplicate_ids:
                blockers.append(
                    "duplicate_active_task:" + ",".join(str(task_id) for task_id in duplicate_ids)
                )

        already_remediated = db.query(TopQueryContextSnapshot.id).filter(
            TopQueryContextSnapshot.task_id == task.id,
            TopQueryContextSnapshot.context_type == "remediation_zha41",
        ).first()
        if already_remediated is not None:
            blockers.append("already_remediated")

        blockers = sorted(set(blockers))
        allowed_modes: List[str] = []
        mode_restrictions: List[str] = []
        if not blockers:
            allowed_modes.append("invalidate")
            if experiment_reference:
                mode_restrictions.append("rerun_blocked_by_experiment_reference")
            else:
                allowed_modes.append("rerun")
        return {
            "taskId": int(task.id),
            "brand": task.brand,
            "query": task.query_text,
            "dataDate": task.data_date.isoformat() if task.data_date else None,
            "evidenceClass": evidence_class,
            "eligibility": "eligible" if allowed_modes else "blocked",
            "blockers": blockers,
            "allowedModes": allowed_modes,
            "modeRestrictions": mode_restrictions,
            "evidence": {
                "emptyBeforeSnapshot": empty_before_snapshot,
                "beforeSnapshotId": int(latest_snapshot.id) if latest_snapshot else None,
                "createdBeforeFix": created_before_fix,
                "previousResultCount": previous_result_count,
                "legacyPositivePreviousResultCount": legacy_positive_previous_result_count,
                "operationWindowStart": _iso_datetime(operation_window_start),
                "failedOperationLogIds": [int(log.id) for log in failed_logs],
                "successfulOperationLogIds": [int(log.id) for log in successful_logs],
                "experimentMetricIds": experiment_metric_ids,
            },
            "sourceStateDigest": self._source_state_digest(db, task),
        }

    def _source_state_digest(
        self,
        db,
        task: TopQueryTask,
        *,
        lock_rows: bool = False,
    ) -> str:
        action_query = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task.id,
        ).order_by(TopQueryActionPlan.id.asc())
        workflow_query = db.query(TopQueryWorkflowRun).filter(
            TopQueryWorkflowRun.task_id == task.id,
        ).order_by(TopQueryWorkflowRun.id.asc())
        snapshot_query = db.query(TopQueryContextSnapshot).filter(
            TopQueryContextSnapshot.task_id == task.id,
        ).order_by(TopQueryContextSnapshot.id.asc())
        operation_log_query = db.query(
            OperationLog.id,
            OperationLog.action,
            OperationLog.status,
            OperationLog.error_message,
            OperationLog.created_at,
        ).filter(
            OperationLog.brand == task.brand,
            OperationLog.query_text == task.query_text,
        ).order_by(OperationLog.id.asc())
        experiment_metric_query = db.query(TopQueryExperimentDailyMetric).filter(
            TopQueryExperimentDailyMetric.governance_task_id == task.id,
        ).order_by(TopQueryExperimentDailyMetric.id.asc())
        if lock_rows:
            action_query = action_query.with_for_update()
            workflow_query = workflow_query.with_for_update()
            snapshot_query = snapshot_query.with_for_update()
            operation_log_query = operation_log_query.with_for_update()
            experiment_metric_query = experiment_metric_query.with_for_update()
        actions = action_query.all()
        workflows = workflow_query.all()
        snapshots = snapshot_query.all()
        operation_logs = operation_log_query.all()
        experiment_metrics = experiment_metric_query.all()
        return _digest({
            "task": {
                "id": int(task.id),
                "brand": task.brand,
                "dataDate": task.data_date.isoformat() if task.data_date else None,
                "query": task.query_text,
                "status": task.status,
                "displayStatus": task.display_status,
                "resultCount": task.result_count,
                "rawPayload": task.raw_payload,
                "updatedAt": _iso_datetime(task.updated_at),
            },
            "actions": [
                {
                    "id": int(action.id),
                    "status": action.status,
                    "executionMode": action.execution_mode,
                    "externalRequestId": action.external_request_id,
                    "updatedAt": _iso_datetime(action.updated_at),
                }
                for action in actions
            ],
            "workflows": [
                {
                    "id": int(workflow.id),
                    "status": workflow.status,
                    "activeSlot": workflow.active_slot,
                    "checkpointVersion": workflow.checkpoint_version,
                    "updatedAt": _iso_datetime(workflow.updated_at),
                }
                for workflow in workflows
            ],
            "snapshots": [
                {
                    "id": int(snapshot.id),
                    "contextType": snapshot.context_type,
                    "payload": snapshot.payload,
                }
                for snapshot in snapshots
            ],
            "operationLogs": [
                {
                    "id": int(log.id),
                    "status": log.status,
                    "action": log.action,
                    "errorMessage": log.error_message,
                    "createdAt": _iso_datetime(log.created_at),
                }
                for log in operation_logs
            ],
            "experimentMetricIds": [int(metric.id) for metric in experiment_metrics],
        })

    def _apply_task(
        self,
        db,
        task: TopQueryTask,
        *,
        manifest_digest: str,
        fixed_at: datetime.datetime,
        mode: str,
        operator: str,
    ) -> TopQueryTask:
        active_workflow = db.query(TopQueryWorkflowRun).filter(
            TopQueryWorkflowRun.task_id == task.id,
            TopQueryWorkflowRun.active_slot == "active",
        ).with_for_update().first()
        if active_workflow is not None:
            raise RemediationValidationError(
                f"active workflow state changed: {task.id}:{active_workflow.id}"
            )

        actions = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.task_id == task.id,
        ).order_by(TopQueryActionPlan.id.asc()).with_for_update().all()
        for action in actions:
            status = str(action.status or "").lower()
            if status not in SAFE_UNEXECUTED_ACTION_STATUSES:
                raise RemediationValidationError(
                    f"unsafe action state changed: {action.id}:{status or 'unknown'}"
                )
            response_payload = _parse_json_object(action.response_payload) or {}
            response_payload["remediation_zha41"] = {
                "status": "cancelled",
                "manifestDigest": manifest_digest,
                "operator": operator,
                "mode": mode,
            }
            updated = db.query(TopQueryActionPlan).filter(
                TopQueryActionPlan.id == action.id,
                TopQueryActionPlan.status == action.status,
                TopQueryActionPlan.status.in_(SAFE_UNEXECUTED_ACTION_STATUSES),
            ).update({
                TopQueryActionPlan.status: "cancelled",
                TopQueryActionPlan.response_payload: _canonical_json(response_payload),
                TopQueryActionPlan.error_message: "ZHA-41 历史假 0 任务处置：取消未执行治理动作",
            }, synchronize_session=False)
            if updated != 1:
                raise RemediationValidationError(
                    f"unsafe action state changed: {action.id}:{status or 'unknown'}"
                )

        source_status = task.status
        source_updated = db.query(TopQueryTask).filter(
            TopQueryTask.id == task.id,
            TopQueryTask.status == source_status,
        ).update({
            TopQueryTask.status: "superseded",
            TopQueryTask.display_status: "superseded",
        }, synchronize_session=False)
        if source_updated != 1:
            raise RemediationValidationError(f"source state changed: {task.id}")

        source_raw_payload = _parse_json_object(task.raw_payload) or {}
        replacement_raw_payload = dict(source_raw_payload)
        replacement_raw_payload["remediation_zha41"] = {
            "sourceTaskId": int(task.id),
            "manifestDigest": manifest_digest,
            "operator": operator,
            "fixedAt": _iso_datetime(fixed_at),
            "mode": mode,
        }
        replacement = TopQueryTask(
            brand=task.brand,
            data_date=task.data_date,
            query_text=task.query_text,
            query_rank=task.query_rank,
            search_uv=task.search_uv,
            search_pv=task.search_pv,
            ctr=task.ctr,
            cvr=task.cvr,
            avg_ctr=task.avg_ctr,
            avg_cvr=task.avg_cvr,
            query_pv_share=task.query_pv_share,
            status="pending" if mode == "rerun" else "cancelled",
            display_status="normal" if mode == "rerun" else "superseded",
            result_count=None,
            started_at=None,
            completed_at=None,
            error_message=None,
            raw_payload=_canonical_json(replacement_raw_payload),
            query_source=task.query_source,
            hot_word_type=task.hot_word_type,
            hot_status=task.hot_status,
        )
        db.add(replacement)
        db.flush()

        remediation_payload = {
            "schemaVersion": MANIFEST_SCHEMA_VERSION,
            "changeTicket": "ZHA-41",
            "manifestDigest": manifest_digest,
            "sourceTaskId": int(task.id),
            "replacementTaskId": int(replacement.id),
            "operator": operator,
            "fixedAt": _iso_datetime(fixed_at),
            "mode": mode,
        }
        db.add_all([
            TopQueryContextSnapshot(
                task_id=task.id,
                brand=task.brand,
                query_text=task.query_text,
                context_type="remediation_zha41",
                payload=_canonical_json({**remediation_payload, "role": "source"}),
            ),
            TopQueryContextSnapshot(
                task_id=replacement.id,
                brand=replacement.brand,
                query_text=replacement.query_text,
                context_type="remediation_zha41",
                payload=_canonical_json({**remediation_payload, "role": "replacement"}),
            ),
        ])
        return replacement

    @staticmethod
    def _database_identity(db) -> str:
        bind = db.get_bind()
        url = bind.url.render_as_string(hide_password=True)
        return "sha256:" + hashlib.sha256(url.encode("utf-8")).hexdigest()
