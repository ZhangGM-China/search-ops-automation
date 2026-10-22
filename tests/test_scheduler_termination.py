import importlib.util
import signal
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "scheduler" / "termination.py"


def _load():
    spec = importlib.util.spec_from_file_location("test_scheduler_termination", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_termination_controller_stops_accepting_then_waits_for_drain_and_releases_once():
    termination = _load()
    events = []
    controller = termination.SchedulerTerminationController(
        stop_accepting=lambda: events.append("stop_accepting"),
        wait_for_drain=lambda timeout: events.append(("wait_for_drain", timeout)) or True,
        release_leader=lambda: events.append("release_leader"),
        timeout_seconds=120,
    )

    controller.request_shutdown("SIGTERM")
    controller.request_shutdown("SIGTERM")
    completed = controller.drain_and_release()
    completed_again = controller.drain_and_release()

    assert completed is True
    assert completed_again is True
    assert events == ["stop_accepting", ("wait_for_drain", 120), "release_leader"]
    assert controller.shutdown_requested is True


def test_termination_controller_never_releases_leader_before_drain_and_returns_failure():
    termination = _load()
    events = []
    controller = termination.SchedulerTerminationController(
        stop_accepting=lambda: events.append("stop_accepting"),
        wait_for_drain=lambda timeout: events.append(("wait_for_drain", timeout)) or False,
        release_leader=lambda: events.append("release_leader"),
        timeout_seconds=120,
    )

    controller.request_shutdown("SIGTERM")

    assert controller.drain_and_release() is False
    assert events == ["stop_accepting", ("wait_for_drain", 120)]


def test_termination_controller_rejects_invalid_timeout_and_propagates_release_failure():
    termination = _load()
    with pytest.raises(ValueError, match="timeout"):
        termination.SchedulerTerminationController(
            stop_accepting=lambda: None,
            wait_for_drain=lambda timeout: True,
            release_leader=lambda: None,
            timeout_seconds=0,
        )

    controller = termination.SchedulerTerminationController(
        stop_accepting=lambda: None,
        wait_for_drain=lambda timeout: True,
        release_leader=lambda: (_ for _ in ()).throw(RuntimeError("db unavailable")),
        timeout_seconds=120,
    )
    controller.request_shutdown("SIGTERM")
    with pytest.raises(RuntimeError, match="db unavailable"):
        controller.drain_and_release()


def test_signal_handlers_request_one_ordered_shutdown_and_can_be_restored():
    termination = _load()
    registered = {}
    restored = []
    events = []
    controller = termination.SchedulerTerminationController(
        stop_accepting=lambda: events.append("stop_accepting"),
        wait_for_drain=lambda timeout: events.append(("wait_for_drain", timeout)) or True,
        release_leader=lambda: events.append("release_leader"),
        timeout_seconds=120,
    )

    def register(signum, handler):
        previous = f"previous-{signum}"
        if handler in {previous for previous in registered.values() if isinstance(previous, str)}:
            restored.append((signum, handler))
        registered[signum] = handler
        return previous

    restore = termination.install_termination_signal_handlers(
        controller=controller,
        register=register,
    )

    registered[signal.SIGTERM](signal.SIGTERM, None)
    assert controller.shutdown_requested is True
    assert events == ["stop_accepting", ("wait_for_drain", 120), "release_leader"]
    restore()
    assert registered[signal.SIGTERM] == f"previous-{signal.SIGTERM}"
    assert registered[signal.SIGINT] == f"previous-{signal.SIGINT}"


def test_active_run_tracker_rejects_new_work_after_stop_and_drains_active_work():
    termination = _load()
    tracker = termination.ActiveRunTracker()

    assert tracker.enter() is True
    tracker.stop_accepting()
    assert tracker.enter() is False
    assert tracker.wait_for_drain(0.001) is False
    tracker.exit()
    assert tracker.wait_for_drain(0.001) is True
