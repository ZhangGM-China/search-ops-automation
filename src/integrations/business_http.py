from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import random
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BusinessRequestLimiterUnavailable(RuntimeError):
    pass


class BusinessCircuitOpen(RuntimeError):
    pass


class RequestLease:
    def __init__(
        self,
        governor: "BusinessRequestGovernor",
        bucket_key: str,
        request_id: str,
        started_at: float,
    ) -> None:
        self._governor = governor
        self._bucket_key = bucket_key
        self._request_id = request_id
        self.started_at = float(started_at)
        self._released = False

    def __enter__(self) -> "RequestLease":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._governor._release(self._bucket_key, self._request_id)
        self._released = True

    def release_rate_limited(self, delay_seconds: float) -> None:
        if self._released:
            return
        self._governor._release(
            self._bucket_key,
            self._request_id,
            defer_seconds=delay_seconds,
            increment_rate_limited=True,
        )
        self._released = True

    def renew(self, request_timeout: float) -> None:
        if self._released:
            return
        self._governor._renew(
            self._bucket_key,
            self._request_id,
            request_timeout=request_timeout,
        )


class BusinessRequestGovernor:
    RATE_PER_SECOND = 4
    MAX_IN_FLIGHT = 4
    WINDOW_SECONDS = 1.0

    _telemetry_cache: dict[str, dict[str, Any]] = {}
    _telemetry_cache_lock: threading.Lock = threading.Lock()

    @classmethod
    def _update_telemetry_cache(cls, cache_key: str, state: dict[str, Any]) -> None:
        try:
            copied = copy.deepcopy(state)
            with cls._telemetry_cache_lock:
                cls._telemetry_cache[cache_key] = copied
        except Exception:
            pass

    @classmethod
    def _get_cached_telemetry(cls, cache_key: str) -> dict[str, Any]:
        with cls._telemetry_cache_lock:
            cached = cls._telemetry_cache.get(cache_key)
            if cached is not None:
                try:
                    return copy.deepcopy(cached)
                except Exception:
                    return dict(cached)
        return cls._new_state()

    def _update_cache_state(self, state: dict[str, Any]) -> None:
        try:
            self._cached_telemetry = copy.deepcopy(state)
        except Exception:
            self._cached_telemetry = dict(state)
        self._cached_telemetry_time = float(self.clock())
        self._update_telemetry_cache(str(self.state_dir), state)

    def _read_state_safe(self) -> dict[str, Any]:
        cache_key = str(self.state_dir)
        try:
            state = self._read_state()
            self._update_cache_state(state)
            return state
        except Exception:
            return self._get_cached_telemetry(cache_key)

    def __init__(
        self,
        *,
        state_dir: Optional[Path] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        lease_grace_seconds: float = 5.0,
        lease_heartbeat_seconds: float = 1.0,
    ) -> None:
        configured_dir = Path(
            state_dir or os.getenv(
                "BUSINESS_HTTP_STATE_DIR",
                "data/runtime/business_http",
            )
        )
        if not configured_dir.is_absolute():
            configured_dir = _PROJECT_ROOT / configured_dir
        self.state_dir = configured_dir
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.lease_grace_seconds = max(0.05, float(lease_grace_seconds))
        self.lease_heartbeat_seconds = max(0.01, float(lease_heartbeat_seconds))
        self._lock_path = self.state_dir / "business_http.lock"
        self._state_path = self.state_dir / "business_http.json"
        self._cached_telemetry: Optional[dict[str, Any]] = None
        self._cached_telemetry_time: float = 0.0

    def acquire(self, key: str, request_timeout: float) -> RequestLease:
        bucket_key = self._hash_key(key)
        while True:
            with self._locked_state() as state:
                now = float(self.clock())
                bucket = self._bucket(state, bucket_key)
                self._trim(bucket, now)
                if (
                    not self._is_deferred(bucket, now)
                    and
                    len(bucket["starts"]) < self.RATE_PER_SECOND
                    and len(bucket["leases"]) < self.MAX_IN_FLIGHT
                ):
                    request_id = uuid.uuid4().hex
                    bucket["starts"].append(now)
                    bucket["leases"][request_id] = now + self._lease_ttl(request_timeout)
                    return RequestLease(self, bucket_key, request_id, now)
                wait_seconds = self._next_wait_seconds(bucket, now)
            self._increment_metric("wait_seconds", wait_seconds)
            self.sleep(wait_seconds)

    def request_with_rate_limit_retry(
        self,
        *,
        key: str,
        method: str,
        url: str,
        request_func: Callable[..., Any],
        max_attempts: int,
        request_timeout: float,
        **kwargs: Any,
    ) -> Any:
        response = None
        for attempt in range(max(1, int(max_attempts))):
            lease = self.acquire(key, request_timeout=request_timeout)
            heartbeat_stop = threading.Event()
            heartbeat_errors: list[Exception] = []
            heartbeat = threading.Thread(
                target=self._heartbeat_lease,
                args=(lease, request_timeout, heartbeat_stop, heartbeat_errors),
                daemon=True,
            )
            heartbeat.start()
            try:
                response = request_func(method, url, **kwargs)
            except Exception:
                heartbeat_stop.set()
                heartbeat.join(timeout=self.lease_heartbeat_seconds + 0.1)
                lease.release()
                raise
            heartbeat_stop.set()
            heartbeat.join(timeout=self.lease_heartbeat_seconds + 0.1)
            if heartbeat_errors:
                lease.release()
                raise heartbeat_errors[0]
            if getattr(response, "status_code", None) != 429:
                lease.release()
                return response
            lease.release_rate_limited(self._retry_after_seconds(response, attempt))
            if attempt + 1 >= max_attempts:
                return response
            self._increment_metric("rate_limit_retries")
        return response

    def _heartbeat_lease(
        self,
        lease: RequestLease,
        request_timeout: float,
        stop_event: threading.Event,
        errors: list[Exception],
    ) -> None:
        interval = min(
            self.lease_heartbeat_seconds,
            max(0.01, self._lease_ttl(request_timeout) / 2.0),
        )
        while not stop_event.wait(interval):
            try:
                lease.renew(request_timeout)
            except Exception as exc:
                errors.append(exc)
                return

    def _lease_ttl(self, request_timeout: float) -> float:
        return max(0.01, float(request_timeout)) + self.lease_grace_seconds

    def _defer(self, key: str, delay_seconds: float) -> None:
        bucket_key = self._hash_key(key)
        with self._locked_state() as state:
            now = float(self.clock())
            bucket = self._bucket(state, bucket_key)
            bucket["blocked_until"] = max(
                float(bucket.get("blocked_until") or 0.0),
                now + max(0.0, float(delay_seconds)),
            )

    def metrics(self) -> dict[str, Any]:
        with self._read_only_state() as state:
            metrics = state.get("metrics") or {}
            return {
                "rateLimitedResponses": int(metrics.get("rate_limited_responses", 0)),
                "waitSeconds": float(metrics.get("wait_seconds", 0.0)),
                "rateLimitRetryCount": int(metrics.get("rate_limit_retries", 0)),
                "structuredFailureCount": int(metrics.get("structured_failures", 0)),
            }

    def snapshot(self) -> dict[str, int]:
        with self._read_only_state() as state:
            now = float(self.clock())
            active_leases = 0
            for bucket in (state.get("buckets") or {}).values():
                if not isinstance(bucket, dict):
                    continue
                leases = bucket.get("leases")
                if isinstance(leases, dict):
                    active_leases += sum(
                        1 for expires_at in leases.values()
                        if float(expires_at) > now
                    )
            return {
                "ratePerSecond": self.RATE_PER_SECOND,
                "maxInFlight": self.MAX_IN_FLIGHT,
                "activeLeases": active_leases,
            }

    def record_structured_failure(self) -> None:
        self._increment_metric("structured_failures")

    def _increment_metric(self, name: str, amount: float = 1.0) -> None:
        with self._locked_state() as state:
            metrics = state.setdefault("metrics", {})
            metrics[name] = float(metrics.get(name, 0.0)) + float(amount)

    @staticmethod
    def _retry_after_seconds(response: Any, attempt: int) -> float:
        headers = getattr(response, "headers", {}) or {}
        raw_retry_after = headers.get("Retry-After")
        try:
            if raw_retry_after is not None:
                return max(0.0, float(raw_retry_after))
        except (TypeError, ValueError):
            pass
        backoff = min(4.0, float(2 ** attempt))
        return backoff + random.uniform(0.0, 0.25)

    def _release(
        self,
        bucket_key: str,
        request_id: str,
        *,
        defer_seconds: Optional[float] = None,
        increment_rate_limited: bool = False,
    ) -> None:
        with self._locked_state() as state:
            bucket = self._bucket(state, bucket_key)
            bucket["leases"].pop(request_id, None)
            if defer_seconds is not None:
                now = float(self.clock())
                bucket["blocked_until"] = max(
                    float(bucket.get("blocked_until") or 0.0),
                    now + max(0.0, float(defer_seconds)),
                )
            if increment_rate_limited:
                metrics = state.setdefault("metrics", {})
                metrics["rate_limited_responses"] = int(
                    metrics.get("rate_limited_responses", 0)
                ) + 1

    def _renew(
        self,
        bucket_key: str,
        request_id: str,
        *,
        request_timeout: float,
    ) -> None:
        with self._locked_state() as state:
            now = float(self.clock())
            bucket = self._bucket(state, bucket_key)
            self._trim(bucket, now)
            if request_id not in bucket["leases"]:
                raise BusinessRequestLimiterUnavailable(
                    "business request lease expired before heartbeat renewal"
                )
            bucket["leases"][request_id] = now + self._lease_ttl(request_timeout)

    @staticmethod
    def _hash_key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _new_state() -> dict[str, Any]:
        return {"version": 1, "buckets": {}, "metrics": {}}

    @staticmethod
    def _bucket(state: dict[str, Any], bucket_key: str) -> dict[str, Any]:
        buckets = state.setdefault("buckets", {})
        return buckets.setdefault(bucket_key, {
            "starts": [],
            "leases": {},
            "blocked_until": None,
        })

    def _trim(self, bucket: dict[str, Any], now: float) -> None:
        cutoff = now - self.WINDOW_SECONDS
        bucket["starts"] = [
            float(started_at)
            for started_at in bucket.get("starts", [])
            if cutoff < float(started_at) <= now
        ]
        bucket["leases"] = {
            str(request_id): float(expires_at)
            for request_id, expires_at in bucket.get("leases", {}).items()
            if float(expires_at) > now
        }
        if float(bucket.get("blocked_until") or 0.0) <= now:
            bucket["blocked_until"] = None

    @staticmethod
    def _is_deferred(bucket: dict[str, Any], now: float) -> bool:
        return float(bucket.get("blocked_until") or 0.0) > now

    def _next_wait_seconds(self, bucket: dict[str, Any], now: float) -> float:
        waits: list[float] = []
        blocked_until = float(bucket.get("blocked_until") or 0.0)
        if blocked_until > now:
            waits.append(blocked_until - now)
        starts = bucket.get("starts", [])
        if len(starts) >= self.RATE_PER_SECOND:
            waits.append(float(starts[0]) + self.WINDOW_SECONDS - now)
        leases = bucket.get("leases", {})
        if len(leases) >= self.MAX_IN_FLIGHT:
            waits.append(min(0.05, min(float(value) for value in leases.values()) - now))
        return max(0.001, min(waits) if waits else 0.001)

    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        lock_file = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            lock_file = self._lock_path.open("a+")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            state = self._read_state()
            yield state
            self._write_state(state)
            self._update_cache_state(state)
        except BusinessRequestLimiterUnavailable:
            raise
        except Exception as exc:
            raise BusinessRequestLimiterUnavailable(
                f"business request limiter state unavailable: {exc}"
            ) from exc
        finally:
            if lock_file is not None:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()

    @contextmanager
    def _read_only_state(self) -> Iterator[dict[str, Any]]:
        lock_file = None
        locked = False
        cache_key = str(self.state_dir)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            if self._lock_path.exists():
                try:
                    lock_file = self._lock_path.open("a+")
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    locked = True
                except (BlockingIOError, OSError):
                    locked = False
            try:
                state = self._read_state_safe()
            except Exception:
                state = self._get_cached_telemetry(cache_key)
        except Exception:
            state = self._get_cached_telemetry(cache_key)

        try:
            yield state
        finally:
            if lock_file is not None:
                if locked:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
                try:
                    lock_file.close()
                except Exception:
                    pass

    def _read_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return self._new_state()
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessRequestLimiterUnavailable(
                f"cannot read limiter state: {exc}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("buckets"), dict):
            raise BusinessRequestLimiterUnavailable("invalid limiter state schema")
        return payload

    def _write_state(self, state: dict[str, Any]) -> None:
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.state_dir,
                prefix="business_http.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                json.dump(state, temp_file, ensure_ascii=False, sort_keys=True)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(temp_path, self._state_path)
        except OSError as exc:
            raise BusinessRequestLimiterUnavailable(
                f"cannot write limiter state: {exc}"
            ) from exc
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


class _CircuitPermit:
    __slots__ = (
        "_circuit_key",
        "_consumed",
        "_issuer",
        "_lock",
        "_generation",
        "_probe_id",
        "_role",
    )

    def __init__(
        self,
        *,
        issuer: object,
        circuit_key: str,
        generation: int,
        role: str,
        probe_id: Optional[str],
    ) -> None:
        object.__setattr__(self, "_issuer", issuer)
        object.__setattr__(self, "_circuit_key", circuit_key)
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_generation", generation)
        object.__setattr__(self, "_role", role)
        object.__setattr__(self, "_probe_id", probe_id)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("circuit permit is immutable")

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def role(self) -> str:
        return self._role

    @property
    def probe_id(self) -> Optional[str]:
        return self._probe_id

    @contextmanager
    def outcome_guard(
        self,
        issuer: object,
        circuit_key: str,
    ) -> Iterator[bool]:
        with self._lock:
            if (
                self._consumed
                or self._issuer is not issuer
                or self._circuit_key != circuit_key
            ):
                yield False
                return
            yield True
            object.__setattr__(self, "_consumed", True)


class CircuitRegistry:
    def __init__(
        self,
        *,
        state_dir: Optional[Path] = None,
        clock: Optional[Callable[[], float]] = None,
        failure_threshold: int = 10,
        cooldown_seconds: float = 60.0,
        half_open_probe_timeout: float = 30.0,
    ) -> None:
        self.clock = clock or time.time
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.half_open_probe_timeout = max(1.0, float(half_open_probe_timeout))
        self._permit_issuer = object()
        self._store = BusinessRequestGovernor(
            state_dir=state_dir,
            clock=self.clock,
            sleep=time.sleep,
        )

    def admit(self, key: str) -> Optional[_CircuitPermit]:
        circuit_key = self._store._hash_key(key)
        with self._store._locked_state() as state:
            circuit = self._circuit(state, circuit_key)
            current_state = str(circuit.get("state") or "closed")
            if current_state == "closed":
                return self._permit(circuit_key, circuit, role="ordinary")
            if current_state == "half_open":
                now = float(self.clock())
                probe_expires_at = float(circuit.get("half_open_probe_expires_at") or 0.0)
                if probe_expires_at <= now:
                    circuit["generation"] = int(circuit.get("generation", 0)) + 1
                    circuit["half_open_probe_id"] = uuid.uuid4().hex
                    circuit["half_open_probe_expires_at"] = now + self.half_open_probe_timeout
                    return self._permit(circuit_key, circuit, role="probe")
                return None

            now = float(self.clock())
            raw_opened_at = circuit.get("opened_at")
            opened_at = now if raw_opened_at is None else float(raw_opened_at)
            if now - opened_at < self.cooldown_seconds:
                return None
            circuit["generation"] = int(circuit.get("generation", 0)) + 1
            circuit["state"] = "half_open"
            circuit["half_open_probe_in_flight"] = True
            circuit["half_open_probe_id"] = uuid.uuid4().hex
            circuit["half_open_probe_expires_at"] = now + self.half_open_probe_timeout
            return self._permit(circuit_key, circuit, role="probe")

    def record_success(self, key: str, permit: _CircuitPermit) -> None:
        circuit_key = self._store._hash_key(key)
        if not isinstance(permit, _CircuitPermit):
            raise TypeError("circuit outcome requires an admission permit")
        with permit.outcome_guard(self._permit_issuer, circuit_key) as accepted:
            if not accepted:
                return
            with self._store._locked_state() as state:
                circuit = self._circuit(state, circuit_key)
                if not self._outcome_matches(circuit, permit):
                    return
                recovered = circuit.get("state") == "half_open"
                if recovered:
                    circuit["generation"] = int(circuit.get("generation", 0)) + 1
                circuit.update({
                    "state": "closed",
                    "consecutive_failures": 0,
                    "opened_at": None,
                    "half_open_probe_in_flight": False,
                    "half_open_probe_id": None,
                    "half_open_probe_expires_at": None,
                })
                if recovered:
                    state["last_recovery_at"] = float(self.clock())

    def record_failure(self, key: str, permit: _CircuitPermit) -> None:
        circuit_key = self._store._hash_key(key)
        if not isinstance(permit, _CircuitPermit):
            raise TypeError("circuit outcome requires an admission permit")
        with permit.outcome_guard(self._permit_issuer, circuit_key) as accepted:
            if not accepted:
                return
            with self._store._locked_state() as state:
                circuit = self._circuit(state, circuit_key)
                if not self._outcome_matches(circuit, permit):
                    return
                failures = int(circuit.get("consecutive_failures", 0)) + 1
                circuit["consecutive_failures"] = failures
                if circuit.get("state") == "half_open" or failures >= self.failure_threshold:
                    circuit["generation"] = int(circuit.get("generation", 0)) + 1
                    circuit.update({
                        "state": "open",
                        "opened_at": float(self.clock()),
                        "half_open_probe_in_flight": False,
                        "half_open_probe_id": None,
                        "half_open_probe_expires_at": None,
                    })

    def record_rate_limited(self, key: str, permit: _CircuitPermit) -> None:
        circuit_key = self._store._hash_key(key)
        if not isinstance(permit, _CircuitPermit):
            raise TypeError("circuit outcome requires an admission permit")
        with permit.outcome_guard(self._permit_issuer, circuit_key) as accepted:
            if not accepted:
                return
            with self._store._locked_state() as state:
                circuit = self._circuit(state, circuit_key)
                if not self._outcome_matches(circuit, permit):
                    return
                if circuit.get("state") == "half_open":
                    circuit["generation"] = int(circuit.get("generation", 0)) + 1
                    circuit.update({
                        "state": "open",
                        "opened_at": float(self.clock()),
                        "half_open_probe_in_flight": False,
                        "half_open_probe_id": None,
                        "half_open_probe_expires_at": None,
                    })

    def status(self, key: str) -> dict[str, Any]:
        circuit_key = self._store._hash_key(key)
        with self._store._read_only_state() as state:
            return dict(self._circuit(state, circuit_key))

    def open_keys(self) -> list[str]:
        with self._store._read_only_state() as state:
            circuits = state.get("circuits") or {}
            return sorted(
                key
                for key, value in circuits.items()
                if isinstance(value, dict) and value.get("state") in {"open", "half_open"}
            )

    def last_recovery_at(self) -> Optional[float]:
        with self._store._read_only_state() as state:
            value = state.get("last_recovery_at")
            return float(value) if value is not None else None

    def _permit(
        self,
        circuit_key: str,
        circuit: dict[str, Any],
        *,
        role: str,
    ) -> _CircuitPermit:
        return _CircuitPermit(
            issuer=self._permit_issuer,
            circuit_key=circuit_key,
            generation=int(circuit.get("generation", 0)),
            role=role,
            probe_id=(
                str(circuit.get("half_open_probe_id"))
                if role == "probe" and circuit.get("half_open_probe_id")
                else None
            ),
        )

    @staticmethod
    def _outcome_matches(
        circuit: dict[str, Any],
        permit: _CircuitPermit,
    ) -> bool:
        current_state = str(circuit.get("state") or "closed")
        if int(circuit.get("generation", 0)) != permit.generation:
            return False
        if permit.role == "ordinary":
            return current_state == "closed"
        return (
            permit.role == "probe"
            and current_state == "half_open"
            and circuit.get("half_open_probe_id") == permit.probe_id
        )

    @staticmethod
    def _circuit(state: dict[str, Any], circuit_key: str) -> dict[str, Any]:
        circuits = state.setdefault("circuits", {})
        defaults = {
            "state": "closed",
            "consecutive_failures": 0,
            "generation": 0,
            "opened_at": None,
            "half_open_probe_in_flight": False,
            "half_open_probe_id": None,
            "half_open_probe_expires_at": None,
        }
        circuit = circuits.setdefault(circuit_key, dict(defaults))
        for field, default in defaults.items():
            circuit.setdefault(field, default)
        return circuit
