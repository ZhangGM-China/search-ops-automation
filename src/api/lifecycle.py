"""Application lifecycle and health-probe support."""

from __future__ import annotations

import asyncio
import os
import math
import threading
import time
from fastapi import FastAPI
from typing import Dict, Any
from contextlib import asynccontextmanager
from src.persistence import check_database_connection, get_latest_task_session, engine, init_db
from src.migrations.release_schema import validate_release_schema_verification, verify_release_schema
from src.config import GLOBAL_SAFETY_CONFIG, get_env

_HEALTH_DB_PROBE_LOCK = threading.Lock()
_DEFAULT_HEALTH_DB_TIMEOUT_SECONDS = 3.0
_DEFAULT_STARTUP_PROBE_TIMEOUT_SECONDS = 60.0
_MAX_STARTUP_PROBE_TIMEOUT_SECONDS = 600.0


def _health_db_timeout_seconds() -> float:
    try:
        configured = float(os.getenv(
            "HEALTH_CHECK_DB_TIMEOUT_SECONDS",
            str(_DEFAULT_HEALTH_DB_TIMEOUT_SECONDS),
        ))
    except (TypeError, ValueError):
        configured = _DEFAULT_HEALTH_DB_TIMEOUT_SECONDS
    if not math.isfinite(configured) or configured <= 0:
        return _DEFAULT_HEALTH_DB_TIMEOUT_SECONDS
    return configured


def _collect_health_db_snapshot():
    if not _HEALTH_DB_PROBE_LOCK.acquire(blocking=False):
        return False, None, None, None
    try:
        db_ok = check_database_connection()
        if not db_ok:
            return False, None, None, None
        return (
            True,
            None,
            get_latest_task_session("service_health_check_job"),
            None,
        )
    finally:
        _HEALTH_DB_PROBE_LOCK.release()


async def _bounded_health_db_probe():
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_collect_health_db_snapshot),
            timeout=_health_db_timeout_seconds(),
        )
    except Exception:
        return False, None, None, None


def _startup_probe_timeout_seconds() -> float:
    try:
        configured = float(os.getenv(
            "STARTUP_PROBE_TIMEOUT_SECONDS",
            str(_DEFAULT_STARTUP_PROBE_TIMEOUT_SECONDS),
        ))
    except (TypeError, ValueError):
        configured = _DEFAULT_STARTUP_PROBE_TIMEOUT_SECONDS
    if not math.isfinite(configured) or configured <= 0:
        return _DEFAULT_STARTUP_PROBE_TIMEOUT_SECONDS
    return min(configured, _MAX_STARTUP_PROBE_TIMEOUT_SECONDS)


def _set_startup_probe_state(app_instance: FastAPI, status_value: str) -> None:
    app_instance.state.startup_probe_status = status_value
    if status_value == "starting":
        app_instance.state.startup_probe_started_at = time.monotonic()


def _startup_probe_snapshot(app_instance: FastAPI) -> Dict[str, Any]:
    timeout_seconds = _startup_probe_timeout_seconds()
    started_at = float(getattr(
        app_instance.state,
        "startup_probe_started_at",
        time.monotonic(),
    ))
    elapsed_seconds = max(0.0, time.monotonic() - started_at)
    status_value = str(getattr(
        app_instance.state,
        "startup_probe_status",
        "starting",
    ))
    if status_value == "starting" and elapsed_seconds > timeout_seconds:
        status_value = "timed_out"
    return {
        "ready": status_value == "ready",
        "status": status_value,
        "elapsedSeconds": round(elapsed_seconds, 3),
        "timeoutSeconds": timeout_seconds,
    }


def _build_release_readiness(db_ok: bool, runtime_status: Any) -> Dict[str, Any]:
    runtime_available = (
        isinstance(runtime_status, dict)
        and runtime_status.get("available") is True
    )
    runtime_circuit_closed = (
        isinstance(runtime_status, dict)
        and runtime_status.get("is_circuit_broken") is False
    )
    return {
        "ready": db_ok and runtime_available and runtime_circuit_closed,
        "components": {
            "database": {"connected": db_ok},
            "runtime": {
                "available": runtime_available,
                "circuitClosed": runtime_circuit_closed,
            },
        },
    }


class _StartupInitializationTimedOut(RuntimeError):
    pass


def _initialize_api_runtime(environment: str) -> None:
    if environment != "prod":
        init_db()
        return
    validate_release_schema_verification(verify_release_schema(engine))


async def _run_bounded_startup_initialization(environment: str) -> None:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_initialize_api_runtime, environment),
            timeout=_startup_probe_timeout_seconds(),
        )
    except asyncio.TimeoutError:
        raise _StartupInitializationTimedOut from None


@asynccontextmanager
async def api_lifespan(app_instance: FastAPI):
    _set_startup_probe_state(app_instance, "starting")
    environment = None
    try:
        environment = get_env()
        await _run_bounded_startup_initialization(environment)
    except _StartupInitializationTimedOut:
        _set_startup_probe_state(app_instance, "timed_out")
        raise RuntimeError("application startup initialization timed out") from None
    except Exception:
        _set_startup_probe_state(app_instance, "failed")
        if environment == "prod":
            raise RuntimeError("production release schema verification failed") from None
        raise
    _set_startup_probe_state(app_instance, "ready")
    yield

__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
