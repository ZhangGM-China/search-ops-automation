import datetime
import concurrent.futures
from collections import defaultdict, deque
import hashlib
import logging
import socket
import signal
import threading
import time
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Optional, Any, Callable
import requests
from apscheduler.events import EVENT_JOB_SUBMITTED, EVENT_SCHEDULER_STARTED
from apscheduler.schedulers.base import SchedulerNotRunningError
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.exc import OperationalError
from src.persistence import (
    SessionLocal, TopQueryActionPlan,
    create_task_session, update_task_session, check_database_connection,
    get_latest_task_session, get_latest_abnormal_query_for_brand,
    get_recent_operation_metrics, get_recent_table_activity,
    mark_stale_running_sessions_failed,
    OperationLogRetentionIndexMissing, purge_expired_operation_logs,
    SCHEDULER_GLOBAL_LEADER_NAME,
    SchedulerJobRun,
    SchedulerJobRunClaimError,
    SchedulerJobRunStateError,
    SchedulerLeaderFenceError,
    SchedulerLeaderLeaseUnavailable,
    acquire_scheduler_leader_lease,
    claim_scheduler_job_run,
    complete_scheduler_job_run,
    create_scheduler_job_run,
    recover_stale_scheduler_job_runs,
    release_scheduler_leader_lease,
    renew_scheduler_leader_lease,
    require_scheduler_leader_fence,
    start_scheduler_job_run,
)
from src.integrations.query_extractor import QueryExtractor
from src.integrations.operational_service import OperationalService
from src.config import reload_config, get_brand_business_config, GLOBAL_BUSINESS_CONFIG, GLOBAL_SAFETY_CONFIG
from src.no_result.governance import NoResultGovernanceService
from src.top_query.service import TopQueryGovernanceService
_TopQueryGovernanceServiceContract = TopQueryGovernanceService
from src.top_query.experiment_service import (
    TopQueryExperimentService,
    build_experiment_quality,
    experiment_quality_gate_passed,
    validate_running_experiment_state,
)
from src.governance.query_policy import ExperimentValidationError, assert_query_governance_allowed
from src.governance.query_identity import normalize_governance_query_key, normalize_query_key
from src.top_query.sanitization import sanitize_action_error
from src.query_governance.retirement import LegacyAgentLoopRetiredError
from src.query_governance.experiments import QueryGovernanceExperimentScheduler
from src.query_governance.scheduler import (
    query_governance_mainline_job,
    query_governance_recovery_job,
    query_governance_remediation_job,
    query_governance_summary_reconciliation_job,
    query_governance_image_integrity_job,
)
import json
from src.top_query.query_demotion_contract import uses_authoritative_query_demotion
from src.runtime.process_guard import ProcessLockError, acquire_process_lock
from .reliability import (
    plan_and_persist_missed_scheduler_runs,
    scheduler_compensation_idempotency_key,
)
from .recovery import SchedulerLeaseHeartbeat
from .termination import (
    ActiveRunTracker,
    SchedulerTerminationController,
    install_termination_signal_handlers,
)

# 全局变量，用于并发任务间的上限阻断
GLOBAL_LIMIT_REACHED = False
INVENTORY_MONITOR_JOB_PREFIX = "inventory_monitor_job:"
QUERY_GOVERNANCE_EXPERIMENT_JOB_ID = "query_governance_experiment_job"
QUERY_GOVERNANCE_REMEDIATION_JOB_ID = "query_governance_remediation_job"
QUERY_GOVERNANCE_RECOVERY_JOB_ID = "query_governance_recovery_job"
QUERY_GOVERNANCE_SUMMARY_RECONCILIATION_JOB_ID = "query_governance_summary_reconciliation_job"
QUERY_GOVERNANCE_IMAGE_INTEGRITY_JOB_ID = "query_governance_image_integrity_job"
LEGACY_INVENTORY_MONITOR_JOB_PREFIXES = (
    "agent_loop_inventory_monitor:",
    "broken_rate_monitor_job:",
)
SCHEDULER_LEASE_SECONDS = 60
SCHEDULER_TERMINATION_DRAIN_SECONDS = 110
SCHEDULER_COMPENSATION_WINDOW = datetime.timedelta(days=7)
SCHEDULER_MAX_COMPENSATIONS = 10
SCHEDULER_STATUS_FILE = Path(
    os.getenv("SCHEDULER_STATUS_FILE", "data/runtime/scheduler-status.json")
)
SCHEDULER_STANDBY_POLL_SECONDS = 1.0
_SCHEDULER_STATUS_LOCK = threading.Lock()
EXPERIMENT_QUALITY_SUMMARY_FIELDS = (
    "status", "expected", "observed", "expectedSamples", "observedSamples",
    "missingKeys", "treatmentTaskCount", "controlTaskCount",
    "controlActionCount", "contaminationCount", "actionFailed", "actionInconclusive", "unresolved",
    "retryCount", "dataDate", "calculationVersion",
)


def _experiment_quality_summary_complete(quality: object) -> bool:
    return (
        isinstance(quality, dict)
        and all(field in quality and quality[field] is not None for field in EXPERIMENT_QUALITY_SUMMARY_FIELDS)
        and isinstance(quality.get("missingKeys"), list)
    )


def _safe_quality_count(quality: object, key: str, fallback: int = 0) -> int:
    value = quality.get(key) if isinstance(quality, dict) else None
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else int(fallback)


def _get_no_result_extractor_type(brand: str) -> str:
    if brand == "summit":
        return "summit"
    return "rl" if brand in {"harbor", "riviera"} else "rds"


def _inventory_monitor_job_id(brand: str) -> str:
    return f"{INVENTORY_MONITOR_JOB_PREFIX}{brand}"


def _skipped_brands_summary_suffix(result: object) -> str:
    """Suffix for the job terminal summary listing anomalously skipped brands.

    异常跳过的品牌（如品牌锁被断码巡检占用导致 batch_busy）必须写进终态摘要，
    否则该品牌当天静默缺失，事后只能靠并发任务时间窗反推。
    """
    if not isinstance(result, Mapping):
        return ""
    skipped_brands = result.get("skippedBrands")
    if not isinstance(skipped_brands, list) or not skipped_brands:
        return ""
    skipped_text = ",".join(str(item) for item in skipped_brands)[:200]
    return f" skipped={skipped_text}"


def _inventory_monitor_start_dates(
    brands: list[str],
    now: Optional[datetime.datetime] = None,
) -> dict[str, datetime.datetime]:
    base = now or datetime.datetime.now()
    return {
        brand: base + datetime.timedelta(minutes=5 + index)
        for index, brand in enumerate(sorted(set(brands)))
    }


def _job_interval_minutes(job) -> Optional[int]:
    interval = getattr(getattr(job, "trigger", None), "interval", None)
    if interval is None:
        return None
    total_seconds = interval.total_seconds()
    return int(total_seconds // 60) if total_seconds % 60 == 0 else None


def _new_scheduler_owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


def scheduler_mode() -> str:
    mode = str(os.getenv("AIOPS_SCHEDULER_MODE") or "active").strip().lower()
    if mode not in {"active", "standby"}:
        raise ValueError("AIOPS_SCHEDULER_MODE must be one of: active, standby")
    return mode


def _scheduler_status_path() -> Path:
    path = SCHEDULER_STATUS_FILE
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path


def write_scheduler_status(*, ready: bool, mode: str, state: str) -> None:
    if mode not in {"active", "standby"}:
        raise ValueError("scheduler status mode is invalid")
    if state not in {"starting", "ready", "stopped", "rejected", "failed"}:
        raise ValueError("scheduler status state is invalid")
    target = _scheduler_status_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    payload = {
        "mode": mode,
        "ready": bool(ready),
        "state": state,
        "pid": os.getpid(),
        "updatedAt": time.time(),
    }
    with _SCHEDULER_STATUS_LOCK:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)


