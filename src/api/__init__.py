"""Stable public API entrypoint.

Deployment continues to use ``src.api:app`` while implementation modules live
below this package.
"""

from .app import *  # noqa: F401,F403
from .routers.health import health_check, liveness_check, readiness_check, startup_check
from .app import app as app
