from src.integrations.business_http import BusinessRequestGovernor
import src.integrations.business_http as business_http
from src.integrations.operational_service import OperationalService
from unittest.mock import MagicMock
import concurrent.futures
import fcntl
import threading
import time

import pytest


@pytest.mark.parametrize("configured_dir", [None, "data/runtime/custom-business-http"])
def test_business_http_state_dir_is_stable_across_working_directories(
    configured_dir,
    monkeypatch,
    tmp_path,
):
    if configured_dir is None:
        monkeypatch.delenv("BUSINESS_HTTP_STATE_DIR", raising=False)
    else:
        monkeypatch.setenv("BUSINESS_HTTP_STATE_DIR", configured_dir)
    first = BusinessRequestGovernor()
    first_path = first.state_dir.resolve()

    monkeypatch.chdir(tmp_path)
    second = BusinessRequestGovernor()

    assert first.state_dir.is_absolute()
    assert second.state_dir.resolve() == first_path


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def record_failure(registry, key):
    permit = registry.admit(key)
    assert permit is not None
    registry.record_failure(key, permit)


def test_shared_governor_limits_two_instances_to_four_starts_per_window(tmp_path):
    clock = FakeClock()
    first = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    second = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    key = "test|search.example.test|tenant:235"

    for governor in (first, second, first, second):
        with governor.acquire(key, request_timeout=15):
            pass

    with first.acquire(key, request_timeout=15):
        pass

    assert clock.sleeps == [1.0]


def test_request_lease_exposes_governor_recorded_start_time(tmp_path):
    clock = FakeClock()
    governor = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )

    lease = governor.acquire(
        "test|search.example.test|tenant:235",
        request_timeout=15,
    )

    assert lease.started_at == 100.0
    lease.release()


def test_shared_governor_caps_in_flight_and_recovers_expired_lease(tmp_path):
    clock = FakeClock()
    governor = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    key = "test|search.example.test|tenant:235"
    leases = [governor.acquire(key, request_timeout=2) for _ in range(4)]

    clock.now += 8.0
    governor.sleep = lambda seconds: (_ for _ in ()).throw(
        AssertionError(f"expired leases should not wait: {seconds}")
    )
    recovered = governor.acquire(key, request_timeout=2)

    assert recovered is not None
    for lease in [*leases, recovered]:
        lease.release()


