from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import threading
from pathlib import Path

import pytest

from s3_storage_node.generation import WorkerGeneration


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_NAMESPACE_TESTS") != "1"
    or os.geteuid() != 0
    or any(
        shutil.which(binary) is None
        for binary in ("unshare", "nsenter", "ip", "iptables", "mount", "umount", "getent")
    ),
    reason="requires root namespace tooling and RUN_NAMESPACE_TESTS=1",
)


class UdpDnsServer:
    def __init__(self, address: str, hostname: str, result: str) -> None:
        self.address = address
        self.hostname = hostname.rstrip(".").lower()
        self.result = result
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.socket: socket.socket | None = None

    def start(self) -> None:
        self.thread.start()
        ready = threading.Event()
        for _ in range(100):
            if self.socket is not None:
                return
            ready.wait(0.01)
        raise RuntimeError("test DNS server did not start")

    def stop(self) -> None:
        self.stop_event.set()
        if self.socket is not None:
            self.socket.close()
        self.thread.join(timeout=2)

    def _serve(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.address, 53))
        server.settimeout(0.2)
        self.socket = server
        while not self.stop_event.is_set():
            try:
                query, client = server.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            response = self._response(query)
            if response is not None:
                server.sendto(response, client)

    def _response(self, query: bytes) -> bytes | None:
        if len(query) < 17:
            return None
        offset = 12
        labels: list[str] = []
        while offset < len(query):
            length = query[offset]
            offset += 1
            if length == 0:
                break
            labels.append(query[offset : offset + length].decode("ascii"))
            offset += length
        if offset + 4 >= len(query):
            return None
        question_end = offset + 5
        qtype = struct.unpack("!H", query[offset + 1 : offset + 3])[0]
        hostname = ".".join(labels).lower()
        if hostname != self.hostname or qtype != 1:
            return (
                query[:2]
                + b"\x81\x80"
                + b"\x00\x01\x00\x00\x00\x00\x00\x00"
                + query[12:question_end]
            )
        return (
            query[:2]
            + b"\x81\x80"
            + b"\x00\x01\x00\x01\x00\x00\x00\x00"
            + query[12:question_end]
            + b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x1e\x00\x04"
            + socket.inet_aton(self.result)
        )


def make_generation(tmp_path: Path, *, resolver_config: Path = Path("/etc/resolv.conf")) -> WorkerGeneration:
    return WorkerGeneration(
        generation=1,
        token="integration",
        mode="namespace",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        host_address="169.254.254.1/30",
        worker_address="169.254.254.2/30",
        gateway="169.254.254.1",
        resolver_config=resolver_config,
    )


def test_worker_mount_and_network_namespaces_can_be_fenced(tmp_path: Path) -> None:
    mountpoint = tmp_path / "isolated"
    mountpoint.mkdir()
    generation = make_generation(tmp_path)
    generation.start()
    try:
        subprocess.run(
            generation.enter_command(["mount", "-t", "tmpfs", "tmpfs", str(mountpoint)]),
            check=True,
        )
        subprocess.run(generation.enter_command(["touch", str(mountpoint / "inside")]), check=True)
        assert not (mountpoint / "inside").exists()
        assert subprocess.run(["ip", "link", "show", generation.HOST_LINK], capture_output=True).returncode == 0
        generation.fence("test fence")
        assert subprocess.run(["ip", "link", "show", generation.HOST_LINK], capture_output=True).returncode != 0
    finally:
        try:
            subprocess.run(generation.enter_command(["umount", "-l", str(mountpoint)]), check=False)
        except Exception:
            pass
        generation.retire()


def test_worker_namespace_resolves_hostname_through_root_dns_relay(tmp_path: Path) -> None:
    upstream_address = "127.0.0.54"
    hostname = "storage-box.integration.test"
    expected = "192.0.2.55"
    upstream = UdpDnsServer(upstream_address, hostname, expected)
    upstream.start()
    resolver = tmp_path / "upstream-resolv.conf"
    resolver.write_text(f"nameserver {upstream_address}\noptions ndots:0\n", encoding="utf-8")
    generation = make_generation(tmp_path, resolver_config=resolver)
    generation.start()
    try:
        result = subprocess.run(
            generation.enter_command(["getent", "ahostsv4", hostname]),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout
        worker_resolv = subprocess.run(
            generation.enter_command(["cat", "/etc/resolv.conf"]),
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        assert "nameserver 169.254.254.1" in worker_resolv
        assert upstream_address not in worker_resolv
    finally:
        generation.retire()
        upstream.stop()
