from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

from . import __version__
from .config import Config, ConfigError, load_config
from .health import HealthState, start_server
from .logging import event
from .processes import ManagedProcess, ProcessError, wait_for_tcp
from .render import render_filer_toml, render_haproxy, render_s3_config
from .s3check import run_canary
from .seaweed_health import UnexpectedReadonlyVolumes, inspect_volume_status
from .storage import StorageError, prepare_barrier


class Guardian:
    def __init__(self, config: Config, config_path: str) -> None:
        self.config = config
        self.config_path = config_path
        self.health = HealthState()
        self.stopping = False
        self.processes: list[ManagedProcess] = []
        self.haproxy: ManagedProcess | None = None
        self.canary_access: str | None = None
        self.canary_secret: str | None = None
        self.lingering_processes: list[ManagedProcess] = []
        self.helper_children: list[subprocess.Popen[str]] = []

    def run(self) -> int:
        self._install_signals()
        self._prepare_directories()
        start_server(self.config.appliance.health_host, self.config.appliance.health_port, self.health)
        delay = self.config.appliance.recovery_initial_seconds

        while not self.stopping:
            try:
                self._ensure_haproxy()
                self.health.set("MOUNTING", False, "mounting required storage")
                self._mount_and_enroll_targets()
                self.health.set("VERIFYING_STORAGE", False, "performing storage durability probe")
                self._probe_targets(full=True)
                self._start_seaweed()
                self._stabilize_recovery()
                self.health.set("ONLINE", True, "all storage and SeaweedFS checks passed")
                event("info", "appliance_online")
                delay = self.config.appliance.recovery_initial_seconds
                self._online_loop()
            except KeyboardInterrupt:
                self.stopping = True
            except Exception as exc:  # noqa: BLE001 - appliance boundary
                if self.stopping:
                    break
                self.health.increment_failure()
                self.health.set("OFFLINE", False, str(exc))
                event("error", "appliance_offline", error=str(exc), error_type=type(exc).__name__)
                self._stop_seaweed()
                self._repair_targets()
                attempt = self.health.increment_recovery()
                event("warning", "recovery_wait", seconds=delay, attempt=attempt)
                self._interruptible_sleep(delay)
                delay = min(delay * 2, self.config.appliance.recovery_max_seconds)

        self.health.set("STOPPING", False, "container stopping")
        self._stop_seaweed()
        if self.haproxy:
            self.haproxy.stop(self.config.appliance.shutdown_grace_seconds)
        self._repair_targets(unmount_all=True)
        return 0

    def _install_signals(self) -> None:
        def stop(signum: int, _frame: object) -> None:
            event("info", "shutdown_requested", signal=signum)
            self.stopping = True
            self.health.set("STOPPING", False, "shutdown requested")

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    def _prepare_directories(self) -> None:
        state = self.config.appliance.state_dir
        runtime = self.config.appliance.runtime_dir
        for directory in [state, runtime, state / "master", runtime / "generated", runtime / "mounts"]:
            directory.mkdir(parents=True, exist_ok=True)
        os.chown(state, self.config.appliance.uid, self.config.appliance.gid)
        os.chown(state / "master", self.config.appliance.uid, self.config.appliance.gid)
        for target in self.config.active_targets:
            if target.type != "path":
                prepare_barrier(target)
            elif target.allow_initialize and target.mountpoint.is_relative_to(state):
                target.mountpoint.mkdir(parents=True, exist_ok=True)
                os.chown(target.mountpoint, self.config.appliance.uid, self.config.appliance.gid)

    def _ensure_haproxy(self) -> None:
        if self.haproxy and self.haproxy.running():
            return
        path = render_haproxy(self.config)
        self.haproxy = ManagedProcess(
            "haproxy",
            ["/usr/sbin/haproxy", "-W", "-db", "-f", str(path)],
            self.config.appliance.uid,
            self.config.appliance.gid,
        )
        self.haproxy.start()
        time.sleep(0.25)
        if not self.haproxy.running():
            raise RuntimeError(f"HAProxy exited with code {self.haproxy.exit_code()}")

    def _mount_and_enroll_targets(self) -> None:
        self._ensure_no_lingering_processes()
        for target in self.config.active_targets:
            self._run_helper("mount", target.name, timeout=self.config.appliance.startup_timeout_seconds)
            self._run_helper("prepare", target.name, timeout=self.config.appliance.startup_timeout_seconds)
            event("info", "storage_mounted", target=target.name, type=target.type, path=str(target.storage_root))
        self._run_helper("prepare-layout", timeout=self.config.appliance.startup_timeout_seconds)

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
        command = [
            sys.executable,
            "-m",
            "s3_storage_node.main",
            operation,
            "--config",
            self.config_path,
        ]
        if target_name is not None:
            command.extend(["--target", target_name])
        if full:
            command.append("--full")
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
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

    def _run_probe(self, target_name: str, full: bool) -> dict[str, object]:
        return self._run_helper(
            "probe",
            target_name,
            full=full,
            timeout=self.config.appliance.probe_timeout_seconds,
        )

    def _probe_targets(self, full: bool) -> None:
        started = time.monotonic()
        try:
            for target in self.config.active_targets:
                result = self._run_probe(target.name, full)
                self.health.set_storage(target.name, result)
        except Exception as exc:
            self.health.record_probe(False, time.monotonic() - started, str(exc))
            raise
        self.health.record_probe(True, time.monotonic() - started)

    def _build_processes(self) -> list[ManagedProcess]:
        config = self.config
        filer_toml = render_filer_toml(config)
        s3_config, self.canary_access, self.canary_secret = render_s3_config(config)
        for generated in (filer_toml, s3_config):
            if generated and generated.is_relative_to(config.appliance.runtime_dir):
                os.chown(generated, config.appliance.uid, config.appliance.gid)
        weed = config.seaweed.binary
        uid = config.appliance.uid
        gid = config.appliance.gid
        master_state = config.appliance.state_dir / "master"

        master = [
            weed, "master",
            "-ip=127.0.0.1", "-ip.bind=127.0.0.1",
            f"-port={config.seaweed.master_port}",
            f"-mdir={master_state}",
            f"-volumeSizeLimitMB={config.seaweed.volume_size_limit_mb}",
            f"-defaultReplication={config.seaweed.default_replication}",
            *config.seaweed.master_extra_args,
        ]
        volume = [
            weed, "volume",
            "-ip=127.0.0.1", "-ip.bind=127.0.0.1",
            f"-port={config.seaweed.volume_port}",
            f"-master=127.0.0.1:{config.seaweed.master_port}",
            f"-dir={config.volume_path}",
            f"-dir.idx={config.index_path}",
            f"-max={config.seaweed.volume_max}",
            "-index=memory",
        ]
        if config.seaweed.data_center:
            volume.append(f"-dataCenter={config.seaweed.data_center}")
        if config.seaweed.rack:
            volume.append(f"-rack={config.seaweed.rack}")
        if config.seaweed.disk_type:
            volume.append(f"-disk={config.seaweed.disk_type}")
        volume.extend(config.seaweed.volume_extra_args)

        filer = [
            weed, "filer",
            "-ip=127.0.0.1", "-ip.bind=127.0.0.1",
            f"-port={config.seaweed.filer_port}",
            f"-master=127.0.0.1:{config.seaweed.master_port}",
            f"-maxMB={config.seaweed.filer_max_mb}",
            f"-defaultReplicaPlacement={config.seaweed.default_replication}",
            *config.seaweed.filer_extra_args,
        ]

        s3 = [
            weed, "s3",
            "-ip.bind=127.0.0.1",
            f"-port={config.seaweed.s3_internal_port}",
            "-port.iceberg=0",
            f"-filer=127.0.0.1:{config.seaweed.filer_port}",
            f"-allowedOrigins={config.s3.allowed_origins}",
        ]
        if config.s3.domain_name:
            s3.append(f"-domainName={config.s3.domain_name}")
        if config.s3.external_url:
            s3.append(f"-externalUrl={config.s3.external_url}")
        if s3_config:
            s3.append(f"-config={s3_config}")
        if config.s3.iam_config_file:
            s3.append(f"-iam.config={config.s3.iam_config_file}")
        if config.s3.audit_log_config_file:
            s3.append(f"-auditLogConfig={config.s3.audit_log_config_file}")
        if config.seaweed.encrypt_volume_data:
            s3.append("-encryptVolumeData=true")
        s3.extend(config.seaweed.s3_extra_args)

        return [
            ManagedProcess("master", master, uid, gid),
            ManagedProcess("volume", volume, uid, gid),
            ManagedProcess("filer", filer, uid, gid, cwd=str(filer_toml.parent) if filer_toml else None),
            ManagedProcess("s3", s3, uid, gid),
        ]

    def _worker_endpoint_host(self) -> str:
        return str(getattr(self.config, "worker_endpoint_host", "127.0.0.1"))

    def _run_s3_canary(self) -> None:
        run_canary(
            self.config.seaweed.s3_internal_port,
            self.canary_access,
            self.canary_secret,
            external_url=self.config.s3.external_url,
        )

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
            wait_for_tcp("127.0.0.1", port, self.config.appliance.startup_timeout_seconds, process)
        if self.config.appliance.s3_canary_enabled:
            self._run_s3_canary()
        self._check_seaweed_volumes()

    def _check_seaweed_volumes(self) -> dict[str, object]:
        if not getattr(self.config.seaweed, "volume_health_enabled", False):
            return {}
        result = inspect_volume_status(
            self._worker_endpoint_host(),
            self.config.seaweed.volume_port,
            expected_readonly_ids=set(getattr(self.config.seaweed, "expected_readonly_volume_ids", ())),
            timeout_seconds=self.config.appliance.probe_timeout_seconds,
        )
        self.health.set_seaweed_volumes(result)
        unexpected = result["unexpected_readonly_volume_ids"]
        if unexpected:
            raise UnexpectedReadonlyVolumes(result)
        return result

    def _stabilize_recovery(self) -> None:
        stability_seconds = getattr(self.config.appliance, "recovery_stability_seconds", 15)
        probe_interval = getattr(self.config.appliance, "recovery_probe_interval_seconds", 2)
        required_successes = getattr(self.config.appliance, "recovery_successes_required", 3)
        stable_since_monotonic = time.monotonic()
        self.health.set_recovery_stable_since(time.time())
        self.health.set("VERIFYING_RECOVERY", False, "waiting for sustained storage and S3 health")
        successes = 0
        while not self.stopping:
            self._probe_targets(full=True)
            self._check_seaweed_volumes()
            if self.config.appliance.s3_canary_enabled:
                self._run_s3_canary()
            successes += 1
            elapsed = time.monotonic() - stable_since_monotonic
            if successes >= required_successes and elapsed >= stability_seconds:
                event("info", "recovery_stable", seconds=elapsed, consecutive_successes=successes)
                return
            self._interruptible_sleep(probe_interval)
        raise KeyboardInterrupt

    def _stop_seaweed(self, *, deadline: float | None = None) -> bool:
        self.health.set("DRAINING", False, "stopping SeaweedFS")
        if deadline is None:
            deadline = time.monotonic() + self.config.appliance.shutdown_grace_seconds
        all_stopped = True
        for process in reversed(self.processes):
            try:
                stop_until = getattr(process, "stop_until", None)
                if callable(stop_until):
                    stopped = stop_until(deadline)
                else:
                    stopped = process.stop(max(0, int(deadline - time.monotonic())))
                if not stopped:
                    all_stopped = False
                    if process not in self.lingering_processes:
                        self.lingering_processes.append(process)
            except Exception as exc:  # noqa: BLE001
                all_stopped = False
                if process.running() and process not in self.lingering_processes:
                    self.lingering_processes.append(process)
                event("error", "process_stop_failed", process=process.name, error=str(exc))
        self.processes = []
        return all_stopped

    def _online_loop(self) -> None:
        next_full = time.monotonic() + self.config.appliance.full_probe_interval_seconds
        while not self.stopping:
            try:
                if self.haproxy and not self.haproxy.running():
                    raise ProcessError(f"HAProxy exited with code {self.haproxy.exit_code()}")
                for process in self.processes:
                    if not process.running():
                        raise ProcessError(f"{process.name} exited with code {process.exit_code()}")
                full = time.monotonic() >= next_full
                self._probe_targets(full=full)
                if full:
                    self._check_seaweed_volumes()
                    if self.config.appliance.s3_canary_enabled:
                        self._run_s3_canary()
                    next_full = time.monotonic() + self.config.appliance.full_probe_interval_seconds
            except Exception as exc:
                self.health.set("SUSPECT", False, str(exc))
                event("warning", "appliance_suspect", error=str(exc), error_type=type(exc).__name__)
                raise
            self._interruptible_sleep(self.config.appliance.probe_interval_seconds)

    def _repair_targets(self, unmount_all: bool = False, *, timeout_seconds: float | None = None) -> bool:
        self.health.set("RECOVERING", False, "repairing storage mounts")
        detached = True
        detach_deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        for target in reversed(self.config.active_targets):
            if target.type == "path" and not unmount_all:
                continue
            try:
                timeout = (
                    self.config.appliance.startup_timeout_seconds
                    if detach_deadline is None
                    else max(0.0, detach_deadline - time.monotonic())
                )
                if timeout <= 0:
                    detached = False
                    event("error", "storage_detach_deadline_expired", target=target.name)
                    continue
                self._run_helper("unmount", target.name, timeout=timeout)
                event("info", "storage_detached", target=target.name)
            except Exception as exc:  # noqa: BLE001
                detached = False
                event("error", "storage_detach_failed", target=target.name, error=str(exc))
        return detached

    def _ensure_no_lingering_processes(self) -> None:
        alive = [process for process in self.lingering_processes if process.running()]
        self.lingering_processes = alive
        if alive:
            names = ", ".join(process.name for process in alive)
            raise ProcessError(f"previous SeaweedFS processes are still blocked: {names}")

    def _reap_helper_children(self) -> None:
        alive: list[subprocess.Popen[str]] = []
        for process in self.helper_children:
            if process.poll() is None:
                alive.append(process)
            else:
                for stream in (process.stdout, process.stderr):
                    if stream and not stream.closed:
                        stream.close()
        self.helper_children = alive

    def _interruptible_sleep(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(0.25, deadline - time.monotonic()))


def run_guardian(config_path: str) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        event("critical", "configuration_invalid", error=str(exc))
        return 2
    event(
        "info",
        "guardian_starting",
        config=config_path,
        appliance=config.appliance.name,
        version=__version__,
    )
    return Guardian(config, config_path).run()