def test_request_heartbeat_keeps_slow_requests_within_four_in_flight(tmp_path):
    governor = BusinessRequestGovernor(
        state_dir=tmp_path,
        lease_grace_seconds=0.25,
        lease_heartbeat_seconds=0.1,
    )
    active = 0
    max_active = 0
    active_lock = threading.Lock()
    first_four_started = threading.Event()
    release_requests = threading.Event()

    def slow_request(method, url):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
            if active == 4:
                first_four_started.set()
        release_requests.wait(timeout=3)
        with active_lock:
            active -= 1
        return MagicMock(status_code=200, headers={})

    def governed_request():
        return governor.request_with_rate_limit_retry(
            key="test|search.example.test|tenant:235",
            method="POST",
            url="http://search.example.test/api/search/mixed",
            request_func=slow_request,
            max_attempts=1,
            request_timeout=0.01,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        first = [executor.submit(governed_request) for _ in range(4)]
        assert first_four_started.wait(timeout=2)
        time.sleep(1.05)
        fifth = executor.submit(governed_request)
        time.sleep(0.1)

        assert max_active == 4
        assert fifth.done() is False

        release_requests.set()
        for future in [*first, fifth]:
            assert future.result(timeout=2).status_code == 200

    assert max_active == 4


def test_429_honors_retry_after_and_reenters_limiter(tmp_path):
    clock = FakeClock()
    governor = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    request = MagicMock(side_effect=[
        MagicMock(status_code=429, headers={"Retry-After": "2"}),
        MagicMock(status_code=200, headers={}),
    ])

    result = governor.request_with_rate_limit_retry(
        key="test|search.example.test|tenant:235",
        method="POST",
        url="http://search.example.test/api/search/mixed",
        request_func=request,
        max_attempts=2,
        request_timeout=15,
    )

    assert result.status_code == 200
    assert request.call_count == 2
    assert clock.sleeps == [2.0]
    assert governor.metrics()["rateLimitedResponses"] == 1


def test_governor_metrics_report_wait_retry_and_structured_failure(tmp_path):
    clock = FakeClock()
    governor = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    request = MagicMock(side_effect=[
        MagicMock(status_code=429, headers={"Retry-After": "2"}),
        MagicMock(status_code=200, headers={}),
    ])

    governor.request_with_rate_limit_retry(
        key="test|search.example.test|tenant:235",
        method="POST",
        url="http://search.example.test/api/search/mixed",
        request_func=request,
        max_attempts=2,
        request_timeout=15,
    )
    governor.record_structured_failure()

    assert governor.metrics() == {
        "rateLimitedResponses": 1,
        "waitSeconds": 2.0,
        "rateLimitRetryCount": 1,
        "structuredFailureCount": 1,
    }


def test_runtime_status_exposes_sanitized_circuit_ids_and_shared_telemetry(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("BUSINESS_HTTP_STATE_DIR", str(tmp_path))
    governor = BusinessRequestGovernor(state_dir=tmp_path)
    governor.record_structured_failure()
    registry = business_http.CircuitRegistry(
        state_dir=tmp_path,
        failure_threshold=1,
    )
    raw_key = "test|search.example.test|tenant:235"
    permit = registry.admit(raw_key)
    assert permit is not None
    registry.record_failure(raw_key, permit)

    status = OperationalService.get_runtime_status()

    assert status["limiter"]["structuredFailureCount"] == 1
    assert status["circuit"]["openCircuitCount"] == 1
    assert len(status["circuit"]["openCircuitIds"]) == 1
    assert status["circuit"]["openCircuitIds"][0].startswith("sha256:")
    assert raw_key not in str(status)


def test_429_retry_after_blocks_other_governor_instances(tmp_path):
    clock = FakeClock()
    first = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    second = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    key = "test|search.example.test|tenant:235"

    first.request_with_rate_limit_retry(
        key=key,
        method="POST",
        url="http://search.example.test/api/search/mixed",
        request_func=lambda method, url: MagicMock(
            status_code=429,
            headers={"Retry-After": "2"},
        ),
        max_attempts=1,
        request_timeout=15,
    )
    with second.acquire(key, request_timeout=15):
        pass

    assert clock.sleeps == [2.0]


def test_429_cooldown_is_persisted_before_lease_release_becomes_visible(tmp_path):
    clock = FakeClock()
    release_written = threading.Event()
    allow_first_to_continue = threading.Event()
    second_waiting = threading.Event()
    allow_second_to_continue = threading.Event()
    second_acquired = threading.Event()

    def controlled_sleep(seconds):
        second_waiting.set()
        assert allow_second_to_continue.wait(timeout=1)
        clock.sleep(seconds)

    first = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    second = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=controlled_sleep,
    )
    original_release = first._release

    def delayed_release(*args, **kwargs):
        original_release(*args, **kwargs)
        release_written.set()
        allow_first_to_continue.wait(timeout=1)

    first._release = delayed_release

    first_thread = threading.Thread(target=lambda: first.request_with_rate_limit_retry(
        key="test|search.example.test|tenant:235",
        method="POST",
        url="http://search.example.test/api/search/mixed",
        request_func=lambda method, url: MagicMock(
            status_code=429,
            headers={"Retry-After": "0.2"},
        ),
        max_attempts=1,
        request_timeout=15,
    ))

    def acquire_second():
        with second.acquire("test|search.example.test|tenant:235", request_timeout=15):
            second_acquired.set()

    first_thread.start()
    assert release_written.wait(timeout=1)
    second_thread = threading.Thread(target=acquire_second)
    second_thread.start()
    assert second_waiting.wait(timeout=1)
    assert second_acquired.is_set() is False

    allow_first_to_continue.set()
    allow_second_to_continue.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)
    assert second_acquired.is_set() is True


def test_circuit_outcome_permit_is_reusable_when_state_write_fails(tmp_path, monkeypatch):
    registry = business_http.CircuitRegistry(
        state_dir=tmp_path,
        failure_threshold=2,
    )
    key = "test|search.example.test|tenant:178"
    permit = registry.admit(key)
    assert permit is not None
    original_write_state = registry._store._write_state
    attempts = 0

    def fail_first_write(state):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("state disk unavailable")
        return original_write_state(state)

    monkeypatch.setattr(registry._store, "_write_state", fail_first_write)

    with pytest.raises(business_http.BusinessRequestLimiterUnavailable):
        registry.record_failure(key, permit)

    registry.record_failure(key, permit)
    assert registry.status(key)["consecutive_failures"] == 1


