from __future__ import annotations

import threading
import logging
from typing import Callable, Optional

from src.persistence import (
    SessionLocal,
    SchedulerLeaderFenceError,
    heartbeat_scheduler_job_run,
    renew_scheduler_leader_lease,
)


class SchedulerLeaseHeartbeat:
    """Renew one leader fence and, when supplied, its active run fact."""

    def __init__(
        self,
        *,
        leader_name: str,
        owner_id: str,
        fencing_token: int,
        lease_seconds: int,
        session_factory: Callable = SessionLocal,
        run_id: Optional[int] = None,
        claim_owner: Optional[str] = None,
        claim_token: Optional[str] = None,
        interval_seconds: Optional[float] = None,
        on_lost: Optional[Callable[[BaseException], None]] = None,
        on_renewed: Optional[Callable[[], None]] = None,
    ) -> None:
        self.leader_name = leader_name
        self.owner_id = owner_id
        self.fencing_token = fencing_token
        self.lease_seconds = lease_seconds
        self.session_factory = session_factory
        self.run_id = run_id
        self.claim_owner = claim_owner
        self.claim_token = claim_token
        self.interval_seconds = (
            max(1.0, lease_seconds / 3)
            if interval_seconds is None
            else interval_seconds
        )
        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, (int, float))
            or self.interval_seconds <= 0
            or self.interval_seconds >= lease_seconds
        ):
            raise ValueError(
                "scheduler heartbeat interval must be positive and shorter than its lease"
            )
        if run_id is not None and (not claim_owner or not claim_token):
            raise ValueError("scheduler run heartbeat requires claim owner and token")
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lost_error: Optional[BaseException] = None
        self._state_lock = threading.Lock()
        self.on_lost = on_lost
        self.on_renewed = on_renewed

    def beat_once(self) -> None:
        db = self.session_factory()
        try:
            renew_scheduler_leader_lease(
                db,
                leader_name=self.leader_name,
                owner_id=self.owner_id,
                fencing_token=self.fencing_token,
                lease_seconds=self.lease_seconds,
            )
            if self.run_id is not None:
                heartbeat_scheduler_job_run(
                    db,
                    self.run_id,
                    claim_owner=self.claim_owner,
                    claim_token=self.claim_token,
                    leader_name=self.leader_name,
                    owner_id=self.owner_id,
                    fencing_token=self.fencing_token,
                )
        finally:
            db.close()

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.beat_once()
                if self.on_renewed is not None:
                    self.on_renewed()
            except BaseException as exc:
                with self._state_lock:
                    self._lost_error = exc
                self._stop_event.set()
                logging.error("scheduler_heartbeat_lost error_type=%s owner=%s token=%s",
                              type(exc).__name__, self.owner_id, self.fencing_token)
                if self.on_lost is not None:
                    try:
                        self.on_lost(exc)
                    except BaseException as callback_error:
                        logging.error("scheduler_heartbeat_shutdown_failed error_type=%s",
                                      type(callback_error).__name__)
                return

    def start(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._run,
            name="scheduler-lease-heartbeat",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 1))
            if self._thread.is_alive():
                error = SchedulerLeaderFenceError(
                    "scheduler heartbeat thread did not stop before timeout"
                )
                with self._state_lock:
                    self._lost_error = error
                raise error

    def assert_owned(self) -> None:
        with self._state_lock:
            lost_error = self._lost_error
        if lost_error is not None:
            raise SchedulerLeaderFenceError("scheduler heartbeat lost its live fence") from lost_error
