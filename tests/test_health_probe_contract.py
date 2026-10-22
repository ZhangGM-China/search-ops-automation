import asyncio
import threading
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest

from importlib import import_module
api_module = import_module("src.api.app")
health_module = import_module("src.api.routers.health")
lifecycle_module = import_module("src.api.lifecycle")


def _set_startup_state(*, status: str, started_at: float) -> None:
    api_module.app.state.startup_probe_status = status
    api_module.app.state.startup_probe_started_at = started_at


def _start_lifespan_failure_observer(
    *,
    failed: threading.Event,
    errors: list[BaseException],
) -> threading.Thread:
    async def exercise_lifespan() -> None:
        try:
            async with api_module.api_lifespan(api_module.app):
                pass
        except BaseException as exc:
            errors.append(exc)
            failed.set()

    thread = threading.Thread(
        target=lambda: asyncio.run(exercise_lifespan()),
        daemon=True,
    )
    thread.start()
    return thread


def test_liveness_only_proves_process_is_serving() -> None:
    _set_startup_state(status="failed", started_at=0.0)

    with patch.object(
        health_module,
        "_bounded_health_db_probe",
        AsyncMock(side_effect=AssertionError("liveness must not access the database")),
    ), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        side_effect=AssertionError("liveness must not access runtime dependencies"),
    ):
        response = TestClient(api_module.app).get("/api/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"
    assert response.json()["live"] is True


def test_startup_fails_closed_during_bounded_initialization_window(monkeypatch) -> None:
    monkeypatch.setenv("STARTUP_PROBE_TIMEOUT_SECONDS", "30")
    _set_startup_state(status="starting", started_at=100.0)

    with patch.object(lifecycle_module.time, "monotonic", return_value=110.0):
        response = TestClient(api_module.app).get("/api/health/startup")

    assert response.status_code == 503
    assert response.json()["status"] == "starting"
    assert response.json()["ready"] is False
    assert response.json()["timeoutSeconds"] == 30.0


def test_startup_reports_timeout_without_consulting_external_dependencies(monkeypatch) -> None:
    monkeypatch.setenv("STARTUP_PROBE_TIMEOUT_SECONDS", "30")
    _set_startup_state(status="starting", started_at=100.0)

    with patch.object(lifecycle_module.time, "monotonic", return_value=131.0), patch.object(
        health_module,
        "_bounded_health_db_probe",
        AsyncMock(side_effect=AssertionError("startup must not access the database")),
    ), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        side_effect=AssertionError("startup must not access runtime dependencies"),
    ):
        response = TestClient(api_module.app).get("/api/health/startup")

    assert response.status_code == 503
    assert response.json()["status"] == "timed_out"
    assert response.json()["ready"] is False


def test_startup_tracks_lifespan_before_and_after_initialization() -> None:
    _set_startup_state(status="starting", started_at=0.0)

    before = TestClient(api_module.app).get("/api/health/startup")
    with patch.object(lifecycle_module, "get_env", return_value="test"), patch.object(
        lifecycle_module, "init_db"
    ) as init_db:
        with TestClient(api_module.app) as client:
            after = client.get("/api/health/startup")

    assert before.status_code == 503
    assert before.json()["ready"] is False
    assert after.status_code == 200
    assert after.json()["status"] == "ready"
    init_db.assert_called_once_with()


def test_startup_fails_closed_when_lifespan_initialization_raises() -> None:
    _set_startup_state(status="starting", started_at=0.0)

    with patch.object(lifecycle_module, "get_env", return_value="test"), patch.object(
        lifecycle_module, "init_db", side_effect=RuntimeError("database unavailable")
    ):
        with pytest.raises(RuntimeError, match="database unavailable"):
            with TestClient(api_module.app):
                pass

    response = TestClient(api_module.app).get("/api/health/startup")
    assert response.status_code == 503
    assert response.json()["status"] == "failed"
    assert response.json()["ready"] is False