def test_circuit_opened_at_zero_still_enters_half_open_after_cooldown(tmp_path):
    clock = FakeClock()
    clock.now = 0.0
    registry = business_http.CircuitRegistry(
        state_dir=tmp_path,
        clock=clock.time,
        failure_threshold=1,
        cooldown_seconds=10,
    )
    key = "test|search.example.test|tenant:178"
    permit = registry.admit(key)
    assert permit is not None
    registry.record_failure(key, permit)

    clock.now = 10.0

    probe = registry.admit(key)
    assert probe is not None
    assert probe.role == "probe"


def test_legacy_circuit_state_is_normalized_with_new_permit_fields(tmp_path):
    registry = business_http.CircuitRegistry(state_dir=tmp_path)
    key = "test|search.example.test|tenant:178"
    circuit_key = registry._store._hash_key(key)
    with registry._store._locked_state() as state:
        state["circuits"] = {
            circuit_key: {
                "state": "closed",
                "consecutive_failures": 0,
                "opened_at": None,
                "half_open_probe_in_flight": False,
                "half_open_probe_expires_at": None,
            }
        }

    permit = registry.admit(key)
    assert permit is not None
    registry.record_success(key, permit)

    status = registry.status(key)
    assert status["generation"] == 0
    assert status["half_open_probe_id"] is None


def test_circuit_isolated_by_upstream_and_recovers_with_single_half_open_probe(tmp_path):
    registry_class = getattr(business_http, "CircuitRegistry", None)
    assert registry_class is not None
    clock = FakeClock()
    first = registry_class(
        state_dir=tmp_path,
        clock=clock.time,
        failure_threshold=2,
        cooldown_seconds=10,
    )
    second = registry_class(
        state_dir=tmp_path,
        clock=clock.time,
        failure_threshold=2,
        cooldown_seconds=10,
    )

    record_failure(first, "test|search.example.test|tenant:178")
    record_failure(first, "test|search.example.test|tenant:178")

    assert first.admit("test|search.example.test|tenant:178") is None
    assert first.admit("test|search.example.test|tenant:235") is not None

    clock.now += 10
    probe = first.admit("test|search.example.test|tenant:178")
    assert probe is not None
    assert second.admit("test|search.example.test|tenant:178") is None

    first.record_success("test|search.example.test|tenant:178", probe)
    assert second.admit("test|search.example.test|tenant:178") is not None


def test_rate_limited_half_open_probe_returns_to_open_without_failure_increment(tmp_path):
    clock = FakeClock()
    registry = business_http.CircuitRegistry(
        state_dir=tmp_path,
        clock=clock.time,
        failure_threshold=1,
        cooldown_seconds=10,
    )
    key = "test|search.example.test|tenant:178"
    record_failure(registry, key)
    clock.now += 10
    probe = registry.admit(key)
    assert probe is not None

    record_rate_limited = getattr(registry, "record_rate_limited", None)
    assert record_rate_limited is not None
    record_rate_limited(key, probe)

    status = registry.status(key)
    assert status["state"] == "open"
    assert status["consecutive_failures"] == 1
    assert registry.admit(key) is None


def test_abandoned_half_open_probe_expires_and_allows_takeover(tmp_path):
    clock = FakeClock()
    registry = business_http.CircuitRegistry(
        state_dir=tmp_path,
        clock=clock.time,
        failure_threshold=1,
        cooldown_seconds=10,
        half_open_probe_timeout=30,
    )
    key = "test|search.example.test|tenant:178"
    record_failure(registry, key)
    clock.now += 10
    assert registry.admit(key) is not None

    clock.now += 31

    assert registry.admit(key) is not None


def test_stale_closed_success_cannot_close_circuit_opened_by_concurrent_failure(tmp_path):
    registry = business_http.CircuitRegistry(
        state_dir=tmp_path,
        failure_threshold=10,
        cooldown_seconds=60,
    )
    key = "test|search.example.test|tenant:178"
    for _ in range(9):
        record_failure(registry, key)

    permit_a = registry.admit(key)
    permit_b = registry.admit(key)
    assert permit_a is not None
    assert permit_b is not None

    registry.record_failure(key, permit_a)
    registry.record_success(key, permit_b)

    status = registry.status(key)
    assert status["state"] == "open"
    assert status["consecutive_failures"] == 10


