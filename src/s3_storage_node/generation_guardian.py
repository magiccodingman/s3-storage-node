from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .generation import GenerationError, GenerationFactory, LocalWriterLease, WorkerGeneration
from .generation_history import GenerationHistory
from .guardian import Guardian as BaseGuardian
from .health import start_server
from .logging import event
from .processes import ManagedProcess, wait_for_tcp
from .s3check import run_canary
from .storage import StorageError, prepare_barrier


def namespace_cwd_command(command: list[str], cwd: str | None) -> list[str]:
    """Establish a working directory after namespace entry, then exec directly."""

    if not cwd:
        return list(command)
    return [
        sys.executable,
        "-m",
        "s3_storage_node.process_exec",
        "--cwd",
        cwd,
        "--",
        *command,
    ]


class Guardian(BaseGuardian):
    """Guardian with isolated worker generations and bounded-drain recovery."""

    def __init__(self, config: Config, config_path: str) -> None:
        super().__init__(config, config_path)
        state_dir = getattr(config.appliance, "state_dir", None) or Path("/tmp/s3-storage-node-test-state")
        runtime_dir = getattr(config.appliance, "runtime_dir", None) or Path("/tmp/s3-storage-node-test-runtime")
        node_name = getattr(config.appliance, "name", "s3-storage-node")
        control_dir = state_dir / "guardian"
        self.writer_lease = LocalWriterLease(control_dir, node_name)
        self.generation_factory = GenerationFactory(control_dir, runtime_dir)
        self.generation_history = GenerationHistory(control_dir)
        self.health.set_generation_history(self.generation_history.snapshot())
        self.generation: WorkerGeneration | None = None
        self.fatal_fence_failure = False

    def run(self) -> int:
        self._install_signals()
        self._prepare_state_directory()
        try:
            self.writer_lease.acquire()
        except GenerationError as exc:
            event("critical", "writer_lease_unavailable", error=str(exc))
            return 3
        self.generation_history.recover_interrupted_active()
        self.health.set_generation_history(self.generation_history.snapshot())
        self.health.set_writer(held=True, owner=self.writer_lease.node_name)
        try:
            self._prepare_directories()
            start_server(self.config.appliance.health_host, self.config.appliance.health_port, self.health)
            delay = self.config.appliance.recovery_initial_seconds
            while not self.stopping:
                try:
                    self._ensure_haproxy()
                    self._begin_generation()
                    self.health.set("MOUNTING", False, "mounting required storage inside worker generation")
                    self._mount_and_enroll_targets()
                    self.health.set("VERIFYING_STORAGE", False, "performing storage durability probe")
                    self._probe_targets(full=True)
                    self._start_seaweed()
                    self._stabilize_recovery()
                    self.health.set("ONLINE", True, "all storage and SeaweedFS checks passed")
                    event("info", "appliance_online", generation=self.generation.generation if self.generation else 0)
                    delay = self.config.appliance.recovery_initial_seconds
                    self._online_loop()
                except KeyboardInterrupt:
                    self.stopping = True
                except Exception as exc:  # noqa: BLE001 - appliance boundary
                    if self.stopping:
                        break
                    phase = str(self.health.snapshot().get("state", "unknown"))
                    cause = self.generation_history.classify_cause(exc, phase)
                    self.health.increment_failure()
                    self.health.set("OFFLINE", False, str(exc))
                    event(
                        "error", "appliance_offline", error=str(exc), error_type=type(exc).__name__,
                        generation_cause=cause, failure_phase=phase,
                    )
                    fenced = self._terminate_generation(str(exc), cause=cause, phase=phase)
                    if not fenced:
                        self.stopping = True
                        break
                    self._retire_generation()
                    attempt = self.health.increment_recovery()
                    event("warning", "recovery_wait", seconds=delay, attempt=attempt)
                    self._interruptible_sleep(delay)
                    delay = min(delay * 2, self.config.appliance.recovery_max_seconds)

            self.health.set("STOPPING", False, "container stopping")
            self._terminate_generation(
                "container stopping", cause="container_shutdown", phase="STOPPING", unmount_all=True,
            )
            if self.haproxy:
                self.haproxy.stop(self.config.appliance.shutdown_grace_seconds)
            self._retire_generation()
            return 4 if self.fatal_fence_failure else 0
        finally:
            self.writer_lease.release()
            self.health.set_writer(held=False, owner=self.writer_lease.node_name)

    def _prepare_state_directory(self) -> None:
        state = self.writer_lease.path.parent.parent
        state.mkdir(parents=True, exist_ok=True)
        os.chown(state, 0, 0)
        os.chmod(state, 0o755)
        control = self.writer_lease.path.parent
        control.mkdir(parents=True, exist_ok=True)
        os.chown(control, 0, 0)
        os.chmod(control, 0o700)

    def _prepare_directories(self) -> None:
        state = self.config.appliance.state_dir
        runtime = self.config.appliance.runtime_dir
        for directory in [state, runtime, state / "master", runtime / "generated", runtime / "mounts"]:
            directory.mkdir(parents=True, exist_ok=True)
        os.chown(state, 0, 0)
        os.chmod(state, 0o755)
        os.chown(state / "master", self.config.appliance.uid, self.config.appliance.gid)
        for target in self.config.active_targets:
            if target.type != "path":
                prepare_barrier(target)
            elif target.allow_initialize and target.mountpoint.is_relative_to(state):
                target.mountpoint.mkdir(parents=True, exist_ok=True)
                os.chown(target.mountpoint, self.config.appliance.uid, self.config.appliance.gid)

    def _begin_generation(self) -> None:
        self._ensure_no_lingering_processes()
        if self.generation is not None:
            self._retire_generation()
        appliance = self.config.appliance
        self.health.set("CREATING_GENERATION", False, "creating isolated worker generation")
        try:
            self.generation = self.generation_factory.create(
                mode=getattr(appliance, "worker_fencing_mode", "disabled"),
                host_address=getattr(appliance, "worker_host_address", "169.254.254.1/30"),
                worker_address=getattr(appliance, "worker_address", "169.254.254.2/30"),
                gateway=getattr(appliance, "worker_gateway", "169.254.254.1"),
            )
        except Exception as exc:
            allocated = self.generation_factory.last_allocated_generation
            if allocated > 0:
                self.generation_history.start(
                    allocated,
                    transport=str(getattr(self, "active_transport", "")),
                    mode=str(getattr(appliance, "worker_fencing_mode", "disabled")),
                )
                self.generation_history.finish(
                    cause="generation_creation_failure",
                    reason=str(exc),
                    phase="CREATING_GENERATION",
                    clean_shutdown=False,
                    fence_verified=False,
                )
                self.health.set_generation_history(self.generation_history.snapshot())
            raise
        self.generation_history.start(
            self.generation.generation,
            transport=str(getattr(self, "active_transport", "")),
            mode=self.generation.mode,
        )
        self.health.set_generation_history(self.generation_history.snapshot())
        self._publish_generation("active")
        event(
            "info", "worker_generation_created",
            generation=self.generation.generation,
            mode=self.generation.mode,
            namespace_pid=self.generation.namespace_pid,
            worker_ip=self.generation.worker_ip,
        )

    def _publish_generation(self, state: str) -> None:
        generation = self.generation
        if generation is None:
            self.health.set_generation({
                "id": 0,
                "mode": getattr(self.config.appliance, "worker_fencing_mode", "disabled"),
                "state": state,
                "fenced": True,
            })
            return
        self.health.set_generation({
            "id": generation.generation,
            "token_id": generation.token[:12],
            "mode": generation.mode,
            "state": state,
            "fenced": generation.fenced,
            "fence_reason": generation.fence_reason,
            "namespace_pid": generation.namespace_pid,
            "worker_ip": generation.worker_ip,
        })

    def _fence_generation(self, reason: str) -> bool:
        if self.generation is None:
            return True
        self.health.set("FENCING", False, reason)
        try:
            self.generation.fence(reason)
        except Exception as exc:  # noqa: BLE001 - physical fencing boundary
            self.fatal_fence_failure = True
            self.health.set("FENCE_FAILED", False, str(exc))
            self._publish_generation("fence_failed")
            event("critical", "worker_generation_fence_failed", generation=self.generation.generation, error=str(exc))
            return False
        self._publish_generation("fenced")
        event("warning", "worker_generation_fenced", generation=self.generation.generation, reason=reason)
        return True

    def _terminate_generation(
        self,
        reason: str,
        *,
        cause: str,
        phase: str,
        unmount_all: bool = False,
    ) -> bool:
        """Drain and detach first, falling back to a verified hard fence."""

        if self.generation is None:
            self._stop_seaweed()
            self._repair_targets(unmount_all=unmount_all)
            return True

        drain_deadline = time.monotonic() + self.config.appliance.shutdown_grace_seconds
        processes_stopped = self._stop_seaweed(deadline=drain_deadline)
        detached = False
        if processes_stopped:
            detached = self._repair_targets(
                unmount_all=unmount_all,
                timeout_seconds=max(0.0, drain_deadline - time.monotonic()),
            )

        clean_shutdown = processes_stopped and detached
        if clean_shutdown:
            event(
                "info", "worker_generation_gracefully_drained",
                generation=self.generation.generation, cause=cause, reason=reason,
            )
            fenced = self._fence_generation(reason)
        else:
            event(
                "warning", "worker_generation_hard_fence_required",
                generation=self.generation.generation, cause=cause, reason=reason,
                processes_stopped=processes_stopped, storage_detached=detached,
            )
            fenced = self._fence_generation(reason)
            self._repair_targets(unmount_all=unmount_all)

        self.generation_history.finish(
            cause=cause,
            reason=reason,
            phase=phase,
            clean_shutdown=clean_shutdown,
            fence_verified=fenced,
        )
        self.health.set_generation_history(self.generation_history.snapshot())
        return fenced

    def _retire_generation(self) -> None:
        if self.generation is None:
            return
        generation = self.generation
        try:
            generation.retire()
        except Exception as exc:  # noqa: BLE001 - cleanup boundary
            self.fatal_fence_failure = True
            generation.stop_keeper()
            self.health.set("FENCE_FAILED", False, str(exc))
            event("critical", "worker_generation_retire_failed", generation=generation.generation, error=str(exc))
        else:
            self._publish_generation("retired")
            event("info", "worker_generation_retired", generation=generation.generation)
        finally:
            self.generation = None

    def _generation_command(self, command: list[str], *, as_worker: bool = False) -> list[str]:
        if self.generation is None:
            if getattr(self.config.appliance, "worker_fencing_mode", "disabled") == "disabled":
                return list(command)
            raise GenerationError("no active worker generation")
        if as_worker:
            return self.generation.enter_command(command, self.config.appliance.uid, self.config.appliance.gid)
        return self.generation.enter_command(command)

    def _run_helper(
        self,
        operation: str,
        target_name: str | None = None,
        *,
        full: bool = False,
        timeout: float,
    ) -> dict[str, object]:
        self._reap_helper_children()
        if operation != "unmount" and self.helper_children:
            raise StorageError("a previous storage helper is still blocked; refusing to start another")
        command = [sys.executable, "-m", "s3_storage_node.main", operation, "--config", self.config_path]
        if target_name is not None:
            command.extend(["--target", target_name])
        if full:
            command.append("--full")
        process = subprocess.Popen(
            self._generation_command(command),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
            self.helper_children.append(process)
            suffix = f" for {target_name}" if target_name else ""
            raise StorageError(f"storage {operation} timed out{suffix}") from exc
        if process.returncode != 0:
            raise StorageError(stderr.strip() or stdout.strip() or f"{operation} failed for {target_name or 'layout'}")
        if not stdout.strip():
            return {}
        return json.loads(stdout)

    def _build_processes(self) -> list[ManagedProcess]:
        processes = super()._build_processes()
        if getattr(self.config.appliance, "worker_fencing_mode", "disabled") != "namespace":
            return processes
        for process in processes:
            command = namespace_cwd_command(process.command, process.cwd)
            process.command = self._generation_command(command, as_worker=True)
            process.uid = None
            process.gid = None
            process.cwd = None
        return processes

    def _worker_endpoint_host(self) -> str:
        return getattr(self.config, "worker_endpoint_host", "127.0.0.1")

    def _run_s3_canary(self) -> None:
        run_canary(
            self.config.seaweed.s3_internal_port,
            self.canary_access,
            self.canary_secret,
            host=self._worker_endpoint_host(),
            external_url=self.config.s3.external_url,
        )

    def _check_seaweed_volumes(self) -> dict[str, object]:
        result = super()._check_seaweed_volumes()
        if not result:
            return result
        history = self.generation_history.snapshot()
        if not history["indexes_certified"]:
            self.generation_history.certify_indexes(
                "SeaweedFS loaded every volume without an unexpected upstream ReadOnly state"
            )
            history = self.generation_history.snapshot()
            self.health.set_generation_history(history)
            event(
                "info", "seaweed_indexes_certified",
                generation=self.generation.generation if self.generation else 0,
                volumes=result.get("total", 0),
            )
        result = dict(result)
        result["orphan_deletion_safe"] = bool(history["indexes_certified"])
        self.health.set_seaweed_volumes(result)
        return result

    def _start_seaweed(self) -> None:
        self.health.set("STARTING_SEAWEED", False, "starting SeaweedFS components")
        self.processes = self._build_processes()
        ports = [
            self.config.seaweed.master_port,
            self.config.seaweed.volume_port,
            self.config.seaweed.filer_port,
            self.config.seaweed.s3_internal_port,
        ]
        for process, port in zip(self.processes, ports, strict=True):
            process.start()
            wait_for_tcp(self._worker_endpoint_host(), port, self.config.appliance.startup_timeout_seconds, process)
        if self.config.appliance.s3_canary_enabled:
            self._run_s3_canary()


def run_guardian(config_path: str) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        event("critical", "configuration_invalid", error=str(exc))
        return 2
    event("info", "guardian_starting", config=config_path, appliance=config.appliance.name, version=__version__)
    return Guardian(config, config_path).run()