def test_lifespan_times_out_blocked_test_initialization_before_worker_finishes(
    monkeypatch,
) -> None:
    monkeypatch.setenv("STARTUP_PROBE_TIMEOUT_SECONDS", "0.02")
    worker_started = threading.Event()
    release_worker = threading.Event()
    lifespan_failed = threading.Event()
    errors: list[BaseException] = []

    def blocked_init_db() -> None:
        worker_started.set()
        release_worker.wait(timeout=2)

    with patch.object(lifecycle_module, "get_env", return_value="test"), patch.object(
        lifecycle_module, "init_db", side_effect=blocked_init_db
    ):
        thread = _start_lifespan_failure_observer(
            failed=lifespan_failed,
            errors=errors,
        )
        assert worker_started.wait(timeout=1)
        try:
            assert lifespan_failed.wait(timeout=0.5)
            assert release_worker.is_set() is False
            assert api_module.app.state.startup_probe_status == "timed_out"
            assert len(errors) == 1
            assert str(errors[0]) == "application startup initialization timed out"
        finally:
            release_worker.set()
            thread.join(timeout=1)

    assert thread.is_alive() is False


def test_lifespan_applies_same_timeout_to_production_schema_verification(
    monkeypatch,
) -> None:
    monkeypatch.setenv("STARTUP_PROBE_TIMEOUT_SECONDS", "0.02")
    worker_started = threading.Event()
    release_worker = threading.Event()
    lifespan_failed = threading.Event()
    errors: list[BaseException] = []

    def blocked_schema_verification(_engine):
        worker_started.set()
        release_worker.wait(timeout=2)
        return {"schemaRelease": "must-not-leak"}

    with patch.object(lifecycle_module, "get_env", return_value="prod"), patch.object(
        lifecycle_module,
        "verify_release_schema",
        side_effect=blocked_schema_verification,
    ), patch.object(lifecycle_module, "validate_release_schema_verification"):
        thread = _start_lifespan_failure_observer(
            failed=lifespan_failed,
            errors=errors,
        )
        assert worker_started.wait(timeout=1)
        try:
            assert lifespan_failed.wait(timeout=0.5)
            assert release_worker.is_set() is False
            assert api_module.app.state.startup_probe_status == "timed_out"
            assert len(errors) == 1
            assert str(errors[0]) == "application startup initialization timed out"
            assert "must-not-leak" not in str(errors[0])
        finally:
            release_worker.set()
            thread.join(timeout=1)

    assert thread.is_alive() is False


def test_startup_remains_ready_when_runtime_dependencies_raise() -> None:
    _set_startup_state(status="ready", started_at=0.0)
    db_probe = AsyncMock(side_effect=AssertionError("startup must not access the database"))

    with patch.object(health_module, "_bounded_health_db_probe", db_probe), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        side_effect=AssertionError("startup must not access runtime dependencies"),
    ) as runtime_probe:
        response = TestClient(api_module.app).get("/api/health/startup")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["ready"] is True
    db_probe.assert_not_awaited()
    runtime_probe.assert_not_called()


def test_readiness_reuses_release_readiness_and_returns_503_for_database_failure() -> None:
    _set_startup_state(status="ready", started_at=0.0)
    runtime = {"available": True, "is_circuit_broken": False}

    with patch.object(
        health_module,
        "_bounded_health_db_probe",
        AsyncMock(return_value=(False, None, None, None)),
    ), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        return_value=runtime,
    ):
        response = TestClient(api_module.app).get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.json()["releaseReadiness"] == {
        "ready": False,
        "components": {
            "database": {"connected": False},
            "runtime": {"available": True, "circuitClosed": True},
        },
    }


def test_readiness_returns_503_for_transient_runtime_failure_without_affecting_liveness() -> None:
    _set_startup_state(status="ready", started_at=0.0)
    runtime = {"available": False, "is_circuit_broken": False}

    with patch.object(
        health_module,
        "_bounded_health_db_probe",
        AsyncMock(return_value=(True, None, None, None)),
    ), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        return_value=runtime,
    ):
        ready_response = TestClient(api_module.app).get("/api/health/ready")
        live_response = TestClient(api_module.app).get("/api/health/live")

    assert ready_response.status_code == 503
    assert ready_response.json()["ready"] is False
    assert live_response.status_code == 200
    assert live_response.json()["live"] is True


