"""Legacy aggregation point for callers that still import persistence.database.

New code imports the narrow ``models``, ``session``, ``schema`` or
``repositories`` module directly.  Keeping this aggregation avoids a risky
all-at-once import rewrite while the public ``src.persistence`` facade remains
the preferred API.
"""

from . import session as _session
from .models import *  # noqa: F401,F403
from .repositories import *  # noqa: F401,F403
from .repositories import (  # noqa: F401
    _operation_log_retention_cutoff,
    _scheduler_leader_db_expiry_expression,
)
from .schema import *  # noqa: F401,F403
from .schema import _ensure_schema_upgrades  # noqa: F401
from .session import get_engine, reset_engine_for_testing  # noqa: F401


def __getattr__(name: str):
    if name in {"engine", "SessionLocal"}:
        return getattr(_session, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
