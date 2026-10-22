"""Read-only application health endpoints."""

from __future__ import annotations

import asyncio
import datetime
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.config import GLOBAL_SAFETY_CONFIG, get_env
from src.integrations.operational_service import OperationalService
from src.runtime.release_info import get_release_info

from ..lifecycle import (
    _bounded_health_db_probe,
    _build_release_readiness,
    _startup_probe_snapshot,
)


router = APIRouter()


@router.get("/api/health/live")
async def liveness_check():
    return {
        "live": True,
        "status": "alive",
    }


@router.get("/api/health/startup")
async def startup_check(request: Request):
    startup = _startup_probe_snapshot(request.app)
    return JSONResponse(
        status_code=200 if startup["ready"] else 503,
        content=startup,
    )


@router.get("/api/health/ready")
async def readiness_check(request: Request):
    db_ok, _, _, _ = await _bounded_health_db_probe()
    try:
        runtime_status = await asyncio.to_thread(OperationalService.get_runtime_status)
    except Exception:
        runtime_status = None
    startup = _startup_probe_snapshot(request.app)
    release_readiness = _build_release_readiness(db_ok, runtime_status)
    ready = startup["ready"] and release_readiness["ready"]
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            **get_release_info(),
            "ready": ready,
            "startup": startup,
            "releaseReadiness": release_readiness,
        },
    )


@router.get("/api/health")
async def health_check():
    db_ok, _, health_session, _ = await _bounded_health_db_probe()
    try:
        runtime_status = await asyncio.to_thread(OperationalService.get_runtime_status)
    except Exception:
        runtime_status = None
    stale_minutes = GLOBAL_SAFETY_CONFIG.get("health_check_stale_minutes", 90)
    task_failed = bool(
        health_session
        and health_session.status in {"failed", "partial_failed"}
    )
    health_session_stale = False
    if health_session and health_session.start_time:
        session_reference_time = health_session.end_time or health_session.start_time
        health_session_stale = (
            datetime.datetime.now(session_reference_time.tzinfo)
            - session_reference_time
        ) > datetime.timedelta(minutes=stale_minutes)

    experiment_quality_fields = (
        "expected", "observed", "missingKeys", "treatmentTaskCount",
        "controlTaskCount", "controlActionCount", "contaminationCount",
        "actionFailed", "actionInconclusive", "unresolved", "retryCount", "dataDate",
        "calculationVersion",
    )

    def serialize_session(session):
        if not session:
            return None
        payload = {
            "taskName": session.task_name,
            "status": session.status,
            "phase": session.current_phase,
            "startTime": session.start_time.isoformat() if session.start_time else None,
            "endTime": session.end_time.isoformat() if session.end_time else None,
        }
        try:
            summary = json.loads(session.summary_data or "{}")
        except (TypeError, json.JSONDecodeError):
            summary = {}
        brands = summary.get("brands") if isinstance(summary, dict) else None
        if isinstance(brands, dict):
            quality_by_brand = {}
            for brand, brand_summary in brands.items():
                quality = brand_summary.get("experimentQuality") if isinstance(brand_summary, dict) else None
                if isinstance(quality, dict):
                    quality_by_brand[str(brand)] = {
                        field: quality.get(field) for field in experiment_quality_fields
                    }
            if quality_by_brand:
                payload["experimentQuality"] = quality_by_brand
        return payload

    healthy = (
        db_ok
        and isinstance(runtime_status, dict)
        and runtime_status.get("available") is True
        and runtime_status.get("is_circuit_broken") is False
        and not task_failed
        and not health_session_stale
    )
    release_readiness = _build_release_readiness(db_ok, runtime_status)
    return {
        **get_release_info(),
        "healthy": healthy,
        "releaseReadiness": release_readiness,
        "env": get_env(),
        "database": {"connected": db_ok},
        "tasks": {
            "serviceHealthCheck": {
                **(serialize_session(health_session) or {}),
                "isStale": health_session_stale,
                "staleThresholdMinutes": stale_minutes,
            } if health_session else None,
        },
        "runtime": runtime_status,
    }
