from typing import Any, Dict

from sqlalchemy.engine import Engine

from src.config import get_env
from .release_schema import ensure_release_schema


def ensure_zha26_storage_schema(engine: Engine) -> Dict[str, Any]:
    if get_env() != "test":
        raise RuntimeError("ZHA-26 storage migration requires ENV=test")

    return ensure_release_schema(engine)
