from __future__ import annotations

import fcntl
import hashlib
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class TopQueryTaskLockError(RuntimeError):
    pass


class TopQueryTaskLockBusy(TopQueryTaskLockError):
    def __init__(self, task_id: int):
        self.task_id = int(task_id)
        super().__init__(f"top query task lock busy: {self.task_id}")


class TopQueryBrandDayLockBusy(TopQueryTaskLockError):
    def __init__(self, brand: str, data_date: str):
        self.brand = str(brand)
        self.data_date = str(data_date)
        super().__init__(
            f"top query brand day lock busy: {self.brand}:{self.data_date}"
        )


_THREAD_STATE = threading.local()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _held_locks() -> dict[str, dict[str, object]]:
    held = getattr(_THREAD_STATE, "held_locks", None)
    if held is None:
        held = {}
        _THREAD_STATE.held_locks = held
    return held


def _lock_dir() -> Path:
    configured_dir = os.getenv(
        "TOP_QUERY_TASK_LOCK_DIR",
        "data/runtime/top_query_task_locks",
    )
    lock_dir = Path(configured_dir)
    if not lock_dir.is_absolute():
        lock_dir = _PROJECT_ROOT / lock_dir
    return lock_dir


def _lock_path(task_id: int) -> Path:
    return _lock_dir() / f"task-{int(task_id)}.lock"


@contextmanager
def _file_lock(
    lock_path: Path,
    *,
    blocking: bool,
    busy_error: TopQueryTaskLockError,
) -> Iterator[None]:
    lock_key = str(lock_path.resolve())
    held = _held_locks()
    current = held.get(lock_key)
    if current is not None:
        current["depth"] = int(current["depth"]) + 1
        try:
            yield
        finally:
            current["depth"] = int(current["depth"]) - 1
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), operation)
    except BlockingIOError as exc:
        handle.close()
        raise busy_error from exc
    except Exception:
        handle.close()
        raise

    held[lock_key] = {"handle": handle, "depth": 1}
    try:
        yield
    finally:
        entry = held.pop(lock_key, None)
        if entry is not None:
            locked_handle = entry["handle"]
            try:
                fcntl.flock(locked_handle.fileno(), fcntl.LOCK_UN)
            finally:
                locked_handle.close()


@contextmanager
def top_query_task_lock(task_id: int) -> Iterator[None]:
    task_id = int(task_id)
    if task_id <= 0:
        raise TopQueryTaskLockError("top query task id must be positive")
    with _file_lock(
        _lock_path(task_id),
        blocking=False,
        busy_error=TopQueryTaskLockBusy(task_id),
    ):
        yield


@contextmanager
def top_query_brand_day_lock(brand: str, data_date) -> Iterator[None]:
    normalized_brand = str(brand or "").strip().lower()
    normalized_date = data_date.isoformat() if hasattr(data_date, "isoformat") else str(data_date)
    if not normalized_brand or not normalized_date:
        raise TopQueryTaskLockError("top query brand and data date are required")
    scope = f"{normalized_brand}|{normalized_date}"
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    with _file_lock(
        _lock_dir() / f"brand-day-{digest}.lock",
        blocking=True,
        busy_error=TopQueryBrandDayLockBusy(normalized_brand, normalized_date),
    ):
        yield


@contextmanager
def top_query_brand_governance_lock(brand: str) -> Iterator[None]:
    normalized_brand = str(brand or "").strip().lower()
    if not normalized_brand:
        raise TopQueryTaskLockError("top query brand is required")
    digest = hashlib.sha256(normalized_brand.encode("utf-8")).hexdigest()
    with _file_lock(
        _lock_dir() / f"brand-governance-{digest}.lock",
        blocking=True,
        busy_error=TopQueryTaskLockError(
            f"top query brand governance lock busy: {normalized_brand}"
        ),
    ):
        yield
