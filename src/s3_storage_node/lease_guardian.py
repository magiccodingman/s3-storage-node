from __future__ import annotations

import os
import threading
import time

from . import __version__
from .config import ConfigError, load_config
from .logging import event
from .safe_writer_lease import SafeWriterLeaseController, load_writer_lease
from .transport_failover import TransportFailoverError, load_exclusive_failover
from .transport_guardian import Guardian as TransportGuardian
from .writer_lease import WriterLeaseError, WriterLeaseLost


class Guardian(TransportGuardian):
    """Transport guardian with optional PostgreSQL dataset-wide writer ownership."""

    def __init__(self, config, config_path: str) -> None:
        super().__init__(config, config_path)
        self.writer_lease_config = load_writer_lease(config_path, config)
        self.writer_epoch = SafeWriterLeaseController(
            self.writer_lease_config,
            on_update=self._publish_writer_epoch,
            on_at_risk=self._writer_epoch_at_risk,
            on_recovered=self._writer_epoch_recovered,
            on_lost=self._writer_epoch_lost,
        )
        self._writer_epoch_lost_event = threading.Event()
        self._writer_epoch_callback_lock = threading.Lock()

    def run(self) -> int:
        try:
            return super().run()
        finally:
            self.writer_epoch.close(release=not self.fatal_fence_failure)
            self._publish_writer_epoch(self.writer_epoch.snapshot())
            os.environ.pop("S3_STORAGE_NODE_FENCING_TOKEN", None)
            os.environ.pop("S3_STORAGE_NODE_WRITER_LEASE", None)

    def _ensure_writer_epoch(self) -> None:
        self.health.set("ACQUIRING_WRITER_LEASE", False, "acquiring dataset writer ownership")
        try:
            token = self.writer_epoch.acquire()
            self.writer_epoch.assert_usable()
        except WriterLeaseError as exc:
            snapshot = self.writer_epoch.snapshot()
            snapshot["last_error"] = str(exc)
            self._publish_writer_epoch(snapshot)
            raise
        self._writer_epoch_lost_event.clear()
        os.environ["S3_STORAGE_NODE_FENCING_TOKEN"] = str(token)
        os.environ["S3_STORAGE_NODE_WRITER_LEASE"] = self.writer_lease_config.lease_name
        self._publish_writer_epoch(self.writer_epoch.snapshot())

    def _begin_generation(self) -> None:
        self._ensure_writer_epoch()
        super()._begin_generation()
        self._publish_generation("active")

    def _publish_writer_epoch(self, snapshot: dict[str, object]) -> None:
        details = dict(snapshot)
        held = bool(details.pop("held", False))
        scope = str(details.pop("scope", "local"))
        owner = str(details.pop("owner", self.writer_lease_config.node_id))
        self.health.set_writer(held=held, scope=scope, owner=owner, **details)
        self.health.set_storage("writer:lease", dict(snapshot))
        generation = dict(self.health.snapshot().get("generation", {}))
        generation.update(
            {
                "writer_lease_name": snapshot.get("lease_name", ""),
                "writer_fencing_token": snapshot.get("fencing_token", 0),
                "writer_lease_healthy": snapshot.get("healthy", False),
            }
        )
        self.health.set_generation(generation)

    def _publish_generation(self, state: str) -> None:
        super()._publish_generation(state)
        self._publish_writer_epoch(self.writer_epoch.snapshot())

    def _writer_epoch_at_risk(self, reason: str) -> None:
        snapshot = self.health.snapshot()
        if snapshot.get("ready"):
            self.health.set("WRITER_LEASE_AT_RISK", False, reason)
        event(
            "warning",
            "writer_lease_at_risk",
            lease=self.writer_lease_config.lease_name,
            token=self.writer_epoch.fencing_token,
            error=reason,
        )

    def _writer_epoch_recovered(self) -> None:
        snapshot = self.health.snapshot()
        generation = snapshot.get("generation", {})
        processes_healthy = bool(self.processes) and all(process.running() for process in self.processes)
        if (
            snapshot.get("state") == "WRITER_LEASE_AT_RISK"
            and not generation.get("fenced", True)
            and processes_healthy
        ):
            self.health.set("ONLINE", True, "writer lease renewal recovered")
        event(
            "info",
            "writer_lease_recovered",
            lease=self.writer_lease_config.lease_name,
            token=self.writer_epoch.fencing_token,
        )

    def _writer_epoch_lost(self, reason: str) -> None:
        with self._writer_epoch_callback_lock:
            self._writer_epoch_lost_event.set()
            self.health.set("WRITER_LEASE_LOST", False, reason)
            event(
                "critical",
                "writer_lease_lost",
                lease=self.writer_lease_config.lease_name,
                token=self.writer_epoch.fencing_token,
                error=reason,
            )
            fenced = self._fence_generation(f"writer lease lost: {reason}")
            if not fenced:
                event(
                    "critical",
                    "writer_lease_loss_fence_failed",
                    lease=self.writer_lease_config.lease_name,
                    token=self.writer_epoch.fencing_token,
                )

    def _fence_generation(self, reason: str) -> bool:
        fenced = super()._fence_generation(reason)
        if not fenced and self.writer_lease_config.backend == "postgres":
            try:
                blocked = self.writer_epoch.block_takeover(reason)
            except WriterLeaseError as exc:
                event(
                    "critical",
                    "writer_lease_takeover_block_failed",
                    lease=self.writer_lease_config.lease_name,
                    token=self.writer_epoch.fencing_token,
                    error=str(exc),
                )
            else:
                event(
                    "critical" if blocked else "error",
                    "writer_lease_takeover_blocked" if blocked else "writer_lease_takeover_block_not_applied",
                    lease=self.writer_lease_config.lease_name,
                    token=self.writer_epoch.fencing_token,
                    reason=reason,
                )
        return fenced

    def _start_seaweed(self) -> None:
        self.writer_epoch.assert_usable()
        super()._start_seaweed()
        self.writer_epoch.assert_usable()

    def _stabilize_recovery(self) -> None:
        self.writer_epoch.assert_usable()
        super()._stabilize_recovery()
        self.writer_epoch.assert_usable()

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping:
            if self._writer_epoch_lost_event.is_set():
                raise WriterLeaseLost(
                    str(self.writer_epoch.snapshot().get("last_error", "writer lease lost"))
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            super()._interruptible_sleep(min(0.5, remaining))


def run_guardian(config_path: str) -> int:
    try:
        config = load_config(config_path)
        load_exclusive_failover(config_path, config)
        load_writer_lease(config_path, config)
    except ConfigError as exc:
        event("critical", "configuration_invalid", error=str(exc))
        return 2
    except TransportFailoverError as exc:
        event("critical", "transport_configuration_invalid", error=str(exc))
        return 2
    except WriterLeaseError as exc:
        event("critical", "writer_lease_configuration_invalid", error=str(exc))
        return 2
    event("info", "guardian_starting", config=config_path, appliance=config.appliance.name, version=__version__)
    return Guardian(config, config_path).run()
