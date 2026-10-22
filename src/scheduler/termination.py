"""Deterministic shutdown ordering for the production scheduler."""

from __future__ import annotations

import threading
import signal
import time
from collections.abc import Callable
from typing import Any


class SchedulerTerminationController:
    """Stop new work, drain submitted work, then release the leader fence."""

    def __init__(
        self,
        *,
        stop_accepting: Callable[[], None],
        wait_for_drain: Callable[[float], bool],
        release_leader: Callable[[], None],
        timeout_seconds: float,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("scheduler termination timeout must be positive")
        self._stop_accepting = stop_accepting
        self._wait_for_drain = wait_for_drain
        self._release_leader = release_leader
        self._timeout_seconds = float(timeout_seconds)
        self._shutdown_event = threading.Event()
        self._lock = threading.Lock()
        self._drain_result: bool | None = None

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    def request_shutdown(self, _reason: str) -> None:
        self._shutdown_event.set()

    def drain_and_release(self) -> bool:
        """Run one idempotent graceful shutdown transaction."""
        with self._lock:
            if self._drain_result is not None:
                return self._drain_result
            self._stop_accepting()
            drained = self._wait_for_drain(self._timeout_seconds)
            if drained is not True:
                self._drain_result = False
                return False
            self._release_leader()
            self._drain_result = True
            return True


def install_termination_signal_handlers(
    *,
    controller: SchedulerTerminationController,
    register: Callable[[int, Any], Any] = signal.signal,
) -> Callable[[], None]:
    """Install SIGTERM/SIGINT handlers and return an idempotent restorer."""
    previous: dict[int, Any] = {}

    def handle(signum: int, _frame: Any) -> None:
        controller.request_shutdown(signal.Signals(signum).name)
        controller.drain_and_release()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = register(signum, handle)
    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        for signum in (signal.SIGTERM, signal.SIGINT):
            register(signum, previous[signum])

    return restore


class ActiveRunTracker:
    """Count active fenced runs without exposing job payloads or identifiers."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = 0
        self._accepting = True

    def stop_accepting(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def enter(self) -> bool:
        with self._condition:
            if not self._accepting:
                return False
            self._active += 1
            return True

    def exit(self) -> None:
        with self._condition:
            if self._active <= 0:
                raise RuntimeError("scheduler active run tracker is unbalanced")
            self._active -= 1
            if self._active == 0:
                self._condition.notify_all()

    def wait_for_drain(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True
