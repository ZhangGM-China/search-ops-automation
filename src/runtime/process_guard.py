import atexit
import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any


_ACTIVE_LOCKS: list["ProcessLock"] = []


class ProcessLockError(RuntimeError):
    pass


def require_expected_process_count(
    name: str,
    *,
    actual_count: int,
    expected_count: int,
    phase: str,
) -> dict[str, Any]:
    if (
        isinstance(actual_count, bool)
        or isinstance(expected_count, bool)
        or not isinstance(actual_count, int)
        or not isinstance(expected_count, int)
        or actual_count < 0
        or expected_count < 0
    ):
        raise ProcessLockError(f"invalid {name} process count")
    if actual_count != expected_count:
        raise ProcessLockError(
            f"{name} process count mismatch during {phase}: "
            f"expected {expected_count}, got {actual_count}"
        )
    return {
        "name": name,
        "phase": phase,
        "actualCount": actual_count,
        "expectedCount": expected_count,
        "ok": True,
    }


@dataclass
class ProcessLock:
    name: str
    path: Path
    handle: IO[str]

    def release(self) -> None:
        if self.handle.closed:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            # 保留 inode，避免等待者锁住旧文件后另一个进程创建同名新锁。
            if self in _ACTIVE_LOCKS:
                _ACTIVE_LOCKS.remove(self)


def _default_lock_dir() -> Path:
    configured = os.getenv("SCHEDULER_RUNTIME_DIR")
    if configured:
        path = Path(configured)
        if path.is_absolute():
            return path
    return Path(__file__).resolve().parents[1] / "data" / "runtime"


def _sanitize_lock_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-", "."} else "_" for char in name)


def acquire_process_lock(name: str, metadata: dict[str, Any] | None = None, lock_dir: Path | None = None) -> ProcessLock:
    runtime_dir = lock_dir or _default_lock_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / f"{_sanitize_lock_name(name)}.lock"
    handle = lock_path.open("a+", encoding="utf-8")

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.seek(0)
        detail = handle.read().strip()
        handle.close()
        raise ProcessLockError(
            f"process lock already held for {name}: {detail or lock_path}"
        ) from exc

    payload = {
        "pid": os.getpid(),
        "name": name,
        "metadata": metadata or {},
    }
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(payload, ensure_ascii=False))
    handle.flush()
    os.fsync(handle.fileno())

    lock = ProcessLock(name=name, path=lock_path, handle=handle)
    _ACTIVE_LOCKS.append(lock)
    return lock


@atexit.register
def _release_active_locks() -> None:
    for lock in list(_ACTIVE_LOCKS):
        lock.release()