def test_circuit_permit_metadata_is_read_only(tmp_path):
    registry = business_http.CircuitRegistry(state_dir=tmp_path)
    key_a = "test|search.example.test|tenant:178"
    key_b = "test|search.example.test|tenant:235"
    permit = registry.admit(key_a)
    assert permit is not None

    forged_values = {
        "_circuit_key": registry._store._hash_key(key_b),
        "_generation": permit.generation + 1,
        "_role": "probe",
        "_probe_id": "forged",
        "_issuer": object(),
        "_consumed": True,
        "generation": permit.generation + 1,
        "role": "probe",
        "probe_id": "forged",
    }
    for field, value in forged_values.items():
        with pytest.raises(AttributeError):
            setattr(permit, field, value)

    registry.record_failure(key_b, permit)
    assert registry.status(key_b)["consecutive_failures"] == 0


def test_circuit_outcomes_require_non_null_admission_permit(tmp_path):
    registry = business_http.CircuitRegistry(state_dir=tmp_path)
    key = "test|search.example.test|tenant:178"

    for outcome_name in ("record_success", "record_failure", "record_rate_limited"):
        with pytest.raises(TypeError):
            getattr(registry, outcome_name)(key, None)
        with pytest.raises(TypeError):
            getattr(registry, outcome_name)(key)
    assert registry.status(key)["consecutive_failures"] == 0


def test_only_current_half_open_probe_can_change_state_after_takeover(tmp_path):
    for outcome_name in ("record_success", "record_failure", "record_rate_limited"):
        clock = FakeClock()
        registry = business_http.CircuitRegistry(
            state_dir=tmp_path / outcome_name,
            clock=clock.time,
            failure_threshold=1,
            cooldown_seconds=10,
            half_open_probe_timeout=30,
        )
        key = "test|search.example.test|tenant:178"
        permit = registry.admit(key)
        assert permit is not None
        registry.record_failure(key, permit)
        clock.now += 10
        probe_1 = registry.admit(key)
        assert probe_1 is not None

        clock.now += 31
        probe_2 = registry.admit(key)
        assert probe_2 is not None

        getattr(registry, outcome_name)(key, probe_1)
        assert registry.status(key)["state"] == "half_open"

        registry.record_success(key, probe_2)
        assert registry.status(key)["state"] == "closed"


def test_governor_snapshot_reports_active_leases_without_exposing_keys(tmp_path):
    clock = FakeClock()
    governor = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    lease = governor.acquire(
        "test|search.example.test|tenant:235",
        request_timeout=15,
    )

    snapshot_method = getattr(governor, "snapshot", None)
    assert snapshot_method is not None
    snapshot = snapshot_method()
    assert snapshot == {
        "ratePerSecond": 4,
        "maxInFlight": 4,
        "activeLeases": 1,
    }

    lease.release()
    assert snapshot_method()["activeLeases"] == 0


def test_telemetry_reads_non_blocking_under_exclusive_file_lock(tmp_path):
    clock = FakeClock()
    governor = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    registry = business_http.CircuitRegistry(
        state_dir=tmp_path,
        clock=clock.time,
    )
    governor.record_structured_failure()

    lock_path = tmp_path / "business_http.lock"
    lock_file = lock_path.open("a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    try:
        snapshot = governor.snapshot()
        assert snapshot["ratePerSecond"] == 4
        assert snapshot["maxInFlight"] == 4
        assert snapshot["activeLeases"] == 0

        metrics = governor.metrics()
        assert metrics["structuredFailureCount"] == 1

        open_keys = registry.open_keys()
        assert open_keys == []

        recovery = registry.last_recovery_at()
        assert recovery is None
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def test_telemetry_fallback_to_cache_when_state_unreadable(tmp_path):
    clock = FakeClock()
    governor = BusinessRequestGovernor(
        state_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
    )
    governor.record_structured_failure()
    assert governor.metrics()["structuredFailureCount"] == 1

    state_path = tmp_path / "business_http.json"
    state_path.write_text("invalid json{{{", encoding="utf-8")

    assert governor.metrics()["structuredFailureCount"] == 1
    assert governor.snapshot()["ratePerSecond"] == 4

