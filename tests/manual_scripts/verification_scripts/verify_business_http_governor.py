#!/usr/bin/env python3
import concurrent.futures
import fcntl
import json
import multiprocessing
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from src.integrations.business_http import BusinessRequestGovernor


IDENTITY = "test|search.example.test|tenant:235"
ENDPOINT_TYPES = ("search", "product_detail", "knowledge_graph", "governance_write")
TOTAL_REQUESTS = 50
PROCESS_COUNT = 2
HOLD_SECONDS = 0.08


def _worker(
    state_dir,
    labels,
):
    governor = BusinessRequestGovernor(state_dir=Path(state_dir))
    state_dir = Path(state_dir)
    start_signal = state_dir / "verify.start"
    while not start_signal.exists():
        time.sleep(0.005)

    def run_one(label):
        lease = governor.acquire(IDENTITY, request_timeout=5)
        _record_probe_state(
            state_dir,
            active_delta=1,
            record=(lease.started_at, label, os.getpid()),
        )
        try:
            time.sleep(HOLD_SECONDS)
        finally:
            lease.release()
            _record_probe_state(state_dir, active_delta=-1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(labels)) as executor:
        futures = [executor.submit(run_one, label) for label in labels]
        for future in futures:
            future.result()


def _record_probe_state(state_dir, *, active_delta, record=None):
    lock_path = Path(state_dir) / "verify.lock"
    state_path = Path(state_dir) / "verify.json"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if state_path.exists():
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            payload = {"active": 0, "maxActive": 0, "records": []}
        payload["active"] = int(payload.get("active", 0)) + int(active_delta)
        payload["maxActive"] = max(
            int(payload.get("maxActive", 0)),
            payload["active"],
        )
        if record is not None:
            payload.setdefault("records", []).append(list(record))
        temp_path = state_path.with_suffix(f".{os.getpid()}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp_path, state_path)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _max_rolling_starts(timestamps):
    ordered = sorted(float(value) for value in timestamps)
    maximum = 0
    left = 0
    for right, timestamp in enumerate(ordered):
        while left <= right and ordered[left] <= timestamp - 1.0:
            left += 1
        maximum = max(maximum, right - left + 1)
    return maximum


def main() -> None:
    context = multiprocessing.get_context("spawn")
    labels = [ENDPOINT_TYPES[index % len(ENDPOINT_TYPES)] for index in range(TOTAL_REQUESTS)]
    groups = [labels[index::PROCESS_COUNT] for index in range(PROCESS_COUNT)]

    with tempfile.TemporaryDirectory(prefix="zha41-business-http-") as state_dir:
        processes = [
            context.Process(
                target=_worker,
                args=(
                    state_dir,
                    group,
                ),
            )
            for group in groups
        ]
        started_at = time.monotonic()
        for process in processes:
            process.start()
        Path(state_dir, "verify.start").touch()
        for process in processes:
            process.join(timeout=60)
            assert process.exitcode == 0, f"worker failed: pid={process.pid}, exit={process.exitcode}"
        duration = time.monotonic() - started_at

        probe_state = json.loads(
            Path(state_dir, "verify.json").read_text(encoding="utf-8")
        )
        records = probe_state["records"]
        timestamps = [record[0] for record in records]
        max_rolling_starts = _max_rolling_starts(timestamps)
        final_snapshot = BusinessRequestGovernor(state_dir=Path(state_dir)).snapshot()

    assert len(records) == TOTAL_REQUESTS
    assert max_rolling_starts <= BusinessRequestGovernor.RATE_PER_SECOND
    assert probe_state["maxActive"] <= BusinessRequestGovernor.MAX_IN_FLIGHT
    assert probe_state["active"] == 0
    assert final_snapshot["activeLeases"] == 0
    assert set(label for _, label, _ in records) == set(ENDPOINT_TYPES)
    assert len(set(pid for _, _, pid in records)) == PROCESS_COUNT

    print(json.dumps({
        "status": "PASS",
        "processes": PROCESS_COUNT,
        "requests": TOTAL_REQUESTS,
        "endpointCounts": dict(sorted(Counter(label for _, label, _ in records).items())),
        "maxRollingStartsPerSecond": max_rolling_starts,
        "maxInFlight": probe_state["maxActive"],
        "durationSeconds": round(duration, 3),
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
