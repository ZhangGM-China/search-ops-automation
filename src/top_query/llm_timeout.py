import queue
import threading
import time
from typing import Any, Callable, Dict, List


class _BoundedDaemonCallPool:
    def __init__(
        self,
        worker_count: int = 4,
        queue_capacity: int = 4,
        monotonic_fn: Callable[[], float] = time.monotonic,
        before_claim: Callable[[], None] | None = None,
    ):
        self._queue: queue.Queue = queue.Queue(maxsize=queue_capacity)
        self._state_condition = threading.Condition()
        self._outstanding_count = 0
        self._monotonic = monotonic_fn
        self._before_claim = before_claim
        self._workers = [
            threading.Thread(
                target=self._worker,
                name=f"top-query-legacy-llm-{index + 1}",
                daemon=True,
            )
            for index in range(worker_count)
        ]
        for worker in self._workers:
            worker.start()

    def invoke(self, invoke_fn: Callable[..., Any], messages: List[Dict[str, str]], timeout_seconds: float) -> Any:
        deadline = self._monotonic() + timeout_seconds
        task = {
            "invoke_fn": invoke_fn,
            "messages": messages,
            "done": threading.Event(),
            "state": "pending",
            "succeeded": False,
            "value": None,
            "deadline": deadline,
            "lock": threading.Lock(),
        }
        with self._state_condition:
            self._outstanding_count += 1
        try:
            self._queue.put(task, timeout=max(0.0, deadline - self._monotonic()))
        except queue.Full as exc:
            with self._state_condition:
                self._outstanding_count -= 1
                self._state_condition.notify_all()
            raise TimeoutError("Legacy LLM invoke queue is saturated") from exc
        remaining = deadline - self._monotonic()
        if remaining <= 0 or not task["done"].wait(remaining):
            with task["lock"]:
                if task["state"] == "pending":
                    task["state"] = "cancelled"
                    task["value"] = TimeoutError("LLM invoke exceeded remaining time budget")
                    task["done"].set()
            raise TimeoutError("LLM invoke exceeded remaining time budget")
        with task["lock"]:
            state = task["state"]
            succeeded = task["succeeded"]
            value = task["value"]
        if state == "finished" and succeeded:
            return value
        if isinstance(value, BaseException):
            raise value
        raise TimeoutError(f"Legacy LLM task ended in unexpected state: {state}")

    def stats(self) -> Dict[str, int]:
        with self._state_condition:
            outstanding_count = self._outstanding_count
        return {
            "workerCount": len(self._workers),
            "aliveWorkerCount": sum(worker.is_alive() for worker in self._workers),
            "queuedTaskCount": self._queue.qsize(),
            "queueCapacity": self._queue.maxsize,
            "outstandingTaskCount": outstanding_count,
        }

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self._state_condition:
            while self._outstanding_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._state_condition.wait(remaining)
        return True

    def close(self) -> None:
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=1)

    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                self._queue.task_done()
                return
            try:
                if self._before_claim is not None:
                    self._before_claim()
                with task["lock"]:
                    if task["state"] != "pending":
                        claimed = False
                    elif self._monotonic() >= task["deadline"]:
                        task["state"] = "cancelled"
                        task["value"] = TimeoutError("LLM invoke exceeded remaining time budget")
                        task["done"].set()
                        claimed = False
                    else:
                        task["state"] = "running"
                        claimed = True
                if claimed:
                    try:
                        value = task["invoke_fn"](task["messages"])
                        succeeded = True
                    except BaseException as exc:
                        value = exc
                        succeeded = False
                    with task["lock"]:
                        task["value"] = value
                        task["succeeded"] = succeeded
                        task["state"] = "finished"
                        task["done"].set()
            finally:
                self._queue.task_done()
                with self._state_condition:
                    self._outstanding_count -= 1
                    self._state_condition.notify_all()


_pool: _BoundedDaemonCallPool | None = None
_pool_lock = threading.Lock()


def invoke_legacy_with_timeout(
    invoke_fn: Callable[..., Any],
    messages: List[Dict[str, str]],
    timeout_seconds: float,
) -> Any:
    return _get_pool().invoke(invoke_fn, messages, timeout_seconds)


def legacy_call_pool_stats() -> Dict[str, int]:
    return _get_pool().stats()


def wait_for_legacy_call_pool_idle(timeout_seconds: float) -> bool:
    return _get_pool().wait_for_idle(timeout_seconds)


def _get_pool() -> _BoundedDaemonCallPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _BoundedDaemonCallPool()
    return _pool