def test_readiness_fails_closed_when_runtime_probe_raises() -> None:
    _set_startup_state(status="ready", started_at=0.0)

    with patch.object(
        health_module,
        "_bounded_health_db_probe",
        AsyncMock(return_value=(True, None, None, None)),
    ), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        side_effect=RuntimeError("runtime probe failed"),
    ):
        response = TestClient(api_module.app).get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.json()["releaseReadiness"]["components"]["runtime"] == {
        "available": False,
        "circuitClosed": False,
    }


def test_readiness_returns_200_after_startup_and_required_dependencies_are_ready() -> None:
    _set_startup_state(status="ready", started_at=0.0)
    runtime = {"available": True, "is_circuit_broken": False}

    with patch.object(
        health_module,
        "_bounded_health_db_probe",
        AsyncMock(return_value=(True, None, None, None)),
    ), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        return_value=runtime,
    ):
        response = TestClient(api_module.app).get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["startup"]["status"] == "ready"
    assert response.json()["releaseReadiness"]["ready"] is True


@pytest.mark.parametrize("startup_status", ["starting", "failed"])
def test_readiness_requires_completed_startup_even_when_dependencies_are_ready(
    startup_status: str,
) -> None:
    _set_startup_state(status=startup_status, started_at=lifecycle_module.time.monotonic())
    runtime = {"available": True, "is_circuit_broken": False}

    with patch.object(
        health_module,
        "_bounded_health_db_probe",
        AsyncMock(return_value=(True, None, None, None)),
    ), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        return_value=runtime,
    ):
        response = TestClient(api_module.app).get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert response.json()["startup"]["status"] == startup_status
    assert response.json()["releaseReadiness"]["ready"] is True


def test_legacy_health_keeps_http_200_when_release_readiness_is_false() -> None:
    _set_startup_state(status="ready", started_at=0.0)
    runtime = {"available": False, "is_circuit_broken": True}

    with patch.object(
        health_module,
        "_bounded_health_db_probe",
        AsyncMock(return_value=(False, None, None, None)),
    ), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        return_value=runtime,
    ):
        response = TestClient(api_module.app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["healthy"] is False
    assert response.json()["releaseReadiness"]["ready"] is False


def test_readiness_probe_succeeds_under_active_business_http_lock(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BUSINESS_HTTP_STATE_DIR", str(tmp_path))
    _set_startup_state(status="ready", started_at=0.0)

    from src.integrations.business_http import BusinessRequestGovernor
    governor = BusinessRequestGovernor(state_dir=tmp_path)
    governor.record_structured_failure()

    lock_file = (tmp_path / "business_http.lock").open("a+")
    import fcntl
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    try:
        with patch.object(
            health_module,
            "_bounded_health_db_probe",
            AsyncMock(return_value=(True, None, None, None)),
        ):
            response = TestClient(api_module.app).get("/api/health/ready")

        assert response.status_code == 200
        assert response.json()["ready"] is True
        assert response.json()["releaseReadiness"]["ready"] is True
        assert response.json()["releaseReadiness"]["components"]["runtime"]["available"] is True
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def test_readiness_runs_runtime_status_in_worker_thread() -> None:
    _set_startup_state(status="ready", started_at=0.0)
    called_threads = []
    main_thread = threading.current_thread()

    def mock_get_runtime_status():
        called_threads.append(threading.current_thread())
        return {"available": True, "is_circuit_broken": False}

    with patch.object(
        health_module,
        "_bounded_health_db_probe",
        AsyncMock(return_value=(True, None, None, None)),
    ), patch.object(
        health_module.OperationalService,
        "get_runtime_status",
        side_effect=mock_get_runtime_status,
    ):
        response = TestClient(api_module.app).get("/api/health/ready")

    assert response.status_code == 200
    assert len(called_threads) == 1
    assert called_threads[0] != main_thread


