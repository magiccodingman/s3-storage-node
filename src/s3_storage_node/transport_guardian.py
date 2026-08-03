from __future__ import annotations

import os
import time

from . import __version__
from .config import ConfigError, load_config
from .generation_guardian import Guardian as GenerationGuardian
from .logging import event
from .transport_failover import (
    TransportFailoverError,
    TransportSelector,
    load_exclusive_failover,
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
            if self.failover is not None
            else None
        )
        self.active_transport = ""
        self._controlled_transport_switch = False
        self._transport_failure = False
        self._online_transport_watch = False

    def _begin_generation(self) -> None:
        self._transport_failure = False
        self._controlled_transport_switch = False
        if self.transport_selector is not None:
            self.health.set("SELECTING_TRANSPORT", False, "selecting one exclusive data transport")
            self.active_transport = self.transport_selector.select()
            os.environ["S3_STORAGE_NODE_TRANSPORT"] = self.active_transport
            event(
                "info",
                "storage_transport_selected",
                target=self.failover.target,
                transport=self.active_transport,
                primary=self.active_transport == self.failover.primary_name,
            )
        else:
            self.active_transport = ""
            os.environ.pop("S3_STORAGE_NODE_TRANSPORT", None)
        super()._begin_generation()
        self._publish_transport()

    def _publish_transport(self) -> None:
        if self.transport_selector is None or self.failover is None:
            return
        status = self.transport_selector.status()
        self.health.set_storage(
            "transport:data",
            {
                "active_transport": self.active_transport,
                "primary_transport": self.failover.primary_name,
                "using_primary": 1 if self.active_transport == self.failover.primary_name else 0,
                "failback_policy": self.failover.failback_policy,
                "requested_transport": status.get("requested", ""),
                "ordered_transports": ",".join(self.failover.ordered_names),
                "failure_domains": 1,
            },
        )
        generation = self.health.snapshot().get("generation", {})
        generation = dict(generation)
        generation.update(
            {
                "transport": self.active_transport,
                "primary_transport": self.failover.primary_name,
                "using_primary_transport": self.active_transport == self.failover.primary_name,
            }
        )
        self.health.set_generation(generation)

    def _run_helper(
        self,
        operation: str,
        target_name: str | None = None,
        *,
        full: bool = False,
        timeout: int,
    ) -> dict[str, object]:
        try:
            return super()._run_helper(
                operation,
                target_name,
                full=full,
                timeout=timeout,
            )
        except Exception:
            if (
                self.failover is not None
                and target_name == self.failover.target
                and operation in {"mount", "prepare", "probe"}
            ):
                self._transport_failure = True
            raise

    def _stabilize_recovery(self) -> None:
        super()._stabilize_recovery()
        if self.transport_selector is not None and self.active_transport:
            self.transport_selector.record_success(self.active_transport)
            self._transport_failure = False
            self._publish_transport()
            event("info", "storage_transport_stable", transport=self.active_transport)

    def _fence_generation(self, reason: str) -> bool:
        fenced = super()._fence_generation(reason)
        if (
            fenced
            and self.transport_selector is not None
            and self.active_transport
            and not self.stopping
            and not self._controlled_transport_switch
            and self._transport_failure
        ):
            self.transport_selector.record_failure(self.active_transport, reason)
            self._publish_transport()
            event(
                "warning",
                "storage_transport_failed",
                transport=self.active_transport,
                reason=reason,
            )
        if fenced:
            self._publish_transport()
        return fenced

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
                self.health.set(
                    "SUSPECT",
                    False,
                    f"operator requested transport switch to {requested}",
                )
                event(
                    "warning",
                    "storage_transport_switch_requested",
                    current=self.active_transport,
                    requested=requested,
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
