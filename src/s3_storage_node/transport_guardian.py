from __future__ import annotations

import os
import time

from . import __version__
from .config import ConfigError, load_config
from .generation_guardian import Guardian as GenerationGuardian
from .logging import event
from .storage import StorageError, _stop_sshfs_process
from .transport_failover import (
    TransportFailoverError,
    TransportSelector,
    load_exclusive_failover,
    resolve_target,
    startup_verification_fingerprint,
)


class TransportSwitchRequested(RuntimeError):
    pass


class Guardian(GenerationGuardian):
    """Generation guardian with one-at-a-time transport selection and failover."""

    def __init__(self, config, config_path: str) -> None:
        super().__init__(config, config_path)
        self.failover = load_exclusive_failover(config_path, config)
        self.transport_selector = (
            TransportSelector(config.appliance.state_dir / "guardian", self.failover)
            if self.failover is not None else None
        )
        self.active_transport = ""
        self._controlled_transport_switch = False
        self._controlled_transport_detached = False
        self._transport_failure = False
        self._transport_failure_name = ""
        self._online_transport_watch = False
        self._startup_verification_pending = False
        self._startup_verification_transport = ""
        self._startup_verification_fingerprint = ""

    def _begin_generation(self) -> None:
        self._transport_failure = False
        self._transport_failure_name = ""
        self._controlled_transport_switch = False
        self._controlled_transport_detached = False
        self._startup_verification_transport = ""
        self._ensure_no_lingering_processes()
        self._reap_helper_children()
        if self.helper_children:
            raise StorageError("a previous storage helper is still blocked; refusing to select another transport")
        if self.transport_selector is not None and self.failover is not None:
            self.health.set("SELECTING_TRANSPORT", False, "selecting one exclusive data transport")
            self.active_transport = self.transport_selector.select()
            os.environ["S3_STORAGE_NODE_TRANSPORT"] = self.active_transport
            self._startup_verification_fingerprint = startup_verification_fingerprint(self.config, self.failover)
            self._startup_verification_pending = (
                self.failover.verify_all_transports_on_startup
                and self.transport_selector.startup_verification_required(self._startup_verification_fingerprint)
            )
            event(
                "info", "storage_transport_selected", target=self.failover.target,
                transport=self.active_transport,
                primary=self.active_transport == self.failover.primary_name,
                startup_verification_pending=self._startup_verification_pending,
            )
        else:
            self.active_transport = ""
            self._startup_verification_pending = False
            os.environ.pop("S3_STORAGE_NODE_TRANSPORT", None)
        super()._begin_generation()
        self._publish_transport()

    def _publish_transport(self) -> None:
        if self.transport_selector is None or self.failover is None:
            return
        status = self.transport_selector.status()
        self.health.set_storage("transport:data", {
            "active_transport": self.active_transport,
            "primary_transport": self.failover.primary_name,
            "using_primary": 1 if self.active_transport == self.failover.primary_name else 0,
            "failback_policy": self.failover.failback_policy,
            "requested_transport": status.get("requested", ""),
            "ordered_transports": ",".join(self.failover.ordered_names),
            "failure_domains": 1,
            "startup_verification_enabled": 1 if self.failover.verify_all_transports_on_startup else 0,
            "startup_verification_pending": 1 if self._startup_verification_pending else 0,
            "startup_verification_transport": self._startup_verification_transport,
            "startup_verified_at": status.get("startup_verified_at", 0.0),
            "startup_verified_transports": ",".join(status.get("startup_verified_transports", [])),
        })
        generation = dict(self.health.snapshot().get("generation", {}))
        generation.update({
            "transport": self.active_transport,
            "primary_transport": self.failover.primary_name,
            "using_primary_transport": self.active_transport == self.failover.primary_name,
            "startup_verification_pending": self._startup_verification_pending,
            "startup_verification_transport": self._startup_verification_transport,
        })
        self.health.set_generation(generation)

    def _run_helper(self, operation: str, target_name: str | None = None, *, full: bool = False, timeout: float) -> dict[str, object]:
        try:
            return super()._run_helper(operation, target_name, full=full, timeout=timeout)
        except Exception:
            if (self.failover is not None and target_name == self.failover.target
                    and operation in {"mount", "prepare", "probe"}):
                self._transport_failure = True
                self._transport_failure_name = self._startup_verification_transport or self.active_transport
            raise

    def _verify_transport_on_startup(self, transport: str) -> None:
        assert self.failover is not None
        os.environ["S3_STORAGE_NODE_TRANSPORT"] = transport
        self._startup_verification_transport = transport
        self.health.set("VERIFYING_TRANSPORTS", False, f"verifying configured data transport {transport}")
        self._publish_transport()
        event("info", "storage_transport_startup_verification_started", transport=transport)
        original_error: Exception | None = None
        try:
            self._run_helper("mount", self.failover.target, timeout=self.config.appliance.startup_timeout_seconds)
            self._run_helper("prepare", self.failover.target, timeout=self.config.appliance.startup_timeout_seconds)
            result = self._run_helper(
                "probe", self.failover.target, full=True,
                timeout=self.config.appliance.startup_timeout_seconds,
            )
            self.health.set_storage(f"startup:{transport}", result)
        except Exception as exc:
            original_error = exc
        try:
            self._run_helper("unmount", self.failover.target, timeout=self.config.appliance.startup_timeout_seconds)
        except Exception as cleanup_error:
            if original_error is None:
                raise
            event(
                "error", "storage_transport_startup_verification_cleanup_failed",
                transport=transport, error=str(cleanup_error),
            )
        if original_error is not None:
            raise original_error
        event("info", "storage_transport_startup_verification_passed", transport=transport)

    def _verify_all_transports_on_startup(self) -> None:
        if not self._startup_verification_pending or self.failover is None or self.transport_selector is None:
            return
        selected = self.active_transport
        verified: list[str] = []
        try:
            for transport in self.failover.ordered_names:
                self._verify_transport_on_startup(transport)
                verified.append(transport)
            self.transport_selector.record_startup_verification(
                self._startup_verification_fingerprint, tuple(verified)
            )
            self._startup_verification_pending = False
            event(
                "info", "storage_transport_startup_verification_complete",
                transports=",".join(verified),
            )
        finally:
            self._startup_verification_transport = ""
            os.environ["S3_STORAGE_NODE_TRANSPORT"] = selected
            self._publish_transport()

    def _mount_and_enroll_targets(self) -> None:
        self._verify_all_transports_on_startup()
        super()._mount_and_enroll_targets()

    def _stabilize_recovery(self) -> None:
        super()._stabilize_recovery()
        if self.transport_selector is not None and self.active_transport:
            self.transport_selector.record_success(self.active_transport)
            self._transport_failure = False
            self._transport_failure_name = ""
            self._publish_transport()
            event("info", "storage_transport_stable", transport=self.active_transport)

    def _fence_generation(self, reason: str) -> bool:
        fenced = super()._fence_generation(reason)
        failed_transport = self._transport_failure_name or self.active_transport
        if fenced and self.failover is not None and failed_transport:
            try:
                target = resolve_target(
                    self.config_path,
                    self.config,
                    self.failover.target,
                    failed_transport,
                )
                if target.type == "sshfs":
                    _stop_sshfs_process(target)
                    event(
                        "info",
                        "fenced_sshfs_process_reaped",
                        transport=failed_transport,
                    )
            except Exception as exc:  # noqa: BLE001 - cleanup follows a verified network fence
                event(
                    "error",
                    "fenced_sshfs_process_cleanup_failed",
                    transport=failed_transport,
                    error=str(exc),
                )
        if (fenced and self.transport_selector is not None and failed_transport
                and not self.stopping and not self._controlled_transport_switch and self._transport_failure):
            self.transport_selector.record_failure(failed_transport, reason)
            self._publish_transport()
            event("warning", "storage_transport_failed", transport=failed_transport, reason=reason)
        if fenced:
            self._publish_transport()
        return fenced

    def _repair_targets(self, unmount_all: bool = False, *, timeout_seconds: float | None = None) -> bool:
        if self._controlled_transport_detached and not unmount_all:
            self._controlled_transport_detached = False
            return True
        previous = os.environ.get("S3_STORAGE_NODE_TRANSPORT")
        repair_transport = self._transport_failure_name or self.active_transport
        if repair_transport:
            os.environ["S3_STORAGE_NODE_TRANSPORT"] = repair_transport
        try:
            return super()._repair_targets(unmount_all=unmount_all, timeout_seconds=timeout_seconds)
        finally:
            if previous is None:
                os.environ.pop("S3_STORAGE_NODE_TRANSPORT", None)
            else:
                os.environ["S3_STORAGE_NODE_TRANSPORT"] = previous

    def _online_loop(self) -> None:
        self._online_transport_watch = True
        try:
            super()._online_loop()
        finally:
            self._online_transport_watch = False

    def _interruptible_sleep(self, seconds: float) -> None:
        if not self._online_transport_watch or self.transport_selector is None:
            return super()._interruptible_sleep(seconds)
        deadline = time.monotonic() + seconds
        while not self.stopping:
            requested = self.transport_selector.pending_request()
            if requested and requested != self.active_transport:
                self._controlled_transport_switch = True
                self.health.set("SUSPECT", False, f"operator requested transport switch to {requested}")
                event(
                    "warning", "storage_transport_switch_requested",
                    current=self.active_transport, requested=requested,
                )
                raise TransportSwitchRequested(f"operator requested transport switch to {requested}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))


def run_guardian(config_path: str) -> int:
    try:
        config = load_config(config_path)
        load_exclusive_failover(config_path, config)
    except ConfigError as exc:
        event("critical", "configuration_invalid", error=str(exc))
        return 2
    except TransportFailoverError as exc:
        event("critical", "transport_configuration_invalid", error=str(exc))
        return 2
    event("info", "guardian_starting", config=config_path, appliance=config.appliance.name, version=__version__)
    return Guardian(config, config_path).run()
