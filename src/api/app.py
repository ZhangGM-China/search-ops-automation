"""Stable public FastAPI entrypoint and application assembly."""

from __future__ import annotations

import sys
import time
import uuid
import logging
import re
from types import ModuleType

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import get_env

from .contracts import install_openapi_contract
from .lifecycle import _set_startup_probe_state, api_lifespan
from .routers.admin import router as admin_router
from .routers.assistant import router as assistant_router
from .routers.auth import router as auth_router
from .routers.dashboard import router as dashboard_router
from .routers.health import router as health_router
from .routers.no_result import router as no_result_router
from .routers.query_governance import router as query_governance_router

request_logger = logging.getLogger(__name__)
SLOW_REQUEST_THRESHOLD_MS = 1000.0
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")


def _install_cors(app_instance: FastAPI) -> None:
    local_dev_origin_regex = (
        r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$"
        if get_env() == "test"
        else None
    )
    app_instance.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:5174",
            "http://localhost:5174",
            "http://127.0.0.1:5176",
            "http://localhost:5176",
        ],
        allow_origin_regex=local_dev_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def create_app() -> FastAPI:
    app_instance = FastAPI(
        title="Search Ops Automation Management API",
        lifespan=api_lifespan,
    )
    _set_startup_probe_state(app_instance, "starting")
    _install_cors(app_instance)

    @app_instance.middleware("http")
    async def add_server_timing(request, call_next):
        # The front proxy overwrites X-Request-ID with its own opaque id.
        # Ignore malformed direct-client values to prevent log injection.
        incoming_id = request.headers.get("X-Request-ID", "")
        request_id = incoming_id.lower() if REQUEST_ID_PATTERN.fullmatch(incoming_id) else uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            route_path = getattr(request.scope.get("route"), "path", None) or "<unmatched>"
            request_logger.warning(
                "api request failed requestId=%s method=%s path=%s appMs=%.1f authMs=%s workspaceMs=%s",
                request_id,
                request.method,
                route_path,
                elapsed_ms,
                getattr(request.state, "auth_duration_ms", None),
                getattr(request.state, "workspace_duration_ms", None),
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        auth_ms = getattr(request.state, "auth_duration_ms", None)
        workspace_ms = getattr(request.state, "workspace_duration_ms", None)
        if elapsed_ms >= SLOW_REQUEST_THRESHOLD_MS:
            route_path = getattr(request.scope.get("route"), "path", None) or "<unmatched>"
            request_logger.warning(
                "slow api request requestId=%s method=%s path=%s appMs=%.1f authMs=%s workspaceMs=%s",
                request_id,
                request.method,
                route_path,
                elapsed_ms,
                auth_ms,
                workspace_ms,
            )
        timings = [f"app;dur={elapsed_ms:.1f}"]
        for name in ("auth", "workspace"):
            duration_ms = getattr(request.state, f"{name}_duration_ms", None)
            if duration_ms is not None:
                timings.append(f"{name};dur={float(duration_ms):.1f}")
        response.headers["Server-Timing"] = ", ".join(timings)
        response.headers["X-Request-ID"] = request_id
        return response

    for router in (
        auth_router,
        admin_router,
        query_governance_router,
        no_result_router,
        assistant_router,
        dashboard_router,
        health_router,
    ):
        app_instance.include_router(router)
    install_openapi_contract(app_instance)
    return app_instance


app = create_app()

# The deployment entrypoint is intentionally stable. Re-export the existing
# handler symbols during this refactor so direct internal imports continue to
# resolve while callers migrate to their domain modules.
from . import dependencies as _dependencies
from . import lifecycle as _lifecycle
from . import shared as _shared
from .application import actions as _actions
from .application import admin as _admin_application
from .application import assistant as _assistant_application
from .application import auth as _auth_application
from .application import dashboard as _dashboard_application
from .application import no_result as _no_result_application
from .application import query_governance_legacy as _query_governance_legacy_application
from .application import query_governance_support as _query_governance_support

_COMPAT_MODULES = (
    _dependencies,
    _lifecycle,
    _shared,
    _actions,
    _auth_application,
    _admin_application,
    _query_governance_support,
    _query_governance_legacy_application,
    _no_result_application,
    _assistant_application,
    _dashboard_application,
)

for _compat_module in _COMPAT_MODULES:
    for _name in _compat_module.__all__:
        globals().setdefault(_name, getattr(_compat_module, _name))


__all__ = [name for name in globals() if not name.startswith("_")]


class _CompatibilityExportModule(ModuleType):
    """Keep existing internal mock/import targets aligned with moved handlers."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _COMPAT_MODULES:
            if name in module.__dict__:
                setattr(module, name, value)


sys.modules[__name__].__class__ = _CompatibilityExportModule