def _run_scheduler_standby(
    *,
    on_started: Callable[[], None] | None,
) -> bool:
    """Keep a candidate Scheduler pod alive without registering business jobs.

    Candidate deployments must prove the scheduler image and runtime inputs can
    start while remaining unable to schedule or execute production work.  The
    process intentionally has no leader lease or APScheduler instance in this
    mode; SIGTERM/SIGINT only changes its status and ends the process.
    """
    stopping = threading.Event()
    previous_handlers: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stopping.set()

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_stop)
        write_scheduler_status(ready=True, mode="standby", state="ready")
        if on_started is not None:
            on_started()
        while not stopping.wait(SCHEDULER_STANDBY_POLL_SECONDS):
            write_scheduler_status(ready=True, mode="standby", state="ready")
        write_scheduler_status(ready=False, mode="standby", state="stopped")
        return True
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def _scheduler_config_fingerprint() -> str:
    snapshot = json.dumps(
        {
            "business": GLOBAL_BUSINESS_CONFIG,
            "safety": GLOBAL_SAFETY_CONFIG,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


class SchedulerRunExecutor:
    """Bind submitted APScheduler slots to one durable leader fence."""

    def __init__(
        self,
        *,
        leader_name: str,
        owner_id: str,
        fencing_token: int,
        release: str,
        config_fingerprint: str,
        lease_seconds: int,
        compensation_handlers: dict[str, Callable[..., Any]] | None = None,
        session_factory=SessionLocal,
        heartbeat_factory=SchedulerLeaseHeartbeat,
        leader_heartbeat: SchedulerLeaseHeartbeat | None = None,
        active_run_tracker: ActiveRunTracker | None = None,
    ) -> None:
        self.leader_name = leader_name
        self.owner_id = owner_id
        self.fencing_token = fencing_token
        self.release = release
        self.config_fingerprint = config_fingerprint
        self.lease_seconds = lease_seconds
        self._compensation_handlers = (
            compensation_handlers if compensation_handlers is not None else {}
        )
        self._session_factory = session_factory
        self._heartbeat_factory = heartbeat_factory
        self._leader_heartbeat = leader_heartbeat
        self._active_run_tracker = active_run_tracker
        self._submitted_slots = defaultdict(deque)
        # APScheduler submits the worker before it dispatches EVENT_JOB_SUBMITTED.
        # A plain queue lookup therefore races the worker and can skip the job
        # before the business callback is reached.
        self._slot_condition = threading.Condition()

    def _complete_run_with_retry(
        self,
        db,
        run_id: int,
        *,
        job_id: str,
        claim_owner: str,
        claim_token: str,
        status: str,
        terminal_summary: str | None = None,
    ) -> bool:
        """Write the terminal fact without turning a successful business job into a failure.

        MySQL may drop a connection while the business callback is running.  The
        first write is attempted on the existing session; an OperationalError
        causes a rollback and one fresh-session retry so the run fact can still
        be read back without masking the callback result.
        """
        kwargs = {
            "claim_owner": claim_owner,
            "claim_token": claim_token,
            "leader_name": self.leader_name,
            "owner_id": self.owner_id,
            "fencing_token": self.fencing_token,
            "status": status,
            "terminal_summary": terminal_summary,
        }
        current_db = db
        retry_db = None
        try:
            for attempt in range(2):
                try:
                    complete_scheduler_job_run(current_db, run_id, **kwargs)
                    return True
                except OperationalError:
                    try:
                        current_db.rollback()
                    except BaseException:
                        pass
                    if attempt == 1:
                        raise
                    retry_db = self._session_factory()
                    current_db = retry_db
            return False
        except BaseException:
            logging.exception(
                "scheduler terminal fact write failed: job_id=%s run_id=%s status=%s",
                job_id,
                run_id,
                status,
            )
            return False
        finally:
            if retry_db is not None:
                retry_db.close()

    def record_scheduled_runs(self, event) -> None:
        job_id = getattr(event, "job_id", None)
        scheduled_run_times = getattr(event, "scheduled_run_times", None)
        if not isinstance(job_id, str) or not job_id or not scheduled_run_times:
            return
        with self._slot_condition:
            self._submitted_slots[job_id].extend(scheduled_run_times)
            self._slot_condition.notify_all()

    def bind(self, job_id: str, job_func: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args, **kwargs):
            return self.execute(job_id, job_func, *args, **kwargs)

        setattr(run, "__scheduler_original_func__", job_func)
        return run

    def _next_scheduled_for(self, job_id: str) -> datetime.datetime | None:
        with self._slot_condition:
            deadline = time.monotonic() + 5
            while True:
                slots = self._submitted_slots.get(job_id)
                if slots:
                    return slots.popleft()
                # The submission event is dispatched by the scheduler thread
                # after the executor has started the worker. Wait for that
                # event instead of treating the normal dispatch ordering as a
                # missing slot. Spurious wakeups and events for other jobs are
                # harmless because the predicate is checked in the loop.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._slot_condition.wait(timeout=remaining)

    @staticmethod
    def _idempotency_key(job_id: str, scheduled_for: datetime.datetime) -> str:
        normalized = scheduled_for.astimezone(datetime.timezone.utc).replace(microsecond=0)
        slot = normalized.isoformat().replace("+00:00", "Z")
        return f"scheduler-run:v1:{job_id}:{slot}"

    def execute(self, job_id: str, job_func: Callable[..., Any], *args, **kwargs):
        if self._active_run_tracker is not None and not self._active_run_tracker.enter():
            logging.info("scheduler run skipped during termination drain: %s", job_id)
            return None
        try:
            return self._execute_fenced_run(job_id, job_func, *args, **kwargs)
        finally:
            if self._active_run_tracker is not None:
                self._active_run_tracker.exit()

    def _execute_fenced_run(
        self, job_id: str, job_func: Callable[..., Any], *args, **kwargs
    ):
        scheduled_for = self._next_scheduled_for(job_id)
        if scheduled_for is None:
            logging.error("scheduler run skipped because no submitted slot was recorded: %s", job_id)
            return None
        db = self._session_factory()
        try:
            if self._leader_heartbeat is not None:
                self._leader_heartbeat.assert_owned()
            try:
                renew_scheduler_leader_lease(
                    db,
                    leader_name=self.leader_name,
                    owner_id=self.owner_id,
                    fencing_token=self.fencing_token,
                    lease_seconds=self.lease_seconds,
                )
            except SchedulerLeaderFenceError:
                logging.error("scheduler run skipped because leader fence is stale: %s", job_id)
                return None

            idempotency_key = self._idempotency_key(job_id, scheduled_for)
            try:
                run = create_scheduler_job_run(
                    db,
                    job_id=job_id,
                    scheduled_for=scheduled_for,
                    idempotency_key=idempotency_key,
                    release=self.release,
                    config_fingerprint=self.config_fingerprint,
                )
            except SchedulerJobRunStateError:
                run = db.query(SchedulerJobRun).filter(
                    SchedulerJobRun.idempotency_key == idempotency_key
                ).one_or_none()
                if run is None:
                    logging.error("scheduler run slot conflict cannot be read back: %s", job_id)
                    return None
            claim_token = uuid.uuid4().hex
            try:
                claim_scheduler_job_run(
                    db,
                    run.id,
                    claim_owner=self.owner_id,
                    claim_token=claim_token,
                    leader_name=self.leader_name,
                    owner_id=self.owner_id,
                    fencing_token=self.fencing_token,
                )
                start_scheduler_job_run(
                    db,
                    run.id,
                    claim_owner=self.owner_id,
                    claim_token=claim_token,
                    leader_name=self.leader_name,
                    owner_id=self.owner_id,
                    fencing_token=self.fencing_token,
                )
            except (SchedulerJobRunClaimError, SchedulerJobRunStateError):
                logging.info("scheduler run is already claimed or terminal: %s", job_id)
                return None

            run_heartbeat = self._heartbeat_factory(
                leader_name=self.leader_name,
                owner_id=self.owner_id,
                fencing_token=self.fencing_token,
                lease_seconds=self.lease_seconds,
                session_factory=self._session_factory,
                run_id=run.id,
                claim_owner=self.owner_id,
                claim_token=claim_token,
            )
            try:
                run_heartbeat.start()
            except BaseException:
                try:
                    if self._leader_heartbeat is not None:
                        self._leader_heartbeat.assert_owned()
                    complete_scheduler_job_run(
                        db,
                        run.id,
                        claim_owner=self.owner_id,
                        claim_token=claim_token,
                        leader_name=self.leader_name,
                        owner_id=self.owner_id,
                        fencing_token=self.fencing_token,
                        status="execution_unknown",
                        terminal_summary="scheduler run heartbeat failed before job execution",
                    )
                except (SchedulerJobRunStateError, SchedulerLeaderFenceError):
                    logging.exception(
                        "scheduler run heartbeat start failure could not be terminalized: %s",
                        job_id,
                    )
                raise
            started_at = time.monotonic()
            try:
                result = job_func(*args, **kwargs)
            except BaseException:
                duration_ms = int(round((time.monotonic() - started_at) * 1000))
                run_heartbeat.stop()
                try:
                    if self._leader_heartbeat is not None:
                        self._leader_heartbeat.assert_owned()
                    run_heartbeat.assert_owned()
                    self._complete_run_with_retry(
                        db,
                        run.id,
                        job_id=job_id,
                        claim_owner=self.owner_id,
                        claim_token=claim_token,
                        status="failed",
                        terminal_summary=f"scheduler job failed duration_ms={duration_ms}",
                    )
                except (SchedulerJobRunStateError, SchedulerLeaderFenceError):
                    logging.exception("scheduler failure could not be terminalized: %s", job_id)
                logging.info(
                    "scheduler job finished brand=%s job_id=%s status=failed duration_ms=%s",
                    job_id.split(":", 1)[1] if ":" in job_id else "",
                    job_id,
                    duration_ms,
                )
                raise
            finally:
                run_heartbeat.stop()
            duration_ms = int(round((time.monotonic() - started_at) * 1000))
            if self._leader_heartbeat is not None:
                self._leader_heartbeat.assert_owned()
            run_heartbeat.assert_owned()
            result_status = str(result.get("status") or "").strip().lower() if isinstance(result, Mapping) else ""
            terminal_status = "failed" if result_status in {"error", "failed"} else "succeeded"
            error_code = str(result.get("error_code") or result.get("errorCode") or "").strip() if isinstance(result, Mapping) else ""
            error_message = str(result.get("error_message") or result.get("errorMessage") or result.get("error") or "").strip() if isinstance(result, Mapping) else ""
            outcome = "failed" if terminal_status == "failed" else "succeeded"
            terminal_summary = f"scheduler job {outcome} duration_ms={duration_ms}"
            if error_code or error_message:
                terminal_summary += f" error_code={error_code or 'UNKNOWN'} error_message={error_message or 'unspecified'}"
            terminal_summary += _skipped_brands_summary_suffix(result)
            self._complete_run_with_retry(
                db,
                run.id,
                job_id=job_id,
                claim_owner=self.owner_id,
                claim_token=claim_token,
                status=terminal_status,
                terminal_summary=terminal_summary,
            )
            logging.info(
                "scheduler job finished brand=%s job_id=%s status=%s duration_ms=%s",
                job_id.split(":", 1)[1] if ":" in job_id else "",
                job_id,
                terminal_status,
                duration_ms,
            )
            return result
        finally:
            db.close()

    def execute_compensation(self, run_id: int, job_id: str):
        """Execute one persisted compensation only after allowlist and fenced claim."""
        if self._active_run_tracker is not None and not self._active_run_tracker.enter():
            logging.info("scheduler compensation skipped during termination drain: %s", job_id)
            return None
        try:
            return self._execute_compensation_fenced(run_id, job_id)
        finally:
            if self._active_run_tracker is not None:
                self._active_run_tracker.exit()

    def _execute_compensation_fenced(self, run_id: int, job_id: str):
        """Execute compensation after its caller has joined the drain tracker."""
        job_func = self._compensation_handlers.get(job_id)
        if (
            not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id <= 0
            or not callable(job_func)
        ):
            logging.error("scheduler compensation rejected before claim: %s", job_id)
            return None

        db = self._session_factory()
        try:
            run = db.get(SchedulerJobRun, run_id)
            scheduled_for = getattr(run, "scheduled_for", None)
            if (
                run is None
                or run.job_id != job_id
                or run.status != "planned"
                or run.attempt_no != 0
                or getattr(run, "release", None) != self.release
                or getattr(run, "config_fingerprint", None) != self.config_fingerprint
                or not isinstance(scheduled_for, datetime.datetime)
            ):
                logging.info("scheduler compensation is not an unclaimed planned run: %s", job_id)
                return None
            scheduled_at_utc = (
                scheduled_for.replace(tzinfo=datetime.timezone.utc)
                if scheduled_for.tzinfo is None
                else scheduled_for.astimezone(datetime.timezone.utc)
            )
            if run.idempotency_key != scheduler_compensation_idempotency_key(job_id, scheduled_at_utc):
                logging.error("scheduler compensation rejected because identity is invalid: %s", job_id)
                return None
            try:
                if self._leader_heartbeat is not None:
                    self._leader_heartbeat.assert_owned()
                renew_scheduler_leader_lease(
                    db,
                    leader_name=self.leader_name,
                    owner_id=self.owner_id,
                    fencing_token=self.fencing_token,
                    lease_seconds=self.lease_seconds,
                )
                claim_token = uuid.uuid4().hex
                claim_scheduler_job_run(
                    db,
                    run.id,
                    claim_owner=self.owner_id,
                    claim_token=claim_token,
                    leader_name=self.leader_name,
                    owner_id=self.owner_id,
                    fencing_token=self.fencing_token,
                )
                start_scheduler_job_run(
                    db,
                    run.id,
                    claim_owner=self.owner_id,
                    claim_token=claim_token,
                    leader_name=self.leader_name,
                    owner_id=self.owner_id,
                    fencing_token=self.fencing_token,
                )
            except (SchedulerLeaderFenceError, SchedulerJobRunClaimError, SchedulerJobRunStateError):
                logging.info("scheduler compensation rejected by leader fence or claim: %s", job_id)
                return None

            try:
                result = job_func()
            except BaseException:
                try:
                    if self._leader_heartbeat is not None:
                        self._leader_heartbeat.assert_owned()
                    complete_scheduler_job_run(
                        db,
                        run.id,
                        claim_owner=self.owner_id,
                        claim_token=claim_token,
                        leader_name=self.leader_name,
                        owner_id=self.owner_id,
                        fencing_token=self.fencing_token,
                        status="failed",
                        terminal_summary="scheduler compensation failed",
                    )
                except SchedulerJobRunStateError:
                    logging.exception("scheduler compensation failure could not be terminalized: %s", job_id)
                raise

            if self._leader_heartbeat is not None:
                self._leader_heartbeat.assert_owned()
            complete_scheduler_job_run(
                db,
                run.id,
                claim_owner=self.owner_id,
                claim_token=claim_token,
                leader_name=self.leader_name,
                owner_id=self.owner_id,
                fencing_token=self.fencing_token,
                status="succeeded",
            )
            return result
        finally:
            db.close()


def sync_inventory_monitor_jobs(scheduler, job_wrapper=None) -> None:
    """Reload config and reconcile per-brand inventory monitor intervals."""
    from src.config import BRAND_CONFIGS, is_brand_enabled

    reload_config()
    existing_jobs = {
        getattr(job, "id", ""): job
        for job in scheduler.get_jobs()
        if getattr(job, "id", "").startswith(
            (INVENTORY_MONITOR_JOB_PREFIX, *LEGACY_INVENTORY_MONITOR_JOB_PREFIXES)
        )
    }
    expected_job_ids: set[str] = set()
    enabled_brands = [brand for brand in BRAND_CONFIGS.keys() if is_brand_enabled(brand)]
    start_dates = _inventory_monitor_start_dates(enabled_brands)
    for brand in BRAND_CONFIGS.keys():
        job_id = _inventory_monitor_job_id(brand)
        if not is_brand_enabled(brand):
            continue
        business_config = get_brand_business_config(brand)
        inventory_check_interval_minutes = int(business_config.get("top_query_inventory_check_interval_minutes") or 60)
        expected_job_ids.add(job_id)
        existing_job = existing_jobs.get(job_id)
        if existing_job and _job_interval_minutes(existing_job) == inventory_check_interval_minutes:
            continue
        job_func = job_wrapper(job_id, inventory_monitor_job) if job_wrapper else inventory_monitor_job
        scheduler.add_job(
            job_func,
            'interval',
            minutes=inventory_check_interval_minutes,
            misfire_grace_time=600,
            coalesce=True,
            max_instances=1,
            kwargs={"brand_filter": brand},
            id=job_id,
            replace_existing=True,
            start_date=start_dates[brand],
        )

    for job_id in existing_jobs.keys():
        if job_id not in expected_job_ids:
            scheduler.remove_job(job_id)


def sync_query_governance_experiment_job(
    scheduler,
    workflow_factory: Callable[[str], Any] | None = None,
    job_wrapper: Callable[[str, Callable[..., Any]], Callable[..., Any]] | None = None,
) -> None:
    """Register the rewrite-owned A/B run in the real scheduler.

    The deterministic workflow is intentionally supplied by the mainline
    composition root.  Until that factory is registered, no recurring job is
    left behind that could accidentally execute a partial workflow.
    """
    existing = scheduler.get_job(QUERY_GOVERNANCE_EXPERIMENT_JOB_ID)
    if workflow_factory is None:
        if existing is not None:
            scheduler.remove_job(QUERY_GOVERNANCE_EXPERIMENT_JOB_ID)
        return
    job_func = (
        job_wrapper(
            QUERY_GOVERNANCE_EXPERIMENT_JOB_ID,
            query_governance_mainline_job,
        )
        if job_wrapper
        else query_governance_mainline_job
    )
    scheduler.add_job(
        job_func,
        "cron",
        hour=0,
        minute=30,
        misfire_grace_time=3600,
        kwargs={"workflow_factory": workflow_factory},
        id=QUERY_GOVERNANCE_EXPERIMENT_JOB_ID,
        replace_existing=True,
    )


def sync_query_governance_remediation_job(
    scheduler,
    workflow_factory: Callable[[str], Any] | None = None,
    job_wrapper: Callable[[str, Callable[..., Any]], Callable[..., Any]] | None = None,
) -> None:
    """Register the idempotent async remediation/retag verification worker."""
    existing = scheduler.get_job(QUERY_GOVERNANCE_REMEDIATION_JOB_ID)
    if workflow_factory is None:
        if existing is not None:
            scheduler.remove_job(QUERY_GOVERNANCE_REMEDIATION_JOB_ID)
        return
    job_func = (
        job_wrapper(QUERY_GOVERNANCE_REMEDIATION_JOB_ID, query_governance_remediation_job)
        if job_wrapper
        else query_governance_remediation_job
    )
    scheduler.add_job(
        job_func,
        "interval",
        minutes=5,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
        kwargs={"workflow_factory": workflow_factory},
        id=QUERY_GOVERNANCE_REMEDIATION_JOB_ID,
        replace_existing=True,
    )


def sync_query_governance_recovery_job(
    scheduler,
    workflow_factory: Callable[[str], Any] | None = None,
    job_wrapper: Callable[[str, Callable[..., Any]], Callable[..., Any]] | None = None,
) -> None:
    """Register the bounded checkpoint recovery worker for interrupted runs."""
    existing = scheduler.get_job(QUERY_GOVERNANCE_RECOVERY_JOB_ID)
    if workflow_factory is None:
        if existing is not None:
            scheduler.remove_job(QUERY_GOVERNANCE_RECOVERY_JOB_ID)
        return
    job_func = (
        job_wrapper(QUERY_GOVERNANCE_RECOVERY_JOB_ID, query_governance_recovery_job)
        if job_wrapper
        else query_governance_recovery_job
    )
    scheduler.add_job(
        job_func,
        "interval",
        minutes=5,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
        kwargs={"workflow_factory": workflow_factory},
        id=QUERY_GOVERNANCE_RECOVERY_JOB_ID,
        replace_existing=True,
    )


def sync_query_governance_summary_reconciliation_job(
    scheduler,
    job_wrapper: Callable[[str, Callable[..., Any]], Callable[..., Any]] | None = None,
) -> None:
    """Register the bounded, idempotent batch-summary repair worker."""
    job_func = (
        job_wrapper(
            QUERY_GOVERNANCE_SUMMARY_RECONCILIATION_JOB_ID,
            query_governance_summary_reconciliation_job,
        )
        if job_wrapper
        else query_governance_summary_reconciliation_job
    )
    scheduler.add_job(
        job_func,
        "interval",
        minutes=5,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
        id=QUERY_GOVERNANCE_SUMMARY_RECONCILIATION_JOB_ID,
        replace_existing=True,
    )


def sync_query_governance_image_integrity_job(
    scheduler,
    job_wrapper: Callable[[str, Callable[..., Any]], Callable[..., Any]] | None = None,
) -> None:
    """Register the daily read-only image integrity audit with Feishu alerting.

    每日任务约 16:45 生成、计划约 3 小时后产出，20:30 巡检可覆盖当天数据；
    索引再次写入共享错图时当天推送飞书告警。
    """
    job_func = (
        job_wrapper(
            QUERY_GOVERNANCE_IMAGE_INTEGRITY_JOB_ID,
            query_governance_image_integrity_job,
        )
        if job_wrapper
        else query_governance_image_integrity_job
    )
    scheduler.add_job(
        job_func,
        "cron",
        hour=20,
        minute=30,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
        id=QUERY_GOVERNANCE_IMAGE_INTEGRITY_JOB_ID,
        replace_existing=True,
    )

def _get_health_check_api_url() -> str:
    return os.getenv("HEALTH_CHECK_API_URL", "http://api:8000/api/health")


def _build_health_retry_result(name: str, ok: bool, detail: dict = None, error: str = None, attempts: int = 1) -> dict:
    return {
        "name": name,
        "ok": ok,
        "detail": detail or {},
        "error": error,
        "attempts": attempts,
    }


def _run_probe_with_retry(name: str, probe_func, retries: int = 3, heal_func = None) -> dict:
    last_result = _build_health_retry_result(name, False, error="probe_not_run", attempts=0)
    for attempt in range(1, retries + 1):
        try:
            result = probe_func()
            result["attempts"] = attempt
            if result.get("ok"):
                return result
            last_result = result
        except Exception as e:
            last_result = _build_health_retry_result(name, False, error=str(e), attempts=attempt)
        if attempt < retries:
            if heal_func:
                heal_func()
            time.sleep(1)
    return last_result


def _probe_api_health() -> dict:
    response = requests.get(_get_health_check_api_url(), timeout=5)
    if response.status_code != 200:
        return _build_health_retry_result(
            "api_probe",
            False,
            detail={"status_code": response.status_code},
            error=f"health endpoint returned {response.status_code}",
        )
    payload = response.json()
    if not isinstance(payload, dict):
        return _build_health_retry_result(
            "api_probe",
            False,
            detail={},
            error="health endpoint returned invalid payload",
        )
    database_connected = bool(payload.get("database", {}).get("connected"))
    if not database_connected:
        return _build_health_retry_result(
            "api_probe",
            False,
            detail=payload,
            error="health endpoint database.connected=false",
        )
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("available") is not True:
        return _build_health_retry_result(
            "api_probe",
            False,
            detail=payload,
            error="health endpoint runtime unavailable",
        )
    if runtime.get("is_circuit_broken") is not False:
        return _build_health_retry_result(
            "api_probe",
            False,
            detail=payload,
            error="health endpoint runtime circuit is open",
        )
    return _build_health_retry_result(
        "api_probe",
        True,
        detail={
            **payload,
            "aggregateHealthy": payload.get("healthy"),
            "aggregateHealthyIgnored": payload.get("healthy") is False,
        },
    )


def _probe_db_freshness(stale_minutes: int) -> dict:
    now = datetime.datetime.now()
    issues = []
    warnings = []
    auto_healed = {
        "service_health_check_job": mark_stale_running_sessions_failed("service_health_check_job", stale_minutes),
    }
    health_session = get_latest_task_session("service_health_check_job")
    activity = get_recent_table_activity(stale_minutes)

    if not check_database_connection():
        issues.append("database_unreachable")

    if health_session and health_session.status == "running" and health_session.start_time and health_session.start_time < now - datetime.timedelta(minutes=stale_minutes):
        issues.append("health_check_stuck_running")

    return _build_health_retry_result(
        "db_freshness_check",
        not issues,
        detail={
            "activity": activity,
            "latest_health_check": {
                "status": health_session.status,
                "start_time": health_session.start_time.isoformat() if health_session and health_session.start_time else None,
            } if health_session else None,
            "auto_healed": auto_healed,
            "warnings": warnings,
        },
        error=";".join(issues) if issues else None,
    )


def _probe_brand_readonly(brand: str) -> dict:
    sample = get_latest_abnormal_query_for_brand(brand)
    if not sample or not sample.query_text:
        return {
            "brand": brand,
            "ok": True,
            "skipped": True,
            "reason": "no_local_query_sample",
        }

    service = OperationalService()
    search_result = service.search_products_with_total(brand, sample.query_text, page_size=1)
    if not isinstance(search_result, dict):
        return {
            "brand": brand,
            "ok": False,
            "skipped": False,
            "sample_query": sample.query_text,
            "result_count": None,
            "error_reason": "invalid_search_response",
        }
    products = search_result.get("products")
    success = search_result.get("success")
    if not isinstance(products, list) or not isinstance(success, bool):
        return {
            "brand": brand,
            "ok": False,
            "skipped": False,
            "sample_query": sample.query_text,
            "result_count": None,
            "error_reason": "invalid_search_response",
        }
    if not success:
        return {
            "brand": brand,
            "ok": False,
            "skipped": False,
            "sample_query": sample.query_text,
            "result_count": None,
            "error_reason": search_result.get("error_reason") or "search_failed",
        }
    raw_result_count = search_result.get("total_count")
    if (
        brand == "summit"
        and "total_count" in search_result
        and raw_result_count is None
        and search_result.get("result_complete") is False
        and search_result.get("error_reason") is None
        and all(isinstance(product, dict) for product in products)
    ):
        return {
            "brand": brand,
            "ok": True,
            "skipped": False,
            "sample_query": sample.query_text,
            "result_count": None,
            "result_count_known": False,
            "observed_result_count": len(products),
            "error_reason": None,
        }
    if (
        not isinstance(raw_result_count, int)
        or isinstance(raw_result_count, bool)
        or raw_result_count < len(products)
    ):
        return {
            "brand": brand,
            "ok": False,
            "skipped": False,
            "sample_query": sample.query_text,
            "result_count": None,
            "observed_result_count": len(products),
            "error_reason": "invalid_search_response",
        }
    result_count = raw_result_count
    return {
        "brand": brand,
        "ok": True,
        "skipped": False,
        "sample_query": sample.query_text,
        "result_count": result_count,
        "observed_result_count": len(products),
        "error_reason": None,
    }


def _probe_external_readonly() -> dict:
    from src.config import BRAND_CONFIGS, is_brand_enabled

    enabled_brands = [brand for brand in BRAND_CONFIGS.keys() if is_brand_enabled(brand)]
    if not enabled_brands:
        return _build_health_retry_result(
            "external_readonly_probe",
            True,
            detail={"brands": {}, "warnings": ["no_enabled_brands"]},
        )

    results = {}

    def run_single_brand(brand: str) -> tuple[str, dict]:
        def heal():
            OperationalService.clear_brand_config_cache()

        def probe_once():
            detail = _probe_brand_readonly(brand)
            return _build_health_retry_result(
                f"external_readonly_probe:{brand}",
                detail.get("ok", False),
                detail=detail,
            )

        probe_result = _run_probe_with_retry(
            f"external_readonly_probe:{brand}",
            probe_once,
            retries=3,
            heal_func=heal,
        )
        return brand, probe_result

    worker_count = min(4, len(enabled_brands))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        for brand, result in executor.map(run_single_brand, enabled_brands):
            results[brand] = result

    failed_brands = [brand for brand, result in results.items() if not result.get("ok")]
    return _build_health_retry_result(
        "external_readonly_probe",
        not failed_brands,
        detail={"brands": results},
        error=f"failed_brands={','.join(failed_brands)}" if failed_brands else None,
    )


def _probe_performance() -> dict:
    window_minutes = int(GLOBAL_BUSINESS_CONFIG.get("health_check_interval_minutes", 60))
    slow_threshold = float(GLOBAL_SAFETY_CONFIG.get("api_delay_threshold", 5))
    p95_threshold = float(max(
        GLOBAL_SAFETY_CONFIG.get("health_check_api_p95_threshold_seconds", slow_threshold),
        slow_threshold,
    ))
    failure_rate_threshold = float(GLOBAL_SAFETY_CONFIG.get("health_check_error_rate_threshold_percent", 20))
    metrics = get_recent_operation_metrics(
        window_minutes=window_minutes,
        slow_threshold=slow_threshold,
        modules=["OperationalService", "SummitService"],
    )
    issues = []

    if metrics["total_count"] > 0 and metrics["failure_rate_percent"] > failure_rate_threshold:
        issues.append(f"failure_rate_percent={metrics['failure_rate_percent']}")
    if metrics["total_count"] > 0 and metrics["p95_duration_seconds"] > p95_threshold:
        issues.append(f"p95_duration_seconds={metrics['p95_duration_seconds']}")

    return _build_health_retry_result(
        "performance_check",
        not issues,
        detail={
            **metrics,
            "slow_threshold_seconds": slow_threshold,
            "p95_threshold_seconds": p95_threshold,
            "failure_rate_threshold_percent": failure_rate_threshold,
        },
        error=";".join(issues) if issues else None,
    )


def _scheduler_authoritative_query_demotion_group(
    action: TopQueryActionPlan,
    actions_by_id: dict[int, TopQueryActionPlan],
    *,
    required_status: str,
) -> tuple[dict, list[TopQueryActionPlan]]:
    metadata_state, metadata = _TopQueryGovernanceServiceContract._query_demote_group_metadata_result(
        action
    )
    if metadata_state != "valid" or metadata is None:
        raise ValueError(f"query demote group metadata is {metadata_state}")
    action_ids = list(metadata["actionIds"])
    group = [actions_by_id.get(action_id) for action_id in action_ids]
    required_metadata_keys = set(metadata)
    if (
        any(peer is None for peer in group)
        or int(metadata["leaderActionId"]) not in action_ids
        or any(
            str(peer.status or "") != required_status
            or str(peer.action_type or "") != "query_demote"
            or not uses_authoritative_query_demotion(
                json.loads(peer.request_payload or "{}")
            )
            or _TopQueryGovernanceServiceContract._query_demote_group_metadata(peer) != metadata
            or int(peer.task_id) != int(action.task_id)
            or normalize_query_key(peer.brand) != normalize_query_key(metadata["brand"])
            or normalize_governance_query_key(peer.brand, peer.query_text)
            != normalize_governance_query_key(metadata["brand"], metadata["normalizedQuery"])
            or (
                required_status in {"verifying", "rollback_pending"}
                and not required_metadata_keys.issubset(
                    json.loads(peer.after_snapshot or "{}")
                )
            )
            for peer in group
        )
    ):
        raise ValueError("query demote authoritative group identity mismatch")
    return metadata, group


def query_demotion_verification_job() -> dict:
    db = SessionLocal()
    try:
        now = datetime.datetime.now()
        actions = db.query(TopQueryActionPlan).filter(
            (
                (
                    TopQueryActionPlan.action_type == "query_demote"
                )
                & (
                    TopQueryActionPlan.status.in_([
                        "execution_unknown",
                        "verifying",
                        "rollback_pending",
                    ])
                    | (
                        TopQueryActionPlan.status.in_(["executing", "reconciling"])
                        & (
                            TopQueryActionPlan.execution_lease_expires_at.is_(None)
                            | (TopQueryActionPlan.execution_lease_expires_at <= now)
                        )
                    )
                )
            )
            | (
                (TopQueryActionPlan.action_type != "query_demote")
                & TopQueryActionPlan.external_request_id.is_not(None)
                & (
                    (TopQueryActionPlan.status == "execution_unknown")
                    | (
                        TopQueryActionPlan.status.in_(["executing", "reconciling"])
                        & (
                            TopQueryActionPlan.execution_lease_expires_at.is_(None)
                            | (TopQueryActionPlan.execution_lease_expires_at <= now)
                        )
                    )
                )
            ),
        ).order_by(TopQueryActionPlan.id.asc()).all()
        actions_by_id = {int(action.id): action for action in actions}
        representatives = []
        preflight_errors = []
        seen_groups = set()
        for action in actions:
            action_id = int(action.id)
            try:
                try:
                    request_payload = json.loads(action.request_payload or "{}")
                except (TypeError, json.JSONDecodeError):
                    request_payload = {}
                status = str(action.status or "")
                is_generic_action = str(action.action_type or "") != "query_demote"
                if is_generic_action:
                    if not str(action.external_request_id or "").strip():
                        raise ValueError("generic action reconciliation requires external_request_id")
                    if status == "executing":
                        operation = "reconcile_expired_action_execution"
                    elif status in {"execution_unknown", "reconciling"}:
                        operation = "reconcile_unknown"
                    else:
                        continue
                    group_key = (operation, "generic", action_id)
                    is_authoritative = False
                else:
                    is_authoritative = uses_authoritative_query_demotion(request_payload)
                    if not is_authoritative:
                        if status not in {"execution_unknown", "reconciling"}:
                            continue
                        operation = "reconcile_unknown"
                        group_key = (operation, "legacy", action_id)
                    elif status == "rollback_pending":
                        source_group_request_id = str(
                            request_payload.get("sourceGroupExternalRequestId") or ""
                        ).strip()
                        raw_source_action_ids = request_payload.get("rollbackOfActionIds")
                        if (
                            request_payload.get("operation") != "restore"
                            or not source_group_request_id
                            or not isinstance(raw_source_action_ids, list)
                            or not raw_source_action_ids
                            or any(isinstance(peer_id, bool) for peer_id in raw_source_action_ids)
                        ):
                            raise ValueError("query demote rollback pending identity is incomplete")
                        try:
                            source_action_ids = tuple(sorted({
                                int(peer_id) for peer_id in raw_source_action_ids
                            }))
                        except (TypeError, ValueError):
                            raise ValueError(
                                "query demote rollback pending identity is incomplete"
                            ) from None
                        operation = "reconcile_rollback"
                        group_key = (
                            operation,
                            source_group_request_id,
                            source_action_ids,
                        )
                    elif status == "executing":
                        metadata, group = _scheduler_authoritative_query_demotion_group(
                            action,
                            actions_by_id,
                            required_status=status,
                        )
                        identity = (
                            metadata["groupExternalRequestId"],
                            int(metadata["leaderActionId"]),
                            tuple(int(peer_id) for peer_id in metadata["actionIds"]),
                        )
                        operation = "reconcile_expired_execution"
                        group_key = (operation, identity)
                    elif status in {"execution_unknown", "reconciling"}:
                        metadata, group = _scheduler_authoritative_query_demotion_group(
                            action,
                            actions_by_id,
                            required_status=status,
                        )
                        identity = (
                            metadata["groupExternalRequestId"],
                            int(metadata["leaderActionId"]),
                            tuple(int(peer_id) for peer_id in metadata["actionIds"]),
                        )
                        operation = "reconcile_unknown"
                        group_key = (operation, identity)
                    else:
                        metadata, group = _scheduler_authoritative_query_demotion_group(
                            action,
                            actions_by_id,
                            required_status=status,
                        )
                        operation = "refresh_verification"
                        group_key = (
                            operation,
                            metadata["groupExternalRequestId"],
                            tuple(metadata["actionIds"]),
                        )
            except Exception as exc:
                preflight_errors.append({
                    "action_id": action_id,
                    "error": sanitize_action_error(exc),
                })
                continue
            if group_key in seen_groups:
                continue
            seen_groups.add(group_key)
            representative_id = (
                action_id
                if operation == "reconcile_rollback"
                else int(metadata["leaderActionId"])
                if is_authoritative
                else action_id
            )
            representatives.append((operation, representative_id))
    finally:
        db.close()

    service = TopQueryGovernanceService()
    summary = {
        "scanned": len(representatives) + len(preflight_errors),
        "processed": 0,
        "busy": 0,
        "skipped": 0,
        "failed": len(preflight_errors),
        "errors": list(preflight_errors),
    }
    for operation, action_id in representatives:
        try:
            if operation == "reconcile_unknown":
                result = service.reconcile_unknown_action(action_id)
            elif operation == "reconcile_expired_action_execution":
                result = service.reconcile_expired_action_execution(action_id)
            elif operation == "reconcile_expired_execution":
                result = service.reconcile_expired_query_demote_execution(action_id)
            elif operation == "reconcile_rollback":
                result = service.reconcile_pending_query_demote_rollback(action_id)
            else:
                result = service.refresh_query_demotion_verification(action_id)
            if isinstance(result, dict) and result.get("status") == "busy":
                summary["busy"] += 1
                continue
            if isinstance(result, dict) and result.get("processed") is False:
                summary["skipped"] += 1
                continue
            summary["processed"] += 1
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append({
                "action_id": action_id,
                "error": sanitize_action_error(exc),
            })
    summary["status"] = "partial_failed" if summary["failed"] else "success"
    return summary


def agent_loop_governance_job(
    brand_filter: str = None,
    target_date: datetime.date = None,
    dry_run: bool = True,
):
    """旧 Top Query Agent Loop 已退役，不允许再次启动。"""
    raise LegacyAgentLoopRetiredError()


def query_governance_experiment_job(
    brand_filter: str | None = None,
    target_date: datetime.date | None = None,
    *,
    workflow_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Run rewrite-owned A/B samples through the deterministic workflow.

    The node adapters are supplied by the deterministic mainline owner; this
    entrypoint only handles scheduler fan-out and experiment routing.
    """
    if workflow_factory is None:
        raise RuntimeError("QUERY_GOVERNANCE_WORKFLOW_FACTORY_REQUIRED")
    reload_config()
    from src.config import BRAND_CONFIGS, is_brand_enabled

    target = target_date or (datetime.date.today() - datetime.timedelta(days=1))
    brands = [brand_filter] if brand_filter else list(BRAND_CONFIGS.keys())
    runner = QueryGovernanceExperimentScheduler(SessionLocal, workflow_factory)
    results: dict[str, Any] = {}
    for brand in brands:
        if not brand or not is_brand_enabled(brand):
            continue
        batch_id = f"query-governance:{brand}:{target.isoformat()}"
        try:
            results[brand] = runner.run_brand(
                brand, data_date=target, batch_id=batch_id
            )
        except Exception as exc:
            results[brand] = {
                "brand": brand,
                "status": "error",
                "errorCode": "QUERY_GOVERNANCE_EXPERIMENT_FAILED",
                "errorMessage": str(exc) or exc.__class__.__name__,
            }
    return {
        "dataDate": target.isoformat(),
        "status": "error" if any(item.get("status") == "error" for item in results.values()) else "success",
        "brands": results,
    }


def _legacy_agent_loop_governance_job(
    brand_filter: str = None,
    target_date: datetime.date = None,
    dry_run: bool = True,
):
    """保留旧实现作为迁移参考，不作为运行入口。"""
    knowledge_date = datetime.date.today()
    reload_config()
    session_id = create_task_session("agent_loop_governance_job")
    target_date = target_date or (knowledge_date - datetime.timedelta(days=1))
    summary = {"target_date": target_date.isoformat(), "dry_run": dry_run, "brands": {}}
    has_failures = False
    try:
        update_task_session(session_id, phase="Creating_Top_Query_Tasks")
        from src.config import BRAND_CONFIGS, is_brand_enabled

        service = TopQueryGovernanceService()
        experiment_service = TopQueryExperimentService()
        execution_date = target_date + datetime.timedelta(days=1)
        brands = [brand_filter] if brand_filter else list(BRAND_CONFIGS.keys())
        activation_brands = [
            brand
            for brand in brands
            if is_brand_enabled(brand)
            and get_brand_business_config(brand).get("top_query_governance_enabled") is not False
        ]
        transition_result = experiment_service.transition_scheduled_experiments(
            execution_date=execution_date,
            governance_service=service,
            brands=activation_brands,
        )
        activation_failures = transition_result.get("failures", []) if isinstance(transition_result, dict) else []
        activations = transition_result.get("activations", []) if isinstance(transition_result, dict) else []
        activation_failures_by_brand = {
            str(item.get("brand")): item
            for item in activation_failures
            if item.get("brand")
        }
        activations_by_brand = {
            str(item.get("brand")): item
            for item in activations
            if isinstance(item, dict) and item.get("brand") and item.get("status") == "success"
        }
        for brand in brands:
            brand_summary = {"created": 0, "completed": 0, "manual_required": 0, "failed": 0, "skipped": 0}
            try:
                if not is_brand_enabled(brand):
                    continue
                if brand in activation_failures_by_brand:
                    failure = activation_failures_by_brand[brand]
                    brand_summary["experimentStatus"] = "activation_failed"
                    brand_summary["failed"] = 1
                    brand_summary["errors"] = [failure]
                    has_failures = True
                    summary["brands"][brand] = brand_summary
                    continue
                business_config = get_brand_business_config(brand)
                if business_config.get("top_query_governance_enabled") is False:
                    continue

                limit = int(business_config.get("query_governance_extract_limit") or 150)
                active_experiment = experiment_service.get_active_experiment_for_brand(brand, execution_date=execution_date)
                activation = activations_by_brand.get(brand)
                if activation:
                    activation_experiment_id = int(activation.get("experimentId") or 0)
                    active_experiment_id = int(active_experiment.get("id") or 0) if active_experiment else 0
                    if not active_experiment or active_experiment_id != activation_experiment_id:
                        brand_summary["experimentId"] = activation_experiment_id or None
                        brand_summary["experimentStatus"] = "activation_state_inconsistent"
                        brand_summary["failed"] = 1
                        brand_summary["errors"] = [{
                            "phase": "activation_state_validation",
                            "error": "activation_state_inconsistent",
                        }]
                        has_failures = True
                        summary["brands"][brand] = brand_summary
                        continue
                if active_experiment:
                    running_state_error = validate_running_experiment_state(active_experiment)
                    if running_state_error:
                        brand_summary["experimentId"] = active_experiment.get("id")
                        brand_summary["experimentStatus"] = running_state_error
                        brand_summary["failed"] = 1
                        brand_summary["errors"] = [{
                            "phase": "running_experiment_state_validation",
                            "error": running_state_error,
                        }]
                        has_failures = True
                        summary["brands"][brand] = brand_summary
                        continue
                experiment_id = active_experiment.get("id") if active_experiment else None
                snapshot = None
                if experiment_id:
                    brand_summary["experimentId"] = experiment_id
                    brand_summary["experimentMode"] = "running"
                    if activation and int(activation.get("experimentId") or 0) == int(experiment_id):
                        snapshot = activation.get("snapshot") or {}
                    else:
                        snapshot = experiment_service.prepare_daily_snapshot(int(experiment_id), target_date)
                    data_quality = snapshot.get("dataQuality") or {}
                    brand_summary["dataQuality"] = data_quality
                    brand_summary["experimentQuality"] = build_experiment_quality(
                        data_date=target_date,
                        data_quality=data_quality,
                        calculation_version=(
                            active_experiment.get("calculationVersion")
                            or ""
                        ),
                    )
                    if data_quality.get("status") != "complete":
                        brand_summary["experimentStatus"] = "data_quality_failed"
                        has_failures = True
                        summary["brands"][brand] = brand_summary
                        continue
                    if activation and int(activation.get("experimentId") or 0) == int(experiment_id):
                        created = {
                            "experiment_id": int(experiment_id),
                            "brand": brand,
                            "data_date": target_date.isoformat(),
                            "task_ids": list(activation.get("taskIds") or []),
                            "top_query_count": len(activation.get("taskIds") or []),
                        }
                    else:
                        created = service.create_experiment_tasks(
                            experiment_id=int(experiment_id),
                            brand=brand,
                            data_date=target_date,
                            treatment_rows=snapshot.get("treatmentRows") or [],
                        )
                else:
                    created = service.create_daily_tasks(brand, target_date, limit=limit, allowed_queries=None)
                task_ids = created.get("task_ids", [])
                brand_summary["created"] = len(task_ids)
                if experiment_id:
                    brand_summary["treatmentQueryCount"] = len(snapshot.get("treatmentRows") or [])
                    brand_summary["experimentQuality"] = build_experiment_quality(
                        data_date=target_date,
                        data_quality=snapshot.get("dataQuality") or {},
                        treatment_task_count=len(task_ids),
                        calculation_version=(
                            active_experiment.get("calculationVersion")
                            or ""
                        ),
                    )

                world_knowledge_snapshot = None
                if task_ids:
                    world_knowledge_snapshot = service.prepare_world_knowledge_batch_snapshot(
                        brand,
                        target_date,
                        knowledge_date,
                        scope_id=f"session:{session_id}:{brand}:{target_date.isoformat()}",
                    )
                    brand_summary["worldKnowledgeSnapshot"] = world_knowledge_snapshot.provenance()

                update_task_session(session_id, phase=f"Running_Agent_Loop:{brand}")
                for task_id in task_ids:
                    try:
                        result = service.run_task(
                            task_id,
                            dry_run=dry_run,
                            world_knowledge_snapshot=world_knowledge_snapshot,
                        )
                        final_status = result.get("final_status", "failed")
                        if final_status in brand_summary:
                            brand_summary[final_status] += 1
                        else:
                            brand_summary["failed"] += 1
                    except Exception as exc:
                        brand_summary["failed"] += 1
                        brand_summary.setdefault("errors", []).append({"task_id": task_id, "error": str(exc)})
                if experiment_id:
                    sync_result = experiment_service.sync_daily_metrics(
                        int(experiment_id),
                        target_date,
                        snapshot=snapshot,
                    )
                    sync_data_quality = sync_result.get("dataQuality") or {}
                    sync_experiment_quality = sync_result.get("experimentQuality")
                    if _experiment_quality_summary_complete(sync_experiment_quality):
                        brand_summary["experimentQuality"] = {
                            key: sync_experiment_quality.get(key)
                            for key in EXPERIMENT_QUALITY_SUMMARY_FIELDS
                        }
                    else:
                        action_summary = sync_result.get("actionSummary") or {}
                        treatment_task_count = _safe_quality_count(
                            sync_experiment_quality, "treatmentTaskCount", len(task_ids)
                        )
                        reported_unresolved = _safe_quality_count(
                            sync_experiment_quality,
                            "unresolved",
                            int(brand_summary.get("failed") or 0),
                        )
                        missing_treatment_tasks = max(0, 20 - treatment_task_count)
                        fallback_data_quality = {
                            "status": "failed",
                            "expected": _safe_quality_count(
                                sync_experiment_quality,
                                "expected",
                                _safe_quality_count(sync_experiment_quality, "expectedSamples", sync_data_quality.get("expected") or 40),
                            ),
                            "observed": _safe_quality_count(
                                sync_experiment_quality,
                                "observed",
                                _safe_quality_count(sync_experiment_quality, "observedSamples", sync_data_quality.get("observed") or 0),
                            ),
                            "missingKeys": (
                                list(sync_experiment_quality.get("missingKeys"))
                                if isinstance(sync_experiment_quality, dict)
                                and isinstance(sync_experiment_quality.get("missingKeys"), list)
                                else list(sync_data_quality.get("missingKeys") or [])
                            ),
                        }
                        brand_summary["experimentQuality"] = build_experiment_quality(
                            data_date=target_date,
                            data_quality=fallback_data_quality,
                            treatment_task_count=treatment_task_count,
                            control_task_count=_safe_quality_count(sync_experiment_quality, "controlTaskCount"),
                            control_action_count=_safe_quality_count(sync_experiment_quality, "controlActionCount"),
                            contamination_count=_safe_quality_count(
                                sync_experiment_quality,
                                "contaminationCount",
                                int(sync_data_quality.get("contaminationCount") or 0),
                            ),
                            action_failed=_safe_quality_count(
                                sync_experiment_quality,
                                "actionFailed",
                                int(action_summary.get("failed") or 0),
                            ),
                            action_inconclusive=_safe_quality_count(
                                sync_experiment_quality,
                                "actionInconclusive",
                                int(action_summary.get("inconclusive") or 0),
                            ),
                            existing_unresolved=max(0, reported_unresolved - missing_treatment_tasks),
                            retry_count=_safe_quality_count(sync_experiment_quality, "retryCount"),
                            force_failed=True,
                            calculation_version=(
                                active_experiment.get("calculationVersion")
                                or ""
                            ),
                        )
                    experiment_quality_passed = experiment_quality_gate_passed(
                        brand_summary["experimentQuality"]
                    )
                    sync_failed = bool(sync_result.get("failed")) or (
                        bool(sync_data_quality)
                        and sync_data_quality.get("status") != "complete"
                    )
                    if sync_data_quality:
                        brand_summary["dataQuality"] = sync_data_quality
                    if not experiment_quality_passed:
                        has_failures = True
                        brand_summary["experimentStatus"] = "experiment_quality_failed"
                    elif sync_failed:
                        has_failures = True
                        brand_summary["experimentStatus"] = (
                            "data_quality_failed"
                            if sync_data_quality.get("status") != "complete"
                            else "sync_failed"
                        )
                    planned_end_date_raw = active_experiment.get("plannedEndDate")
                    planned_end_date = (
                        datetime.date.fromisoformat(planned_end_date_raw)
                        if isinstance(planned_end_date_raw, str) and planned_end_date_raw
                        else None
                    )
                    if (
                        not sync_failed
                        and experiment_quality_passed
                        and planned_end_date
                        and execution_date >= planned_end_date
                        and brand_summary["failed"] == 0
                    ):
                        experiment_service.end_experiment(int(experiment_id))
                        brand_summary["experimentStatusAfterRun"] = "ended"
                if brand_summary["failed"] > 0:
                    has_failures = True
                summary["brands"][brand] = brand_summary
            except Exception as exc:
                has_failures = True
                brand_summary["failed"] += 1
                brand_summary.setdefault("errors", []).append({
                    "phase": "brand_pipeline",
                    "error": str(exc),
                })
                summary["brands"][brand] = brand_summary

        final_status = "partial_failed" if has_failures else "success"
        update_task_session(session_id, phase="Finished", status=final_status, summary=summary)
        return {"data_date": target_date.isoformat(), "dry_run": dry_run, "status": final_status, "summary": summary}
    except Exception as e:
        import traceback
        update_task_session(session_id, status="failed", error=traceback.format_exc(), summary=summary)
        return {"data_date": target_date.isoformat(), "dry_run": dry_run, "status": "failed", "summary": summary, "error": str(e)}


def no_result_governance_extract_job(target_date: datetime.date = None):
    """每日拉取各品牌无结果词并写入本地治理批次。"""
    reload_config()
    session_id = create_task_session("no_result_governance_extract_job")
    supported_brands = {"summit", "harbor", "riviera"}
    target_date = target_date or (datetime.date.today() - datetime.timedelta(days=1))
    summary = {"target_date": target_date.strftime("%Y-%m-%d"), "brands": {}}

    try:
        update_task_session(session_id, phase="Extracting_No_Result_Queries")
        from src.config import BRAND_CONFIGS, is_brand_enabled

        service = NoResultGovernanceService()
        for brand in BRAND_CONFIGS.keys():
            if brand not in supported_brands:
                summary["brands"][brand] = {"status": "skipped", "reason": "no_sql"}
                continue
            if not is_brand_enabled(brand):
                summary["brands"][brand] = {"status": "skipped", "reason": "disabled"}
                continue

            business_config = get_brand_business_config(brand)
            if business_config.get("no_result_governance_enabled") is not True:
                summary["brands"][brand] = {"status": "skipped", "reason": "no_result_disabled"}
                continue
            limit = int(business_config.get("no_result_extract_limit") or 100)
            excluded_keywords = business_config.get("no_result_excluded_keywords") or business_config.get("excluded_keywords") or []
            min_search_pv = business_config.get("no_result_search_pv_threshold")
            min_search_uv = business_config.get("no_result_search_uv_threshold")
            extractor = QueryExtractor(brand_type=_get_no_result_extractor_type(brand))
            try:
                raw_rows = extractor.get_no_result_queries(
                    brand,
                    target_date=target_date,
                    limit=limit,
                    excluded_keywords=excluded_keywords,
                    raise_on_error=True,
                )
            except Exception as exc:
                summary["brands"][brand] = {"status": "failed", "error": str(exc)}
                continue
            normalized = service.normalize_rows(
                brand=brand,
                target_date=datetime.datetime.combine(target_date, datetime.time.min),
                rows=raw_rows,
                excluded_keywords=excluded_keywords,
                min_search_pv=int(min_search_pv) if min_search_pv is not None else None,
                min_search_uv=int(min_search_uv) if min_search_uv is not None else None,
            )
            if normalized:
                batch = service.create_batch_from_records(
                    brand,
                    datetime.datetime.combine(target_date, datetime.time.min),
                    normalized,
                )
                summary["brands"][brand] = {"status": "created", "count": len(normalized), "batch_id": batch.id}
            else:
                summary["brands"][brand] = {"status": "empty", "count": 0}

        update_task_session(session_id, phase="Finished", status="success", summary=summary)
        return summary
    except Exception as e:
        import traceback
        update_task_session(session_id, status="failed", error=traceback.format_exc())
        print(f"无结果词治理抽取任务崩溃: {e}")
        return {"status": "failed", "error": str(e), **summary}

def no_result_governance_process_job():
    """推进无结果词治理主循环。"""
    reload_config()
    session_id = create_task_session("no_result_governance_process_job")
    try:
        update_task_session(session_id, phase="Processing_No_Result_Governance")
        try:
            cycle_limit = int(GLOBAL_BUSINESS_CONFIG.get("no_result_governance_cycle_limit") or 5)
        except (TypeError, ValueError):
            cycle_limit = 5
        result = NoResultGovernanceService().run_governance_cycle(limit=max(1, cycle_limit))
        update_task_session(session_id, phase="Finished", status="success", summary=result)
        return result
    except Exception as e:
        import traceback
        update_task_session(session_id, status="failed", error=traceback.format_exc())
        print(f"无结果词治理处理任务崩溃: {e}")
        return {"status": "failed", "error": str(e)}

def inventory_monitor_job(brand_filter: str | None = None):
    """唯一的独立断码周期入口。"""
    reload_config()
    from src.query_governance.inventory_monitor import run_inventory_monitor_cycle

    return run_inventory_monitor_cycle(brand_filter)


def sync_agent_loop_inventory_monitor_jobs(scheduler, job_wrapper=None) -> None:
    """兼容旧调用方；新 Scheduler 使用 ``sync_inventory_monitor_jobs``。"""
    return sync_inventory_monitor_jobs(scheduler, job_wrapper)

def service_health_check_job():
    reload_config()
    session_id = create_task_session("service_health_check_job")
    summary = {
        "checks": {},
        "issues": [],
    }
    try:
        print(f"[{datetime.datetime.now()}] 启动服务健康检查任务...")
        stale_minutes = int(GLOBAL_SAFETY_CONFIG.get("health_check_stale_minutes", 90))

        update_task_session(session_id, phase="API_Probe")
        api_result = _run_probe_with_retry("api_probe", _probe_api_health, retries=3)
        summary["checks"]["api_probe"] = api_result
        api_detail = api_result.get("detail") if isinstance(api_result, dict) else None
        runtime = api_detail.get("runtime") if isinstance(api_detail, dict) else None
        if isinstance(runtime, dict):
            limiter = runtime.get("limiter") if isinstance(runtime.get("limiter"), dict) else {}
            circuit = runtime.get("circuit") if isinstance(runtime.get("circuit"), dict) else {}
            summary["businessHttp"] = {
                "activeLeases": limiter.get("activeLeases", limiter.get("inFlight")),
                "rateLimitedResponses": limiter.get("rateLimitedResponses"),
                "waitSeconds": limiter.get("waitSeconds"),
                "rateLimitRetryCount": limiter.get("rateLimitRetryCount"),
                "structuredFailureCount": limiter.get("structuredFailureCount"),
                "openCircuitCount": circuit.get("openCircuitCount"),
                "openCircuitIds": circuit.get("openCircuitIds") or [],
                "lastRecoveryAt": circuit.get("lastRecoveryAt"),
            }

        update_task_session(session_id, phase="DB_Freshness_Check")
        db_result = _probe_db_freshness(stale_minutes)
        summary["checks"]["db_freshness_check"] = db_result

        update_task_session(session_id, phase="External_Readonly_Probe")
        external_result = _probe_external_readonly()
        summary["checks"]["external_readonly_probe"] = external_result

        update_task_session(session_id, phase="Performance_Check")
        performance_result = _probe_performance()
        summary["checks"]["performance_check"] = performance_result

        failed_checks = [
            name for name, result in summary["checks"].items()
            if not result.get("ok")
        ]
        summary["issues"] = failed_checks
        status = "success" if not failed_checks else "failed"

        if failed_checks:
            from src.runtime.notifier import send_feishu_notification
            send_feishu_notification(
                json.dumps(summary, ensure_ascii=False, indent=2),
                "自动化运维系统健康检查告警"
            )

        update_task_session(session_id, phase="Finished", status=status, summary=summary)
        print(f"[{datetime.datetime.now()}] 服务健康检查任务完成，状态: {status}")
    except Exception as e:
        import traceback
        update_task_session(session_id, status="failed", error=traceback.format_exc(), summary=summary)
        print(f"服务健康检查任务崩溃: {e}")

def operation_log_retention_job() -> int:
    started_at = time.monotonic()
    try:
        deleted = purge_expired_operation_logs()
        print(json.dumps({
            "event": "operation_log_retention",
            "status": "success",
            "deletedRows": deleted,
            "durationMs": int(round((time.monotonic() - started_at) * 1000)),
        }, ensure_ascii=False, sort_keys=True), flush=True)
        logging.info("operation log retention removed %s rows", deleted)
        return deleted
    except OperationLogRetentionIndexMissing as error:
        logging.error("operation log retention blocked: %s", error)
        return 0
    except Exception:
        logging.exception("operation log retention failed")
        return 0


def start_scheduler(
    singleton_name: str | None = None,
    on_started: Callable[[], None] | None = None,
    release: str = "dev",
    config_fingerprint: str | None = None,
    owner_id: str | None = None,
    query_governance_workflow_factory: Callable[[str], Any] | None = None,
    leader_wait_seconds: float = 0,
) -> bool:
    if isinstance(leader_wait_seconds, bool) or not isinstance(leader_wait_seconds, (int, float)) or leader_wait_seconds < 0:
        raise ValueError("leader_wait_seconds must be nonnegative")
    mode = scheduler_mode()
    write_scheduler_status(ready=False, mode=mode, state="starting")
    if mode == "standby":
        return _run_scheduler_standby(on_started=on_started)
    if singleton_name:
        try:
            acquire_process_lock(singleton_name, metadata={"entrypoint": "start_scheduler"})
        except ProcessLockError:
            logging.error("scheduler_startup_rejected reason=process_lock_busy")
            write_scheduler_status(ready=False, mode=mode, state="rejected")
            return False

    leader_owner_id = owner_id or _new_scheduler_owner_id()
    fingerprint = config_fingerprint or _scheduler_config_fingerprint()
    db = SessionLocal()
    lease = None
    try:
        wait_deadline = time.monotonic() + leader_wait_seconds
        while True:
            try:
                lease = acquire_scheduler_leader_lease(
                    db, leader_name=SCHEDULER_GLOBAL_LEADER_NAME,
                    owner_id=leader_owner_id, lease_seconds=SCHEDULER_LEASE_SECONDS)
                break
            except SchedulerLeaderLeaseUnavailable:
                db.rollback()
                remaining = wait_deadline - time.monotonic()
                if remaining <= 0:
                    raise
                logging.warning("scheduler_leader_wait reason=leader_lease_busy owner=%s", leader_owner_id)
                write_scheduler_status(ready=False, mode=mode, state="starting")
                time.sleep(min(2.0, remaining))
        recovered_runs = 0
        while True:
            recovered_batch = recover_stale_scheduler_job_runs(
                db,
                leader_name=SCHEDULER_GLOBAL_LEADER_NAME,
                owner_id=leader_owner_id,
                fencing_token=lease.fencing_token,
            )
            recovered_runs += recovered_batch
            if recovered_batch < 100:
                break
        if recovered_runs:
            logging.warning(
                "scheduler recovered stale running facts as execution_unknown: %s",
                recovered_runs,
            )
    except SchedulerLeaderLeaseUnavailable:
        logging.error("scheduler_startup_rejected reason=leader_lease_busy owner=%s", leader_owner_id)
        write_scheduler_status(ready=False, mode=mode, state="rejected")
        return False
    except BaseException:
        if lease is not None:
            try:
                release_scheduler_leader_lease(
                    db,
                    leader_name=SCHEDULER_GLOBAL_LEADER_NAME,
                    owner_id=leader_owner_id,
                    fencing_token=lease.fencing_token,
                )
            except BaseException:
                logging.exception(
                    "scheduler leader lease release failed after stale-run recovery failure"
                )
        raise
    finally:
        db.close()

    lease_released = False
    active_run_tracker = ActiveRunTracker()
    leader_heartbeat = SchedulerLeaseHeartbeat(
        leader_name=SCHEDULER_GLOBAL_LEADER_NAME,
        owner_id=leader_owner_id,
        fencing_token=lease.fencing_token,
        lease_seconds=SCHEDULER_LEASE_SECONDS,
    )

    def release_leader_lease() -> None:
        nonlocal lease_released
        if lease_released:
            return
        lease_released = True
        heartbeat_stop_error = None
        try:
            leader_heartbeat.stop()
        except BaseException as exc:
            heartbeat_stop_error = exc
        release_db = SessionLocal()
        try:
            release_scheduler_leader_lease(
                release_db,
                leader_name=SCHEDULER_GLOBAL_LEADER_NAME,
                owner_id=leader_owner_id,
                fencing_token=lease.fencing_token,
            )
        except SchedulerLeaderFenceError:
            logging.warning("scheduler leader lease was already lost before shutdown")
        finally:
            release_db.close()
        if heartbeat_stop_error is not None:
            raise heartbeat_stop_error

    def release_leader_lease_after_failure() -> None:
        try:
            release_leader_lease()
        except BaseException:
            logging.exception(
                "scheduler leader lease release failed during scheduler failure"
            )

    try:
        leader_heartbeat.start()
    except BaseException:
        release_leader_lease_after_failure()
        raise

    try:
        scheduler = BlockingScheduler()
        run_executor = SchedulerRunExecutor(
            leader_name=SCHEDULER_GLOBAL_LEADER_NAME,
            owner_id=leader_owner_id,
            fencing_token=lease.fencing_token,
            release=release,
            config_fingerprint=fingerprint,
            lease_seconds=SCHEDULER_LEASE_SECONDS,
            leader_heartbeat=leader_heartbeat,
            active_run_tracker=active_run_tracker,
        )

        def stop_accepting_and_scheduler() -> None:
            write_scheduler_status(ready=False, mode=mode, state="stopped")
            active_run_tracker.stop_accepting()
            try:
                scheduler.pause()
            except SchedulerNotRunningError:
                pass
            try:
                scheduler.shutdown(wait=False)
            except SchedulerNotRunningError:
                pass

        termination = SchedulerTerminationController(
            stop_accepting=stop_accepting_and_scheduler,
            wait_for_drain=active_run_tracker.wait_for_drain,
            release_leader=release_leader_lease,
            timeout_seconds=SCHEDULER_TERMINATION_DRAIN_SECONDS,
        )
        scheduler_ready = threading.Event()

        def refresh_health() -> None:
            if scheduler_ready.is_set() and not termination.shutdown_requested:
                leader_heartbeat.assert_owned()
                write_scheduler_status(ready=True, mode=mode, state="ready")

        def handle_fence_loss(_error: BaseException) -> None:
            scheduler_ready.clear()
            active_run_tracker.stop_accepting()
            write_scheduler_status(ready=False, mode=mode, state="failed")
            termination.request_shutdown("leader_heartbeat_lost")
            # 排空在线程外执行，避免租约线程在 release 中 join 自身。
            threading.Thread(target=termination.drain_and_release,
                             name="scheduler-fence-shutdown", daemon=True).start()

        leader_heartbeat.on_lost = handle_fence_loss
        leader_heartbeat.on_renewed = refresh_health

        def fenced_workflow_factory(brand: str) -> Any:
            workflow = query_governance_workflow_factory(brand)
            def assert_live_fence() -> None:
                leader_heartbeat.assert_owned()
                fence_db = SessionLocal()
                try:
                    require_scheduler_leader_fence(fence_db,
                        leader_name=SCHEDULER_GLOBAL_LEADER_NAME,
                        owner_id=leader_owner_id, fencing_token=lease.fencing_token)
                finally:
                    fence_db.close()
            workflow.assert_scheduler_fence = assert_live_fence
            workflow.should_continue = lambda: not termination.shutdown_requested
            return workflow

        governance_factory = (fenced_workflow_factory if query_governance_workflow_factory is not None else None)
        def add_fenced_listener(listener: Callable[..., Any], event_mask: int) -> None:
            try:
                scheduler.add_listener(listener, event_mask)
            except BaseException:
                release_leader_lease_after_failure()
                raise

        add_fenced_listener(run_executor.record_scheduled_runs, EVENT_JOB_SUBMITTED)

        def add_fenced_job(*args, **kwargs) -> None:
            try:
                scheduler.add_job(*args, **kwargs)
            except BaseException:
                release_leader_lease_after_failure()
                raise

        def fenced(job_id: str, job_func: Callable[..., Any]) -> Callable[..., Any]:
            return run_executor.bind(job_id, job_func)

        started_notified = False

        def notify_started(_event) -> None:
            nonlocal started_notified
            if started_notified:
                return
            started_notified = True
            leader_heartbeat.assert_owned()
            scheduler_ready.set()
            refresh_health()
            if on_started is not None:
                on_started()
        add_fenced_listener(notify_started, EVENT_SCHEDULER_STARTED)
        # 增加 misfire_grace_time 防止因任务执行过长导致后续任务被跳过
        # 允许 1 小时的容错窗口
        from src.config import BRAND_CONFIGS, is_brand_enabled
        if GLOBAL_BUSINESS_CONFIG.get("no_result_agent_enabled") is True:
            add_fenced_job(
                fenced("no_result_governance_extract_job", no_result_governance_extract_job),
                'cron',
                hour=1,
                minute=30,
                misfire_grace_time=3600,
                id="no_result_governance_extract_job",
                replace_existing=True,
            )
            add_fenced_job(
                fenced("no_result_governance_process_job", no_result_governance_process_job),
                'interval',
                minutes=int(GLOBAL_BUSINESS_CONFIG.get("no_result_governance_interval_minutes", 15)),
                misfire_grace_time=600,
                id="no_result_governance_process_job",
                replace_existing=True,
            )
        add_fenced_job(
            fenced("query_demotion_verification_job", query_demotion_verification_job),
            'interval',
            minutes=1,
            misfire_grace_time=60,
            id='query_demotion_verification_job',
            replace_existing=True,
        )
        add_fenced_job(
            fenced("service_health_check_job", service_health_check_job),
            'interval',
            minutes=int(GLOBAL_BUSINESS_CONFIG.get("health_check_interval_minutes", 60)),
            misfire_grace_time=600,
            id="service_health_check_job",
            replace_existing=True,
        )
        add_fenced_job(
            fenced("operation_log_retention_job", operation_log_retention_job),
            'interval',
            minutes=10,
            misfire_grace_time=600,
            id='operation_log_retention_job',
            replace_existing=True,
        )
        add_fenced_job(
            fenced("service_health_check_startup", service_health_check_job),
            'date',
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=10),
            misfire_grace_time=300,
            id="service_health_check_startup",
            replace_existing=True,
        )
        enabled_inventory_brands = [
            brand for brand in BRAND_CONFIGS.keys()
            if is_brand_enabled(brand)
        ]
        inventory_start_dates = _inventory_monitor_start_dates(enabled_inventory_brands)
        for brand in enabled_inventory_brands:
            business_config = get_brand_business_config(brand)
            inventory_check_interval_minutes = business_config.get("top_query_inventory_check_interval_minutes", 60)
            add_fenced_job(
                fenced(_inventory_monitor_job_id(brand), inventory_monitor_job),
                'interval',
                minutes=inventory_check_interval_minutes,
                misfire_grace_time=600,
                coalesce=True,
                max_instances=1,
                kwargs={"brand_filter": brand},
                id=_inventory_monitor_job_id(brand),
                replace_existing=True,
                start_date=inventory_start_dates[brand],
            )
        sync_query_governance_experiment_job(
            scheduler,
            workflow_factory=governance_factory,
            job_wrapper=fenced,
        )
        sync_query_governance_remediation_job(
            scheduler,
            workflow_factory=governance_factory,
            job_wrapper=fenced,
        )
        sync_query_governance_recovery_job(
            scheduler,
            workflow_factory=governance_factory,
            job_wrapper=fenced,
        )
        sync_query_governance_summary_reconciliation_job(
            scheduler,
            job_wrapper=fenced,
        )
        sync_query_governance_image_integrity_job(
            scheduler,
            job_wrapper=fenced,
        )
        add_fenced_job(
            fenced("inventory_monitor_config_sync", sync_inventory_monitor_jobs),
            'interval',
            minutes=1,
            misfire_grace_time=60,
            kwargs={"scheduler": scheduler, "job_wrapper": fenced},
            id='inventory_monitor_config_sync',
            replace_existing=True,
        )
        planning_db = SessionLocal()
        try:
            compensation_summary = plan_and_persist_missed_scheduler_runs(
                planning_db,
                jobs=scheduler.get_jobs(),
                observed_at=datetime.datetime.now(datetime.timezone.utc),
                compensation_window=SCHEDULER_COMPENSATION_WINDOW,
                max_compensations=SCHEDULER_MAX_COMPENSATIONS,
                release=release,
                config_fingerprint=fingerprint,
            )
            logging.info("scheduler missed-run plans persisted: %s", compensation_summary)
        finally:
            planning_db.close()
        restore_signal_handlers = install_termination_signal_handlers(
            controller=termination,
        )
        try:
            try:
                leader_heartbeat.assert_owned()
                scheduler.start()
            except (KeyboardInterrupt, SystemExit):
                termination.request_shutdown("scheduler_interrupt")
                if not termination.drain_and_release():
                    raise RuntimeError("scheduler termination drain timed out")
                print(f"[{datetime.datetime.now()}] 调度器已停止。")
            except Exception as exc:
                logging.error("scheduler_start_failed error_type=%s", type(exc).__name__)
                raise RuntimeError("scheduler start failed") from exc
            if not termination.shutdown_requested:
                termination.request_shutdown("scheduler_stopped")
            if not termination.drain_and_release():
                raise RuntimeError("scheduler termination drain timed out")
        finally:
            restore_signal_handlers()
        return True
    except BaseException:
        write_scheduler_status(ready=False, mode=mode, state="failed")
        release_leader_lease_after_failure()
        raise
    finally:
        # Keep the heartbeat bounded even when a test/runtime termination
        # controller returns without invoking its release callback.
        if not lease_released:
            release_leader_lease_after_failure()

if __name__ == "__main__":
    raise SystemExit(LegacyAgentLoopRetiredError())
