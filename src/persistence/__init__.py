"""Canonical exports for persistence models, sessions, schema and repositories."""

from . import session as _session
from .models import *  # noqa: F401,F403
from .session import *  # noqa: F401,F403
from .schema import *  # noqa: F401,F403
from .repositories import *  # noqa: F401,F403

# ``reset_engine_for_testing`` replaces ``session.engine``. Do not retain the
# old object as a second facade-level binding.
globals().pop("engine", None)


def __getattr__(name: str):
    if name == "engine":
        return _session.engine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
