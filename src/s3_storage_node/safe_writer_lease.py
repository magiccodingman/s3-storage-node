from __future__ import annotations

import threading
import time
from typing import Any, Callable, TypeVar

from .writer_lease import (
    LeaseRecord,
    WriterLeaseConfig,
    WriterLeaseController,
    WriterLeaseError,
    WriterLeaseLost,
    load_writer_lease as _load_writer_lease,
)


T = TypeVar("T")


class WriterLeaseOperationTimeout(WriterLeaseError):
    pass


def load_writer_lease(config_path: str, config: Any) -> WriterLeaseConfig:
    lease = _load_writer_lease(config_path, config)
    if (
        lease.backend == "postgres"
        and lease.renew_interval_seconds
        + lease.connect_timeout_seconds
        + lease.fence_margin_seconds
        >= lease.ttl_seconds
    ):
        raise WriterLeaseError(
            "writer_lease.renew_interval_seconds + connect_timeout_seconds + "
            "fence_margin_seconds must be less than ttl_seconds"
        )
    return lease


class SafeWriterLeaseController(WriterLeaseController):
    """Writer lease controller with wall-clock-independent, hard-bounded renewals.

    PostgreSQL owns expiry using its server clock. The guardian never compares
    that timestamp with the appliance clock. Instead, it subtracts the locally
    measured round-trip duration from the configured TTL and records a local
    monotonic deadline. Database calls execute in daemon threads so the guardian
    can self-fence even if libpq, DNS, or a query outlives its normal timeout.

    A timed-out call may finish later and extend the database row, which can only
    delay takeover. Its result is ignored and this controller has already
    withdrawn readiness and fenced the worker generation.
    """

    def _bounded_call(self, operation: Callable[[], T], timeout: float, description: str) -> T:
        completed = threading.Event()
        values: list[T] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                values.append(operation())
            except BaseException as exc:  # noqa: BLE001 - thread boundary
                errors.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(
            target=run,
            name=f"writer-lease-{description}",
            daemon=True,
        )
        thread.start()
        if not completed.wait(max(0.001, timeout)):
            raise WriterLeaseOperationTimeout(
                f"postgres writer lease {description} exceeded {timeout:.3f} seconds"
            )
        if errors:
            error = errors[0]
            if isinstance(error, WriterLeaseError):
                raise error
            raise WriterLeaseError(f"postgres writer lease {description} failed: {error}") from error
        if not values:
            raise WriterLeaseError(f"postgres writer lease {description} returned no result")
        return values[0]

    def _accept_conservative(self, record: LeaseRecord, elapsed: float) -> None:
        remaining = max(0.0, float(self.config.ttl_seconds) - elapsed)
        if remaining <= self.config.fence_margin_seconds:
            raise WriterLeaseLost(
                "writer lease operation consumed the safe TTL window before ownership could be used"
            )
        now_monotonic = time.monotonic()
        with self._lock:
            self._held = True
            self._at_risk = False
            self._lost = False
            self._blocked = record.takeover_blocked
            self._fencing_token = record.fencing_token
            # This is an appliance-side projected timestamp for observability;
            # safety decisions use only _deadline_monotonic.
            self._lease_until_epoch = time.time() + remaining
            self._deadline_monotonic = now_monotonic + remaining
            self._last_error = ""

    def acquire(self) -> int:
        if self.config.backend == "local":
            return super().acquire()
        with self._lock:
            if self._held and not self._lost:
                return self._fencing_token
        assert self.backend is not None
        timeout = float(self.config.connect_timeout_seconds)
        if not self._initialized:
            self._bounded_call(self.backend.initialize, timeout, "initialization")
            self._initialized = True
        started = time.monotonic()
        record = self._bounded_call(
            lambda: self.backend.acquire(self.owner_id),
            timeout,
            "acquisition",
        )
        elapsed = time.monotonic() - started
        if record is None:
            current = self._bounded_call(self.backend.status, timeout, "status")
            detail = "held by another node"
            if current is not None:
                detail = f"held by {current.owner_id} with token {current.fencing_token}"
                if current.takeover_blocked:
                    detail += f"; takeover blocked: {current.block_reason or 'no reason recorded'}"
            from .writer_lease import WriterLeaseUnavailable

            raise WriterLeaseUnavailable(
                f"writer lease {self.config.lease_name!r} unavailable: {detail}"
            )
        self._accept_conservative(record, elapsed)
        self._start_monitor()
        self._publish()
        return record.fencing_token

    def _monitor(self) -> None:
        delay = float(self.config.renew_interval_seconds)
        while not self._stop.wait(delay):
            with self._lock:
                if not self._held or self._lost or self._blocked:
                    return
                token = self._fencing_token
                deadline = self._deadline_monotonic
            assert self.backend is not None
            safe_deadline = deadline - self.config.fence_margin_seconds
            operation_room = safe_deadline - time.monotonic()
            if operation_room <= 0:
                self._mark_lost("writer lease reached its local monotonic fence deadline")
                return
            operation_timeout = min(
                float(self.config.connect_timeout_seconds),
                operation_room,
            )
            started = time.monotonic()
            try:
                record = self._bounded_call(
                    lambda: self.backend.renew(self.owner_id, token),
                    operation_timeout,
                    "renewal",
                )
                if record is None:
                    self._mark_lost("postgres rejected writer lease renewal")
                    return
                elapsed = time.monotonic() - started
                self._accept_conservative(record, elapsed)
            except WriterLeaseOperationTimeout as exc:
                with self._lock:
                    self._renewal_failures_total += 1
                    self._last_error = str(exc)
                self._publish()
                if self.on_at_risk is not None:
                    self.on_at_risk(str(exc))
                # The abandoned call can complete later. Fence now rather than
                # issue parallel renewals or trust a result arriving after the
                # guardian-side deadline.
                self._mark_lost(str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - renewal boundary
                message = str(exc)
                first_risk = False
                with self._lock:
                    self._renewal_failures_total += 1
                    self._last_error = message
                    if not self._at_risk:
                        self._at_risk = True
                        first_risk = True
                self._publish()
                if first_risk and self.on_at_risk is not None:
                    self.on_at_risk(message)
                remaining = safe_deadline - time.monotonic()
                required_room = float(self.config.connect_timeout_seconds)
                if remaining <= required_room:
                    self._mark_lost(
                        f"insufficient safe lease window for another renewal: {message}"
                    )
                    return
                delay = min(
                    float(self.config.retry_interval_seconds),
                    remaining - required_room,
                )
                continue

            recovered = False
            with self._lock:
                recovered = self._at_risk
                self._at_risk = False
                self._lost = False
                self._renewals_total += 1
                self._last_error = ""
            self._publish()
            if recovered and self.on_recovered is not None:
                self.on_recovered()
            delay = float(self.config.renew_interval_seconds)

    def block_takeover(self, reason: str) -> bool:
        if self.config.backend != "postgres":
            return False
        with self._lock:
            token = self._fencing_token
        if token <= 0 or self.backend is None:
            return False
        blocked = self._bounded_call(
            lambda: self.backend.block_takeover(self.owner_id, token, reason),
            float(self.config.connect_timeout_seconds),
            "takeover block",
        )
        if blocked:
            with self._lock:
                self._blocked = True
                self._held = False
                self._last_error = reason
            self._stop.set()
            self._publish()
        return blocked

    def close(self, *, release: bool = True) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, float(self.config.connect_timeout_seconds + 1)))
        with self._lock:
            held = self._held
            blocked = self._blocked
            token = self._fencing_token
        if release and held and not blocked and self.config.backend == "postgres" and self.backend is not None:
            try:
                self._bounded_call(
                    lambda: self.backend.release(self.owner_id, token),
                    float(self.config.connect_timeout_seconds),
                    "release",
                )
            except WriterLeaseError:
                pass
        with self._lock:
            self._held = False
        self._publish()
