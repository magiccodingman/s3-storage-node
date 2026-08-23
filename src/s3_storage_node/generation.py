from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class GenerationError(RuntimeError):
    pass


RunCommand = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise GenerationError(f"command failed: {' '.join(command)}: {message}")
    return result


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def ensure_ip_forwarding(path: Path = Path("/proc/sys/net/ipv4/ip_forward")) -> None:
    """Enable IPv4 forwarding, accepting a pre-enabled read-only sysctl."""

    try:
        current = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise GenerationError(f"unable to inspect namespace forwarding: {exc}") from exc
    if current == "1":
        return
    try:
        path.write_text("1\n", encoding="ascii")
    except OSError as exc:
        raise GenerationError(f"unable to enable namespace forwarding: {exc}") from exc
    try:
        enabled = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise GenerationError(f"unable to verify namespace forwarding: {exc}") from exc
    if enabled != "1":
        raise GenerationError(f"namespace forwarding remained disabled after update: {enabled!r}")


def namespace_visible_command(command: list[str]) -> list[str]:
    """Expose namespace-managed service listeners to the supervisor veth."""

    return ["-ip.bind=0.0.0.0" if argument == "-ip.bind=127.0.0.1" else argument for argument in command]


def resolver_nameservers(path: Path) -> tuple[str, ...]:
    """Return validated resolver IPs from the guardian namespace configuration."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GenerationError(f"unable to read resolver configuration {path}: {exc}") from exc

    nameservers: list[str] = []
    for line in lines:
        fields = line.split()
        if not fields or fields[0] != "nameserver":
            continue
        if len(fields) < 2:
            raise GenerationError(f"invalid nameserver line in {path}: {line!r}")
        try:
            address = str(ipaddress.ip_address(fields[1]))
        except ValueError as exc:
            raise GenerationError(f"invalid nameserver address in {path}: {fields[1]!r}") from exc
        if address not in nameservers:
            nameservers.append(address)
    if not nameservers:
        raise GenerationError(f"resolver configuration {path} contains no nameserver entries")
    return tuple(nameservers)


def render_worker_resolv_conf(source: Path, destination: Path, gateway: str) -> None:
    """Write a worker-only resolv.conf that reaches the root-side DNS relay."""

    try:
        source_lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GenerationError(f"unable to read resolver configuration {source}: {exc}") from exc

    try:
        gateway_ip = str(ipaddress.ip_address(gateway))
    except ValueError as exc:
        raise GenerationError(f"invalid worker DNS gateway: {gateway!r}") from exc

    preserved: list[str] = []
    for line in source_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key = stripped.split(maxsplit=1)[0]
        if key in {"search", "domain", "options"}:
            preserved.append(stripped)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(f"nameserver {gateway_ip}\n")
        for line in preserved:
            handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o644)
    temporary.replace(destination)


class LocalWriterLease:
    """Exclusive ownership of one appliance state directory."""

    def __init__(self, state_dir: Path, node_name: str) -> None:
        self.path = state_dir / "writer.lock"
        self.node_name = node_name
        self._handle: object | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "another guardian"
            handle.close()
            raise GenerationError(f"writer lease already held: {owner}") from exc
        handle.seek(0)
        handle.truncate()
        json.dump({"node": self.node_name, "pid": os.getpid(), "acquired_at": time.time()}, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@dataclass
class WorkerGeneration:
    generation: int
    token: str
    mode: str
    state_dir: Path
    runtime_dir: Path
    host_address: str
    worker_address: str
    gateway: str
    run_command: RunCommand = _run
    resolver_config: Path = Path("/etc/resolv.conf")
    keeper: subprocess.Popen[bytes] | None = None
    dns_proxy: subprocess.Popen[bytes] | None = None
    fenced: bool = False
    fence_reason: str = ""

    HOST_LINK = "s3g-host"
    PEER_LINK = "s3g-peer"

    @property
    def worker_ip(self) -> str:
        if self.mode != "namespace":
            return "127.0.0.1"
        return str(ipaddress.ip_interface(self.worker_address).ip)

    @property
    def namespace_pid(self) -> int | None:
        return None if self.keeper is None else self.keeper.pid

    def start(self) -> None:
        if self.mode == "disabled":
            self._persist("active")
            return
        if self.mode != "namespace":
            raise GenerationError(f"unsupported worker fencing mode: {self.mode}")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.keeper = subprocess.Popen(
            ["unshare", "--mount", "--net", "--propagation", "private", "--", "sleep", "infinity"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.keeper.poll() is not None:
                stderr = b"" if self.keeper.stderr is None else self.keeper.stderr.read()
                raise GenerationError(f"worker namespace exited during startup: {stderr.decode(errors='replace').strip()}")
            if Path(f"/proc/{self.keeper.pid}/ns/net").exists():
                break
            time.sleep(0.05)
        else:
            self.stop_keeper()
            raise GenerationError("timed out creating worker namespace")
        try:
            self._configure_network()
            self._configure_dns()
        except Exception:
            self.fence("namespace setup failed")
            self.stop_keeper()
            raise
        self._persist("active")

    def _configure_network(self) -> None:
        if self.keeper is None:
            raise GenerationError("worker namespace is not running")
        host = ipaddress.ip_interface(self.host_address)
        worker = ipaddress.ip_interface(self.worker_address)
        if host.network != worker.network:
            raise GenerationError("worker host and namespace addresses must share one subnet")
        subprocess.run(["ip", "link", "del", self.HOST_LINK], capture_output=True, check=False)
        self.run_command(["ip", "link", "add", self.HOST_LINK, "type", "veth", "peer", "name", self.PEER_LINK])
        self.run_command(["ip", "addr", "add", self.host_address, "dev", self.HOST_LINK])
        self.run_command(["ip", "link", "set", self.HOST_LINK, "up"])
        self.run_command(["ip", "link", "set", self.PEER_LINK, "netns", str(self.keeper.pid)])
        prefix = ["nsenter", "--target", str(self.keeper.pid), "--net", "--"]
        self.run_command([*prefix, "ip", "link", "set", "lo", "up"])
        self.run_command([*prefix, "ip", "addr", "add", self.worker_address, "dev", self.PEER_LINK])
        self.run_command([*prefix, "ip", "link", "set", self.PEER_LINK, "up"])
        self.run_command([*prefix, "ip", "route", "replace", "default", "via", self.gateway])
        ensure_ip_forwarding()
        subnet = str(host.network)
        self._ensure_iptables(
            ["-t", "nat", "-C", "POSTROUTING", "-s", subnet, "-j", "MASQUERADE"],
            ["-t", "nat", "-A", "POSTROUTING", "-s", subnet, "-j", "MASQUERADE"],
        )
        self._ensure_iptables(
            ["-C", "FORWARD", "-i", self.HOST_LINK, "-j", "ACCEPT"],
            ["-A", "FORWARD", "-i", self.HOST_LINK, "-j", "ACCEPT"],
        )
        self._ensure_iptables(
            ["-C", "FORWARD", "-o", self.HOST_LINK, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
            ["-A", "FORWARD", "-o", self.HOST_LINK, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        )

    def _configure_dns(self) -> None:
        if self.keeper is None:
            raise GenerationError("worker namespace is not running")
        upstreams = resolver_nameservers(self.resolver_config)
        gateway = str(ipaddress.ip_address(self.gateway))
        worker_resolv = self.runtime_dir / "resolv.conf"
        ready_file = self.runtime_dir / "dns-proxy.ready"
        render_worker_resolv_conf(self.resolver_config, worker_resolv, gateway)
        command = [
            sys.executable,
            "-m",
            "s3_storage_node.dns_proxy",
            "--listen-address",
            gateway,
            "--ready-file",
            str(ready_file),
        ]
        for upstream in upstreams:
            command.extend(["--upstream", upstream])
        self.dns_proxy = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if ready_file.exists():
                break
            if self.dns_proxy.poll() is not None:
                stderr = b"" if self.dns_proxy.stderr is None else self.dns_proxy.stderr.read()
                raise GenerationError(f"worker DNS proxy exited during startup: {stderr.decode(errors='replace').strip()}")
            time.sleep(0.05)
        else:
            self.stop_dns_proxy()
            raise GenerationError("timed out starting worker DNS proxy")

        mount_prefix = ["nsenter", "--target", str(self.keeper.pid), "--mount", "--"]
        try:
            self.run_command([*mount_prefix, "mount", "--bind", str(worker_resolv), "/etc/resolv.conf"])
        except Exception:
            self.stop_dns_proxy()
            raise

    def _ensure_iptables(self, check_args: list[str], add_args: list[str]) -> None:
        checked = subprocess.run(["iptables", "-w", *check_args], text=True, capture_output=True, check=False)
        if checked.returncode != 0:
            self.run_command(["iptables", "-w", *add_args])

    def enter_command(self, command: list[str], uid: int | None = None, gid: int | None = None) -> list[str]:
        if self.mode == "disabled":
            return list(command)
        if self.keeper is None or self.keeper.poll() is not None:
            raise GenerationError("worker namespace is unavailable")
        wrapped = ["nsenter", "--target", str(self.keeper.pid), "--mount", "--net", "--"]
        if uid is not None or gid is not None:
            if uid is None or gid is None:
                raise GenerationError("worker uid and gid must be supplied together")
            wrapped.extend(["setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--"])
        wrapped.extend(namespace_visible_command(command))
        return wrapped

    def fence(self, reason: str) -> None:
        if self.fenced:
            return
        if self.mode == "namespace":
            subprocess.run(["ip", "link", "del", self.HOST_LINK], text=True, capture_output=True, check=False)
            remaining = subprocess.run(
                ["ip", "link", "show", self.HOST_LINK],
                text=True,
                capture_output=True,
                check=False,
            )
            if remaining.returncode == 0:
                raise GenerationError(f"failed to fence worker generation {self.generation}: {self.HOST_LINK} still exists")
            self.stop_dns_proxy()
        self.fenced = True
        self.fence_reason = reason
        self._persist("fenced")

    def stop_dns_proxy(self) -> None:
        proxy = self.dns_proxy
        self.dns_proxy = None
        if proxy is None or proxy.poll() is not None:
            return
        try:
            os.killpg(proxy.pid, signal.SIGTERM)
            proxy.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proxy.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proxy.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def stop_keeper(self) -> None:
        keeper = self.keeper
        self.keeper = None
        if keeper is None or keeper.poll() is not None:
            return
        try:
            os.killpg(keeper.pid, signal.SIGTERM)
            keeper.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(keeper.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                keeper.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def retire(self) -> None:
        try:
            self.fence(self.fence_reason or "generation retired")
        finally:
            self.stop_dns_proxy()
            self.stop_keeper()
        self._persist("retired")

    def _persist(self, state: str) -> None:
        _write_json_atomic(
            self.state_dir / "generation.json",
            {
                "generation": self.generation,
                "token": self.token,
                "mode": self.mode,
                "state": state,
                "namespace_pid": self.namespace_pid,
                "worker_ip": self.worker_ip,
                "fenced": self.fenced,
                "fence_reason": self.fence_reason,
                "updated_at": time.time(),
            },
        )


class GenerationFactory:
    def __init__(self, state_dir: Path, runtime_dir: Path) -> None:
        self.state_dir = state_dir
        self.runtime_dir = runtime_dir
        self.counter_path = state_dir / "generation-counter"
        self.last_allocated_generation = 0

    def create(
        self,
        *,
        mode: str,
        host_address: str,
        worker_address: str,
        gateway: str,
        run_command: RunCommand = _run,
    ) -> WorkerGeneration:
        generation = self._next_generation()
        self.last_allocated_generation = generation
        instance = WorkerGeneration(
            generation=generation,
            token=secrets.token_hex(16),
            mode=mode,
            state_dir=self.state_dir,
            runtime_dir=self.runtime_dir / "generations" / str(generation),
            host_address=host_address,
            worker_address=worker_address,
            gateway=gateway,
            run_command=run_command,
        )
        instance.runtime_dir.mkdir(parents=True, exist_ok=True)
        instance.start()
        return instance

    def _next_generation(self) -> int:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        current = 0
        try:
            current = int(self.counter_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, ValueError):
            current = 0
        next_value = current + 1
        temporary = self.counter_path.with_suffix(".tmp")
        with open(temporary, "w", encoding="ascii") as handle:
            handle.write(f"{next_value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.counter_path)
        directory_fd = os.open(self.state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return next_value
